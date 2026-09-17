"""
LLM classification: event type, predicted direction, reasoning, and any
quantifiable detail (e.g. EPS beat %) for a single news article.

Three free `openai`-SDK-compatible backends, tried in order (see
_caller_chain): Groq (1000 requests/day, capped at 8000 tokens/min — the default
primary, because volume is what this pipeline is short of), Gemini
(frontier-class but only 20 requests/day on the free tier), and local Ollama
(fully offline fallback). INFERENCE_BACKEND in .env can reorder which leads.

Backend selection here is the throughput ceiling of the whole system, not an
implementation detail: while Gemini led and the Groq fallback was pointed at a
decommissioned model, 5223 of 6820 ingested filings (77%) were never classified.

Free models vary in how reliably they follow strict JSON, so responses are
validated with pydantic and, on a parse failure, retried once with a stricter
"JSON only" follow-up. If that also fails, the article is treated as
classification_failed by the caller (pipeline.py) — never crashes the pipeline.
"""

from __future__ import annotations

import json
import logging
import re
import time

from pydantic import ValidationError

from src.classification.schema import ClassificationResult
from src.config import get_settings
from src.ingestion.common import RawArticle

logger = logging.getLogger(__name__)

_GROQ_BASE = "https://api.groq.com/openai/v1"
# Verified live 2026-09-17 against Groq's /models: the previous model here
# (llama-3.3-70b-versatile) had been decommissioned and every call returned
# 404 model_not_found. Because that failure was logged at DEBUG while the run
# logs are at INFO, the fallback looked configured while doing nothing — so
# once Gemini's daily quota ran out there was no backend at all and every
# remaining article of the day was burned as classification_failed. gpt-oss-120b
# is the replacement: 131K context, free tier, and it parses our strict-JSON
# prompt on the first attempt (tested on results/demise/downgrade filings).
_GROQ_MODEL = "openai/gpt-oss-120b"

# Gemini free tier via its OpenAI-compatible endpoint: frontier-class accuracy at
# zero cost, but the free-tier quota is TWENTY REQUESTS PER DAY for this model —
# read verbatim from its own 429 on 2026-09-17:
#   Quota exceeded for metric: generate_content_free_tier_requests,
#   limit: 20, model: gemini-3.5-flash
#   quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier
# That single number is the reason 77% of ingested filings were never classified:
# the pipeline could read 20 filings a day against 150-230 arriving, the quota
# reset at midnight Pacific (= the unexplained 07:00 UTC recovery in the failure
# histogram), and the Groq fallback was 404ing so nothing caught the overflow.
# Gemini is therefore now the SECOND backend — a small daily allowance of
# best-quality reads — and Groq, at 1000 requests/day, carries the volume.
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
_GEMINI_MODEL = "gemini-3.5-flash"
# response_format isn't documented for Gemini's OpenAI-compat layer, so (like
# Ollama) it relies on the strict prompt + the existing parse-retry-once logic
# rather than a JSON-mode kwarg that might be silently ignored or rejected.

# Output is a small JSON object (~80 tokens); 200 is plenty of headroom. Kept low
# because Groq's free tier caps tokens-per-minute (input+output), and a smaller
# per-call token cost = more articles classified before hitting that ceiling.
_MAX_TOKENS = 200
# gpt-oss is a reasoning model and its thinking tokens count against max_tokens
# even at reasoning_effort="low". Verified live: at 200 the budget is consumed
# before the JSON starts and Groq rejects the call outright with
# json_validate_failed / "max completion tokens reached". 900 leaves room.
_GROQ_MAX_TOKENS = 900

# Groq free-tier limits, read live from its own response headers 2026-09-17:
# 1000 requests/day but only 8000 TOKENS/minute. Tokens are what binds — one
# filing costs ~1.2K tokens, or ~2.4K once a 3500-char PDF body is attached — so
# a flat "20 calls/min" throttle (what used to be here) was ~3x over the real
# ceiling and earned a genuine 429. Track observed usage in a rolling minute
# instead, so short filings are not paced as if every one carried a PDF.
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_GROQ_TPM_BUDGET = 7000
# Charged against the budget before a call, then replaced by what Groq actually
# billed. Observed live: ~1.2K tokens for a short filing, ~2.4K with a full
# 3500-char PDF body attached.
_GROQ_ASSUMED_TOKENS = 2000
# Budget frees up over a rolling minute, so a wait can legitimately approach
# that. Past this the cycle is better off handing the filing back to the retry
# queue than blocking on it.
_GROQ_MAX_WAIT_SECONDS = 45.0
# Primary pacing. 7000 tokens/min at ~1.6K a filing is ~4.4 calls/min, so
# spacing calls ~14s apart spends the budget evenly instead of firing a burst
# and then stalling for the rest of the minute — which is what a pure rolling
# window does on its own, and it starves the back half of every cycle.
_GROQ_MIN_INTERVAL_SECONDS = 14.0
_last_groq_call_at = 0.0
_groq_token_window: list[tuple[float, int]] = []

# Gemini free-tier RPM (~10/min): space calls at least this far apart so a full
# cycle's worth of classifications never trips a 429. Simple min-interval
# throttle rather than a sliding window — Gemini's quota resets fast enough
# that "one call every ~7s" alone keeps us comfortably under.
_GEMINI_MIN_INTERVAL_SECONDS = 6.5
_last_gemini_call_at = 0.0

# Logged at most once per pipeline cycle — reset via reset_cycle_state().
_logged_unreachable_this_cycle = False
_logged_rate_limited_this_cycle: set[str] = set()
_rate_limited_backends: set[str] = set()
# Backends that failed for a reason that will not resolve before the next call
# (wrong model name, revoked key, malformed request). Skipped for the rest of
# the cycle instead of being retried once per article.
_dead_backends: set[str] = set()
_logged_error_this_cycle: set[str] = set()
_DEAD_STATUS_CODES = frozenset({400, 401, 403, 404})

# Why the current article's classification failed, per backend. Reset at the
# start of every classify() call and read back by the pipeline so the reason
# lands in the DB rather than only in a log that scrolls away.
_last_errors: dict[str, str] = {}

_SYSTEM_PROMPT = """You read an Indian-stock exchange filing (or news item) and classify it. The text may be extracted from a filing PDF, so ignore letterhead/addresses/boilerplate and focus on the substance. Return ONLY a JSON object, no other text.

UNITS — READ THIS BEFORE QUOTING ANY MONEY FIGURE. Indian results tables state their unit in a header line such as "(Rs. in million)", "(Rs. in lakh)", "(Rs. in crore)" or "(Amount in Rs. '000)". That unit applies to EVERY number in the table. Find it and convert before writing any figure:
  1 crore = 10 million = 100 lakh = 10,000 thousand
So "Profit 9,751.2" under "(Rs. in million)" is Rs 975.1 cr, NOT Rs 9,751.2 cr. Under "(Rs. in lakh)" the same digits are Rs 97.5 cr.
ALWAYS express money in crore as "Rs <n> cr", whatever unit the filing used. Never copy the digits across unconverted, and never relabel a figure's unit without converting it. If no unit header is stated, use the figure as printed and do not guess a multiplier. Percentages, ratios and per-share amounts are never converted.

headline: a clean, factual one-line summary of what actually happened, with the key number if present (e.g. "Reports FY26 net profit up 29% to Rs 236 cr", "Wins Rs 5,000 cr order from NHAI", "Board recommends Rs 229 final dividend"). If the filing is purely procedural (newspaper notice, AGM intimation, trading-window closure, compliance certificate) say so plainly (e.g. "Routine AGM notice, no financial detail").
event_type (pick ONE): earnings_surprise (results beat/miss), guidance_change (company revises outlook), ma_deal (merger/acquisition/stake sale), analyst_rating (rating/target change), regulatory_legal (regulator/investigation/litigation/penalty), insider_activity (promoter/insider buy/sell), partnership_contract (order win/partnership), macro_sector (sector/macro), other.
direction: bullish | bearish | neutral (likely short-term price impact).
reason: ONE sentence citing a specific fact from the filing.
magnitude_pct: number if stated (e.g. "profit up 29%" -> 29.0), else null. Never invent one.
materiality_score: 0.0-1.0 — is this genuinely stock-moving for THIS company? HIGH for real results/orders/M&A/penalties/rating/buyback/dividend. LOW (below 0.3) for procedural notices (AGM/newspaper/trading-window/compliance-certificate/record-date) that carry no new financial fact.
impact_horizon: intraday | 1_3_days | swing | long_term | unknown.

Example: {"headline":"Reports FY26 net profit up 29% to Rs 236 cr","event_type":"earnings_surprise","direction":"bullish","reason":"FY26 net profit rose 29% YoY to Rs 236 cr.","magnitude_pct":29.0,"materiality_score":0.88,"impact_horizon":"1_3_days"}

Unit-conversion example — filing says "(Rs. in million)" and "Profit for the period 9,751.2" vs "6,592.3" prior year, so 9,751.2 million = Rs 975.1 cr:
{"headline":"Reports Q1 net profit up 48% YoY to Rs 975.1 cr","event_type":"earnings_surprise","direction":"bullish","reason":"Q1 net profit rose 48% YoY to Rs 975.1 cr from Rs 659.2 cr.","magnitude_pct":48.0,"materiality_score":0.9,"impact_horizon":"1_3_days"}
"""

_STRICT_SUFFIX = (
    "\n\nYou must return ONLY valid JSON matching the schema above. "
    "No markdown fences, no preamble, no explanation — JSON only."
)


def reset_cycle_state() -> None:
    """Call at the start of each pipeline cycle so backend-unreachable/rate-limit
    warnings are logged at most once per cycle instead of once per article."""
    global _logged_unreachable_this_cycle, _logged_rate_limited_this_cycle, _rate_limited_backends
    global _dead_backends, _logged_error_this_cycle
    _logged_unreachable_this_cycle = False
    _logged_rate_limited_this_cycle = set()
    _rate_limited_backends = set()
    _dead_backends = set()
    _logged_error_this_cycle = set()


def is_rate_limited() -> bool:
    """True only when EVERY configured cloud backend is unusable for the rest of
    this cycle — rate-limited (429) or dead (bad model/key). Ollama, local and
    fail-fast, isn't counted: trying it costs nothing.

    The pipeline uses this to stop early. It matters that dead backends count:
    while Groq was 404ing this never fired, so every article of a quota-exhausted
    cycle was still attempted and permanently stored as classification_failed."""
    settings = get_settings()
    cloud_backends = [
        name
        for name, key in (("gemini", settings.gemini_api_key), ("groq", settings.groq_api_key))
        if key
    ]
    if not cloud_backends:
        return False
    return all(
        name in _rate_limited_backends or name in _dead_backends for name in cloud_backends
    )


def classification_failure_reason() -> str:
    """Why the last classify() call failed, short enough to store on the row."""
    if not _last_errors:
        return "LLM classification failed or backend unreachable."
    return "; ".join(f"{name}: {why}" for name, why in _last_errors.items())[:300]


def _mark_rate_limited(name: str, reason: str = "429 rate limited") -> None:
    global _logged_rate_limited_this_cycle
    _rate_limited_backends.add(name)
    _last_errors[name] = reason
    if name not in _logged_rate_limited_this_cycle:
        logger.warning("classifier: %s rate limit reached this cycle (%s)", name, reason)
        _logged_rate_limited_this_cycle.add(name)


def _note_backend_error(name: str, reason: str, dead: bool) -> None:
    """Record why a backend call failed, and surface it at WARNING once per
    cycle. Previously this was a DEBUG line, which is how a decommissioned Groq
    model stayed invisible in INFO-level Actions logs."""
    _last_errors[name] = reason
    if dead:
        _dead_backends.add(name)
    if name not in _logged_error_this_cycle:
        logger.warning(
            "classifier: %s backend %s this cycle: %s",
            name,
            "unusable" if dead else "call failed",
            reason,
        )
        _logged_error_this_cycle.add(name)


def _groq_tokens_used() -> int:
    """Tokens Groq has billed us in the last minute, pruning what has aged out."""
    global _groq_token_window
    cutoff = time.monotonic() - _RATE_LIMIT_WINDOW_SECONDS
    _groq_token_window = [(t, n) for t, n in _groq_token_window if t > cutoff]
    return sum(n for _, n in _groq_token_window)


def _throttle_groq() -> bool:
    """Wait until this call fits inside Groq's tokens-per-minute budget.

    Sleeping is the point: at ~1.6K tokens a filing, 7000 tokens/min is about
    4 filings a minute, and pacing to that is what keeps a cycle's worth of
    classifications from earning a real 429. False means even waiting would not
    free enough budget soon — the filing is then left unstored and re-offered
    next cycle rather than burning an attempt."""
    global _last_groq_call_at
    spacing = _GROQ_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_groq_call_at)
    if spacing > 0:
        time.sleep(spacing)
    _last_groq_call_at = time.monotonic()

    used = _groq_tokens_used()
    need = used + _GROQ_ASSUMED_TOKENS - _GROQ_TPM_BUDGET
    if need > 0:
        now = time.monotonic()
        # Budget comes back as individual calls age out of the rolling minute,
        # so wait for however many of the oldest it takes to cover `need` — not
        # just the single oldest, which may not free enough on its own.
        freed = 0
        wait = None
        for timestamp, tokens in sorted(_groq_token_window):
            freed += tokens
            if freed >= need:
                wait = timestamp + _RATE_LIMIT_WINDOW_SECONDS - now
                break
        if wait is None or wait > _GROQ_MAX_WAIT_SECONDS:
            # Our own pacing, not Groq's 429 — worth telling apart in the stored
            # classification_error, because the fixes differ.
            _mark_rate_limited(
                "groq", f"local throttle: {used} tokens used in the last minute"
            )
            return False
        if wait > 0:
            time.sleep(wait)
        _groq_tokens_used()
    _groq_token_window.append((time.monotonic(), _GROQ_ASSUMED_TOKENS))
    return True


def _record_groq_usage(tokens: int | None) -> None:
    """Replace this call's assumed cost with what Groq actually billed."""
    if not tokens or not _groq_token_window:
        return
    timestamp, _ = _groq_token_window[-1]
    _groq_token_window[-1] = (timestamp, int(tokens))


def _throttle_gemini() -> None:
    """Sleep just enough to keep calls spaced under the free-tier RPM. Unlike
    Groq's reject-and-skip, this blocks briefly — Gemini's per-call cost in
    wait time is small and the quota resets fast, so waiting beats skipping."""
    global _last_gemini_call_at
    now = time.monotonic()
    wait = _GEMINI_MIN_INTERVAL_SECONDS - (now - _last_gemini_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_gemini_call_at = time.monotonic()


def _build_user_message(article: RawArticle) -> str:
    lines = [f"Ticker: {article.ticker}"]
    if article.category:
        lines.append(f"Filing category: {article.category}")
    lines.append(f"Title: {article.headline}")
    # The PDF body is the real substance when present; fall back to the short
    # summary/category for media items or unextractable filings.
    if article.body:
        lines.append(f"Filing content:\n{article.body}")
    else:
        lines.append(f"Summary: {article.summary or '(none)'}")
    return "\n".join(lines)


def _strip_wrapping(raw: str) -> str:
    """Strip <think>...</think> blocks (qwen3 emits these) and markdown fences."""
    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if m:
        raw = m.group(1)
    return raw.strip()


def _try_parse(raw: str) -> ClassificationResult | None:
    cleaned = _strip_wrapping(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    try:
        return ClassificationResult.model_validate(data)
    except ValidationError:
        return None


def _call_backend(name: str, base_url: str, model: str, api_key: str, messages: list[dict]) -> str | None:
    try:
        from openai import OpenAI
    except ImportError:
        return None

    try:
        if name == "groq":
            if not _throttle_groq():
                return None
        elif name == "gemini":
            _throttle_gemini()
        # max_retries=0: the SDK's own exponential-backoff retries would stack
        # with our retry-once-on-parse-failure logic and the rate throttles
        # above, turning a single invalid key or down backend into a
        # multi-minute hang. timeout keeps a genuinely hung connection (e.g.
        # Ollama installed but wedged) from blocking the whole cycle.
        client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=45.0)
        kwargs = {}
        max_tokens = _MAX_TOKENS
        if name == "groq":
            kwargs["response_format"] = {"type": "json_object"}
            # gpt-oss thinks before answering; "low" keeps that short enough to
            # fit the JSON inside _GROQ_MAX_TOKENS. Classification needs no
            # chain-of-thought, but unlike Gemini this backend has no "none".
            kwargs["reasoning_effort"] = "low"
            max_tokens = _GROQ_MAX_TOKENS
        elif name == "gemini":
            # gemini-3.5-flash is a thinking model and its reasoning tokens count
            # against max_tokens — without this the 200-token budget is consumed
            # by thoughts and the JSON comes back truncated (verified live: bare
            # "{"). Classification needs no chain-of-thought; disable it.
            kwargs["reasoning_effort"] = "none"
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            messages=messages,
            **kwargs,
        )
        if name == "groq":
            _record_groq_usage(getattr(getattr(resp, "usage", None), "total_tokens", None))
        return resp.choices[0].message.content
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            _mark_rate_limited(name)
            return None
        reason = f"{type(exc).__name__}"
        if status_code:
            reason += f" {status_code}"
        reason += f": {str(exc)[:160]}"
        _note_backend_error(name, reason, dead=status_code in _DEAD_STATUS_CODES)
        return None


def _caller_chain(settings) -> list[tuple[str, str, str, str]]:
    """Ordered (name, base_url, model, api_key) backends to try.

    Groq leads by default. It is not the better model — Gemini is — but Gemini's
    free tier allows 20 requests/day against Groq's 1000, and a filing nobody
    reads is worth less than a filing read by the second-best model. Set
    INFERENCE_BACKEND=gemini to put quality first on a paid key, or =ollama for
    a fully offline setup."""
    gemini = ("gemini", _GEMINI_BASE, _GEMINI_MODEL, settings.gemini_api_key)
    groq = ("groq", _GROQ_BASE, _GROQ_MODEL, settings.groq_api_key)
    ollama = ("ollama", settings.ollama_url, settings.ollama_model, "ollama")

    if settings.inference_backend == "ollama":
        return [ollama, groq, gemini]
    if settings.inference_backend == "gemini":
        return [gemini, groq, ollama]
    return [groq, gemini, ollama]


def _call_llm(system_prompt: str, user_msg: str) -> str | None:
    settings = get_settings()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    for name, base_url, model, api_key in _caller_chain(settings):
        if name in _rate_limited_backends or name in _dead_backends:
            continue
        if name in ("gemini", "groq") and not api_key:
            continue
        raw = _call_backend(name, base_url, model, api_key, messages)
        if raw is not None:
            return raw

    global _logged_unreachable_this_cycle
    if not _logged_unreachable_this_cycle:
        logger.error(
            "classifier: no LLM backend reachable (Gemini/Groq keys missing/failed "
            "and Ollama not running?) — skipping classification for this cycle"
        )
        _logged_unreachable_this_cycle = True
    return None


def classify(article: RawArticle) -> ClassificationResult | None:
    """Classify one article. Returns None if the backend is unreachable or the
    model's output fails validation twice (caller should store the article as
    classification_failed rather than crash)."""
    user_msg = _build_user_message(article)
    _last_errors.clear()

    raw = _call_llm(_SYSTEM_PROMPT, user_msg)
    if raw is None:
        return None

    result = _try_parse(raw)
    if result is not None:
        return result

    raw_retry = _call_llm(_SYSTEM_PROMPT, user_msg + _STRICT_SUFFIX)
    if raw_retry is None:
        return None

    result = _try_parse(raw_retry)
    if result is not None:
        return result

    _last_errors["parse"] = f"invalid JSON after strict retry: {(raw_retry or '')[:80]!r}"
    logger.warning(
        "classifier: classification_failed for %r — raw=%r retry_raw=%r",
        article.headline[:120],
        raw[:300],
        raw_retry[:300],
    )
    return None
