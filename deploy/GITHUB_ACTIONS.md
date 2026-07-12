# Running 24/7 free on GitHub Actions

No card, no server. GitHub runs `pipeline --once` on a schedule; the workflow is
already in `.github/workflows/news_scan.yml`.

## ⚠️ The one thing that decides if this works
GitHub's runners have **US datacenter IPs**, and NSE blocks many of those. Step 5
below is the make-or-break test — do it first, before trusting the schedule.

## ⚠️ Public vs private repo (this affects whether it's actually free)
- **Public repo → Actions minutes are UNLIMITED (free).** The 5-min cron is fine.
  Your code is visible, but there are **no secrets in the code** (`.env` is
  gitignored; keys live in encrypted GitHub Secrets). Recommended.
- **Private repo → only 2,000 free minutes/month** (~1.3 min/run ⇒ ~1,300 runs ⇒
  roughly one run every 30 min if you want to stay free). If you go private, widen
  the cron (e.g. `*/30 * * * *`).

## Steps

### 1. Put the project in a GitHub repo
```bash
cd stock-news-alerts
git init
git add .
git commit -m "stock-news-alerts"
gh repo create stock-news-alerts --public --source=. --push
# (or create the repo on github.com and: git remote add origin ... ; git push -u origin main)
```
`.env` and `*.db` are gitignored, so your keys are NOT uploaded. Good.

### 2. Add your secrets (encrypted, never in the code)
Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add three:

| Name | Value |
|---|---|
| `GROQ_API_KEY` | your Groq key |
| `TELEGRAM_TOKEN` | your bot token |
| `TELEGRAM_CHAT_ID` | your chat id |

(Or via CLI: `gh secret set GROQ_API_KEY`, etc.)

### 3. Enable Actions
Repo → **Actions** tab → enable workflows if prompted.

### 4. Trigger a manual test run
Repo → **Actions → "news-scan" → Run workflow** (this is the `workflow_dispatch`
button). Or CLI: `gh workflow run news-scan`.

### 5. MAKE-OR-BREAK: read the run log
Open the run → the **"Run one pipeline cycle"** step. Look for:

- ✅ `nse_announcements: N market-wide filing(s)` with **N > 0** → NSE works from
  GitHub's IP. You're done — the 5-min schedule takes over automatically.
- ❌ `N = 0` every time, or fetch errors/timeouts → **NSE is blocking GitHub's
  runner.** GitHub Actions won't work for `market_wide`; fall back to a home
  device (old phone/Raspberry Pi — residential Indian IP) or AWS Mumbai.

### 6. That's it
Once the manual test shows filings, the `schedule:` cron runs it every ~5 min
automatically. High-impact filings alert straight to Telegram.

## Notes
- Scheduled runs only fire from the **default branch**, and GitHub auto-disables
  schedules on repos with no activity for 60 days (a commit re-enables).
- Cron is best-effort — GitHub may delay runs during peak load. Expect
  "every 5–15 min," not exact.
- Dedup state (`stock_news.db`) is persisted between runs via `actions/cache`, so
  you won't get re-alerted for the same filing.
- To pause it: Actions tab → "news-scan" → "···" → Disable workflow.
