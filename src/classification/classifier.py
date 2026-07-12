"""
LLM classification: event type, predicted direction, reasoning, and any
quantifiable detail (e.g. EPS beat %) for a single news article.

Backend selection mirrors stock_selector's src/agent.py: the `openai` SDK
pointed at Groq's OpenAI-compatible endpoint (free tier, llama-3.3-70b-versatile)
as the primary backend, with local Ollama (qwen3:8b, also OpenAI-compatible) as
an optional fallback. INFERENCE_BACKEND in .env picks which one is tried first;
the other is tried if the first raises.

Local/free models are less reliable at strict JSON than hosted frontier APIs, so
responses are validated with pydantic and, on a parse failure, retried once with
a stricter "JSON only" follow-up. If that also fails, the article is treated as
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
_GROQ_MODEL = "llama-3.3-70b-versatile"

# Output is a small JSON object (~80 tokens); 200 is plenty of headroom. Kept low
# because Groq's free tier caps tokens-per-minute (input+output), and a smaller
# per-call token cost = more articles classified before hitting that ceiling.
_MAX_TOKENS = 200

# Groq free-tier rate limiting: throttle to <=20 classification calls/min.
_RATE_LIMIT_CALLS = 20
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_call_timestamps: list[float] = []

# Logged at most once per pipeline cycle — reset via reset_cycle_state().
_logged_unreachable_this_cycle = False
_logged_rate_limited_this_cycle = False
_rate_limited_this_cycle = False

_SYSTEM_PROMPT = """Classify one Indian-stock news item. Return ONLY a JSON object, no other text.

event_type (pick ONE): earnings_surprise (results beat/miss), guidance_change (company revises outlook), ma_deal (merger/acquisition/stake sale), analyst_rating (rating/target change), regulatory_legal (regulator/investigation/litigation), insider_activity (promoter/insider buy/sell), partnership_contract (order win/partnership), macro_sector (sector/macro), other.
direction: bullish | bearish | neutral (likely short-term price impact).
reason: ONE sentence citing a fact from the item.
magnitude_pct: number if stated (e.g. "beat by 12%" -> 12.0), else null. Never invent one.
materiality_score: 0.0-1.0 — is this genuinely stock-moving for THIS company? High for real earnings/orders/M&A/regulatory/insider/buyback/dividend/rating actions; low for routine filings (board-meeting/investor-meet intimations), generic market commentary, old/repeated news, or "stock moved today" pieces.
impact_horizon: intraday | 1_3_days | swing | long_term | unknown.

Example: {"event_type":"earnings_surprise","direction":"bullish","reason":"EPS beat consensus by 12%.","magnitude_pct":12.0,"materiality_score":0.86,"impact_horizon":"1_3_days"}
"""

_STRICT_SUFFIX = (
    "\n\nYou must return ONLY valid JSON matching the schema above. "
    "No markdown fences, no preamble, no explanation — JSON only."
)


def reset_cycle_state() -> None:
    """Call at the start of each pipeline cycle so backend-unreachable warnings
    are logged at most once per cycle instead of once per article."""
    global _logged_unreachable_this_cycle, _logged_rate_limited_this_cycle, _rate_limited_this_cycle
    _logged_unreachable_this_cycle = False
    _logged_rate_limited_this_cycle = False
    _rate_limited_this_cycle = False


def is_rate_limited() -> bool:
    return _rate_limited_this_cycle


def _mark_rate_limited() -> None:
    global _logged_rate_limited_this_cycle, _rate_limited_this_cycle
    _rate_limited_this_cycle = True
    if not _logged_rate_limited_this_cycle:
        logger.warning(
            "classifier: Groq rate limit reached; stopping new LLM classifications for this cycle"
        )
        _logged_rate_limited_this_cycle = True


def _throttle_groq() -> bool:
    now = time.monotonic()
    global _call_timestamps
    _call_timestamps = [t for t in _call_timestamps if now - t < _RATE_LIMIT_WINDOW_SECONDS]
    if len(_call_timestamps) >= _RATE_LIMIT_CALLS:
        _mark_rate_limited()
        return False
    _call_timestamps.append(time.monotonic())
    return True


def _build_user_message(article: RawArticle) -> str:
    return (
        f"Ticker: {article.ticker}\n"
        f"Headline: {article.headline}\n"
        f"Summary: {article.summary or '(none)'}"
    )


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
        # max_retries=0: the SDK's own exponential-backoff retries would stack
        # with our retry-once-on-parse-failure logic and the Groq rate
        # throttle below, turning a single invalid key or down backend into a
        # multi-minute hang. timeout keeps a genuinely hung connection (e.g.
        # Ollama installed but wedged) from blocking the whole cycle.
        client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=30.0)
        kwargs = {}
        if name == "groq":
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            messages=messages,
            **kwargs,
        )
        return resp.choices[0].message.content
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            _mark_rate_limited()
            return None
        logger.debug("classifier: %s backend call failed: %s", name, exc)
        return None


def _call_llm(system_prompt: str, user_msg: str) -> str | None:
    settings = get_settings()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    groq_caller = ("groq", _GROQ_BASE, _GROQ_MODEL, settings.groq_api_key)
    ollama_caller = ("ollama", settings.ollama_url, settings.ollama_model, "ollama")

    if settings.inference_backend == "ollama":
        callers = [ollama_caller, groq_caller]
    else:
        callers = [groq_caller, ollama_caller]

    for name, base_url, model, api_key in callers:
        if _rate_limited_this_cycle and name == "groq":
            continue
        if name == "groq" and not api_key:
            continue
        raw = _call_backend(name, base_url, model, api_key, messages)
        if raw is not None:
            return raw

    global _logged_unreachable_this_cycle
    if not _logged_unreachable_this_cycle:
        logger.error(
            "classifier: no LLM backend reachable (Groq key missing/failed and "
            "Ollama not running?) — skipping classification for this cycle"
        )
        _logged_unreachable_this_cycle = True
    return None


def classify(article: RawArticle) -> ClassificationResult | None:
    """Classify one article. Returns None if the backend is unreachable or the
    model's output fails validation twice (caller should store the article as
    classification_failed rather than crash)."""
    user_msg = _build_user_message(article)

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

    logger.warning(
        "classifier: classification_failed for %r — raw=%r retry_raw=%r",
        article.headline[:120],
        raw[:300],
        raw_retry[:300],
    )
    return None
