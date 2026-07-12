# stock-news-alerts

A personal research/decision-support tool. Its default mode watches the **entire
NSE market's corporate-announcement stream** in near-real-time, keeps only the
**high-impact** filings (order wins, results, M&A, fund-raising, rating changes,
penalties…), classifies each with an LLM (event type, direction, materiality,
horizon), and pushes only the high-confidence, directional ones to Telegram.

This is the same backbone the pro platforms (Dhan/ScanX/Groww "News Flash") use —
exchange filings are the fastest, highest-signal, most confirmable source of
stock-moving news. This tool filters that firehose down to what matters.

**This is NOT an auto-trading system.** No order execution, no broker integration.
Everything runs on free tools only.

### What free tools can and can't do (be honest)
- ✅ Market-wide exchange filings, minute-fresh, category-filtered to high-impact.
- ✅ LLM direction + materiality + impact scoring, Telegram alerts, 24/7.
- ⚠️ "Real-time" = **~2-minute polling**, not sub-second push. No free service
  offers websocket-push Indian filings; filings only appear every few minutes
  anyway, so polling loses very little.
- ❌ No paid news-wire breadth (Reuters/PTI/Accord), no reliable live per-stock
  prices (NSE quote API is rate-limited; yfinance is stale for NSE).
- ❌ Analyst-rating changes and macro/policy news mostly live in **media, not
  filings** — for those, use `COVERAGE_MODE=watchlist` (adds Google/ET/Moneycontrol
  for your tickers).

## ⚠️ Read this before trusting an alert

- **The confidence table (`confidence_table.yaml`) is a starting heuristic, not a
  statistically validated model.** The base rates are subjective priors about how
  often an event type tends to move a stock in a given direction — they have not
  been backtested against actual price outcomes. `src/scoring/confidence.py`
  documents an extension point (`ConfidenceProvider`) for a future backtested
  version; that backtester does not exist yet.
- **LLM classification runs on free/local models (Groq's free tier, optionally
  local Ollama), not a hosted frontier model.** It will misclassify some articles
  and occasionally return malformed output — the pipeline handles that (see
  "How failures are handled" below) but accuracy is a real trade-off for going
  fully free.
- **The NSE/BSE corporate-announcements endpoints are unofficial** and may change
  or block requests without notice; the fetchers fail soft (return nothing) if
  they do.
- Treat every alert as a filtered starting point for your own research — not a
  signal to act on directly.

## Architecture

```
COVERAGE_MODE=market_wide (default):
  poll NSE market-wide announcement feed (every ~2 min, all stocks)
    -> impact gate: keep HIGH/MEDIUM categories, DROP procedural noise (pre-LLM)
    -> dedupe (URL / headline hash)
    -> classify survivors (LLM: event_type, direction, materiality, horizon)
    -> score (static confidence table + magnitude/materiality nudges)
    -> store (SQLite)
    -> alert gate (directional + confident + HIGH-impact OR material)
    -> alert (Telegram, with the exchange category tag)

COVERAGE_MODE=watchlist:
  per-ticker NSE/BSE filings + Google News + ET/Moneycontrol RSS + NewsAPI
  (same classify/score/alert stages; adds media coverage for your tickers)
```

### The impact filter (the "only what matters" gate)

Every NSE filing carries a category (`desc`). `src/scoring/impact.py` maps it to:

- **`high`** — order wins, acquisitions/M&A, results, fund-raising, credit
  ratings, buybacks, dividends, penalties/fraud, KMP (MD/CEO/CFO) changes,
  insolvency. Always classified; a HIGH category counts as material by itself.
- **`medium`** — board meetings, litigation, appointments, agreements, updates.
  Classified, but must clear the LLM materiality bar to alert.
- **`drop`** — procedural compliance noise (SEBI certificates, trading-window
  closures, newspaper publications, record dates, investor-complaint statements).
  **Dropped before the LLM** — never classified, never alerted. This is what keeps
  the volume down and the free-tier LLM budget spent only on real catalysts.

Unknown categories default to `medium` so nothing genuinely material is silently
lost. Tiers are a documented heuristic, not a backtested model.

### Why not Finnhub?

The original brief for this kind of tool suggests Finnhub's free `company-news`
endpoint. Its free tier only covers **US-listed** companies — it returns nothing
for NSE/BSE tickers — so it's dropped entirely here in favor of sources that
actually work for the Indian market:

1. **NSE corporate announcements** — free, no API key. Primary source for
   filings: results, order wins, buybacks, dividends. Queried **per symbol** with
   a warmed-up cookie session (a plain request to NSE's API is rejected, and the
   index-wide feed only returns the ~20 latest filings market-wide — useless for a
   focused watchlist). This is the authoritative, freshest source.
2. **BSE corporate announcements** — free and no API key, but requires numeric
   `bse_code` values in `watchlist.yaml`. Optional per stock; entries without a
   BSE code are skipped.
3. **Economic Times + Moneycontrol RSS** — free, no API key. Market-wide feeds
   filtered to your watchlist by company name. Fresh mainstream-media coverage.
4. **Google News RSS** — free, no API key. Uses the `when:Nd` recency operator
   (derived from `MAX_NEWS_AGE_HOURS`) and keeps only the freshest items per
   ticker. **This recency filter is important** — without it Google returns
   relevance-ranked results that mix in weeks-old articles, which was the original
   cause of stale alerts.
5. **NewsAPI** (free dev tier) — 100 requests/day, so it's the most restricted:
   the app tracks a daily counter in the database and stops querying it once
   `NEWSAPI_DAILY_CAP` (default 90) is hit for the day, and queries each ticker
   at most once per hour.

**Freshness:** only articles newer than `MAX_NEWS_AGE_HOURS` (default 48) are ever
stored or alerted, at three layers — the Google `when:` window, a fetch-time age
filter, and the pending-alert query. **Coverage:** NSE/BSE announcements only fire
when a company *actually files something*, so a quiet day for a given stock
legitimately produces no alerts. The media sources (Google/ET/Moneycontrol) fill
day-to-day coverage.

### Alert quality gates

Articles are stored for review, but Telegram alerts are sent only when all of
these are true:

- the event is not `other` or `classification_failed`
- the direction is `bullish` or `bearish`, not `neutral`
- confidence is at least `ALERT_CONFIDENCE_THRESHOLD`
- materiality is at least `MIN_MATERIALITY_SCORE`
- source quality is at least `MIN_SOURCE_QUALITY_FOR_ALERTS`

Source quality currently favors official exchange filings:

| Source | Quality |
|---|---:|
| NSE announcements | 1.00 |
| BSE announcements | 1.00 |
| Economic Times / Moneycontrol RSS | 0.75 |
| NewsAPI | 0.70 |
| Google News RSS | 0.55 |

### Why Groq + Ollama, not a raw Ollama REST call?

The sibling project `stock_selector` already has a proven, working LLM backend
pattern, so this project reuses it instead of inventing a new one: the `openai`
Python SDK pointed at Groq's OpenAI-compatible endpoint
(`https://api.groq.com/openai/v1`, model `llama-3.3-70b-versatile`, **free tier**)
as the primary backend, with local Ollama (model `qwen3:8b`, also
OpenAI-compatible) as an optional fallback if Groq is unavailable or unconfigured.
`INFERENCE_BACKEND` in `.env` picks which is tried first.

### How failures are handled

Every stage fails soft and logs instead of crashing the pipeline:

- A news source that errors or times out returns an empty list for that source;
  the other sources still run.
- If neither LLM backend is reachable, that's logged **once per cycle** (not once
  per article) and every article that cycle is stored with
  `event_type = "classification_failed"` and excluded from alerts.
- If the LLM responds but the output isn't valid JSON matching the schema, the
  classifier retries once with a stricter "JSON only" instruction. If that also
  fails, the article is stored as `classification_failed` and skipped.
- A Telegram send failure is logged; the article's `alert_sent` flag is **not**
  set, so it's naturally retried as new the next time it's picked up (though in
  practice it won't be re-fetched once already stored — see "Known limitations").
- Articles classified as `other`, `classification_failed`, low-materiality, low
  source-quality, or `neutral` are stored but never trigger an alert.

## Setup

### 1. Install dependencies

This project uses its own virtual environment (`nenv/`) so it never touches your
system or other projects' Python packages:

```bash
cd stock-news-alerts
python3 -m venv nenv
nenv/bin/pip install -r requirements.txt
```

All commands below assume you run them with `nenv/bin/python`, or activate the
venv first with `source nenv/bin/activate` and use plain `python`.

### 2. (Optional) Install Ollama for a local LLM fallback

Groq's free tier is the default and requires no local install. If you also want
a fully-offline fallback:

```bash
# see https://ollama.com for install instructions
ollama pull qwen3:8b
```

Leave `OLLAMA_MODEL=qwen3:8b` in `.env` as-is if you do this; it's only used when
`INFERENCE_BACKEND=ollama` or when Groq fails.

### 3. Get a free Groq API key

Sign up at [console.groq.com](https://console.groq.com), create an API key, and
put it in `.env` as `GROQ_API_KEY`.

### 4. Create a Telegram bot and get your chat ID

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, and
   follow the prompts. You'll get a bot token.
2. Send any message to your new bot.
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and
   find `"chat":{"id": ...}` in the response — that's your chat ID.
4. If you already run `stock_selector` with a Telegram bot configured, you can
   reuse the same `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` values — this project
   uses the identical env var names.

### 5. (Optional) Get a free NewsAPI key

Sign up at [newsapi.org](https://newsapi.org) for a free developer key (100
requests/day, 1-month lookback limit) and put it in `.env` as `NEWSAPI_API_KEY`.
The pipeline works without it — NSE announcements and Google News RSS need no
key at all — this just adds broader media coverage.

### 6. Configure `.env`

```bash
cp .env.example .env
# edit .env with your keys
```

### 7. Edit your watchlist

Edit `watchlist.yaml` — add/remove NSE/BSE tickers and their company names. The
company name is used as the search query for Google News / NewsAPI and to match
NSE filings. Add `bse_code` when you want BSE corporate announcements for that
stock.

### 8. Initialize the database

```bash
nenv/bin/python init_db.py
```

### 9. Smoke-test Telegram

```bash
nenv/bin/python -m src.alerting.telegram_bot
```

You should get a "👋 Hello from stock-news-alerts!" message in Telegram. If
`TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` aren't set, or `DRY_RUN=true`, it logs instead
of sending and does not mark alerts as delivered — useful for testing without
spamming your chat.

### 10. Run one pipeline cycle manually

```bash
nenv/bin/python -m src.pipeline --once
```

Check the logs for `fetched=... new=... classified=... alerted=...`. Set
`DRY_RUN=true` in `.env` first if you want alerts logged instead of sent while
you're testing.

### 11. Run the scheduler (continuous polling)

```bash
nenv/bin/python scheduler.py
```

Polls every `POLL_INTERVAL_MINUTES` (default 2), runs an immediate first cycle
on startup, sends a startup message, and — if `DAILY_SUMMARY_ENABLED=true` —
sends a daily summary at `DAILY_SUMMARY_HOUR` (IST). Stop with Ctrl+C.

### Running it 24/7 (without your laptop on)

You can't keep this alive on a laptop that turns off. Run it on a free always-on
server instead. **See [`deploy/DEPLOY.md`](deploy/DEPLOY.md)** for a step-by-step
guide to Oracle Cloud's "Always Free" VM in the **Mumbai** region — free forever,
and the Indian IP is what keeps NSE from blocking the market-wide feed (NSE blocks
most foreign/datacenter IPs). It includes a ready-made `systemd` service
(`deploy/stock-news-alerts.service`) so it auto-starts and auto-restarts.

## Configuration reference (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `INFERENCE_BACKEND` | `groq` | `groq` or `ollama` — which LLM backend to try first |
| `GROQ_API_KEY` | — | Free key from console.groq.com |
| `OLLAMA_URL` | `http://localhost:11434/v1` | Local Ollama's OpenAI-compatible endpoint |
| `OLLAMA_MODEL` | `qwen3:8b` | Ollama model name |
| `TELEGRAM_TOKEN` | — | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | — | Your chat ID |
| `TG_API_ID` / `TG_API_HASH` | — | Only for `fetch_telegram_history.py`; from my.telegram.org (reusable from stock_selector) |
| `NEWSAPI_API_KEY` | — | Optional; NewsAPI free dev key (watchlist mode only) |
| `NEWSAPI_DAILY_CAP` | `90` | Stop querying NewsAPI once this many requests are used today |
| `COVERAGE_MODE` | `market_wide` | `market_wide` = all NSE filings, high-impact filtered; `watchlist` = your tickers + media |
| `POLL_INTERVAL_MINUTES` | `2` | Scheduler polling interval (2 min ≈ near-real-time for the filing feed) |
| `ALERT_CONFIDENCE_THRESHOLD` | `0.70` | Minimum confidence to send an alert (overrides `confidence_table.yaml`'s `alert_threshold` if set) |
| `MIN_MATERIALITY_SCORE` | `0.65` | Minimum LLM-estimated materiality for Telegram alerts |
| `MIN_SOURCE_QUALITY_FOR_ALERTS` | `0.55` | Minimum source credibility score for Telegram alerts |
| `MAX_ARTICLES_PER_CYCLE` | `18` | Max articles to classify per run, chosen round-robin across tickers so every stock gets covered. Sized to fit Groq's free-tier tokens/minute — raising it risks mid-cycle rate-limiting |
| `MAX_GOOGLE_NEWS_PER_TICKER` | `8` | Maximum (freshest) Google News RSS items to keep per ticker per run |
| `MAX_NEWS_AGE_HOURS` | `48` | Ignore fetched and pending-alert articles older than this; also sets the Google `when:Nd` window |
| `DAILY_SUMMARY_ENABLED` | `true` | Send a daily "N processed, M alerted" summary |
| `DAILY_SUMMARY_HOUR` | `18` | Hour (IST, 24h) to send the daily summary |
| `DB_PATH` | `stock_news.db` | SQLite file path |
| `DRY_RUN` | `false` | Log alerts instead of sending them to Telegram |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Running tests

```bash
nenv/bin/python -m pytest tests/ -v
```

Tests cover the two most fragile parts, with no network access required:

- `tests/test_classification.py` — JSON parsing/validation from LLM output,
  markdown-fence and `<think>`-block stripping, the retry-once-then-fail flow,
  and backend-unreachable handling.
- `tests/test_confidence.py` — base-rate lookup per event type, the magnitude
  nudge, materiality nudge, caps, and confidence clamping.
- `tests/test_source_quality.py` — source credibility scoring and the final
  directional/material alert gate.
- `tests/test_materiality_filter.py` — the pre-LLM materiality prefilter (media).
- `tests/test_impact.py` — the exchange-category → impact-tier gate (filings).

## Reading back sent alerts (`fetch_telegram_history.py`)

To verify what the bot actually delivered — the same capability as
`stock_selector`'s history fetcher — you can pull the bot's message history into
`telegram_history.json`:

```bash
nenv/bin/pip install telethon        # already in requirements.txt
nenv/bin/python fetch_telegram_history.py
```

- Needs `TG_API_ID` / `TG_API_HASH` in `.env` (from
  [my.telegram.org](https://my.telegram.org) → API development tools; the same
  values as stock_selector work).
- The bot's numeric id is derived automatically from `TELEGRAM_TOKEN`, so there's
  nothing else to configure.
- **First run is interactive**: Telethon asks for your phone number and the login
  code Telegram sends, then saves `tg_session.session` so later runs are
  non-interactive and incremental (only new messages).

## Known limitations

- Once an article is stored (even as `classification_failed`, e.g. because no
  LLM backend was reachable that cycle), it will never be re-classified — the
  dedupe check is by URL/headline, not by classification status. If you run the
  pipeline with no working LLM backend for a while, those articles are
  effectively lost to alerting. Re-running classification on
  `classification_failed` rows is a reasonable future addition, not built here.
- The NSE/BSE announcements endpoints are unofficial and unauthenticated from the
  exchanges' perspective; they may start requiring different headers/cookies or
  block scripted access entirely without warning.
- Google News RSS descriptions are often short/truncated, which can limit how
  much detail (e.g. exact EPS beat %) the LLM has to extract a `magnitude_pct`
  from.
- The system still does not backtest alerts against future price/volume moves.
  That is the next big step before treating confidence as calibrated.
