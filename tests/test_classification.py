"""Tests for LLM output parsing/validation and retry logic — the most fragile
part of the pipeline, per the project README's stated trade-off (local/free
models are less reliable at strict JSON than hosted frontier APIs)."""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from src.classification import classifier
from src.ingestion.common import RawArticle
from datetime import datetime, timezone

VALID_JSON = (
    '{"event_type": "earnings_surprise", "direction": "bullish", '
    '"reason": "EPS beat consensus by 12%", "magnitude_pct": 12.0, '
    '"materiality_score": 0.88, "impact_horizon": "1_3_days"}'
)


@pytest.fixture
def no_groq_spacing(monkeypatch):
    """Drop the inter-call pacing so throttle tests don't really sleep."""
    monkeypatch.setattr(classifier, "_GROQ_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(classifier, "_last_groq_call_at", 0.0)


def _fake_settings():
    return types.SimpleNamespace(
        inference_backend="ollama",
        gemini_api_key="",
        groq_api_key="",
        ollama_url="http://localhost:11434/v1",
        ollama_model="qwen3:8b",
    )


def _article() -> RawArticle:
    return RawArticle(
        ticker="RELIANCE.NS",
        headline="Reliance Q1 profit beats estimates by 12%",
        summary="EPS beat consensus, revenue also ahead",
        url="https://example.com/article",
        source="nse_announcements",
        published_at=datetime.now(timezone.utc),
    )


def setup_function(_):
    classifier.reset_cycle_state()


def test_valid_json_parses():
    with patch.object(classifier, "get_settings", _fake_settings), \
         patch.object(classifier, "_call_backend", return_value=VALID_JSON) as mock_call:
        result = classifier.classify(_article())

    assert result is not None
    assert result.event_type == "earnings_surprise"
    assert result.direction == "bullish"
    assert result.magnitude_pct == 12.0
    assert result.materiality_score == 0.88
    assert result.impact_horizon == "1_3_days"
    assert mock_call.call_count == 1


def test_json_in_markdown_fences_parses():
    wrapped = f"```json\n{VALID_JSON}\n```"
    with patch.object(classifier, "get_settings", _fake_settings), \
         patch.object(classifier, "_call_backend", return_value=wrapped):
        result = classifier.classify(_article())

    assert result is not None
    assert result.event_type == "earnings_surprise"


def test_think_block_stripped():
    wrapped = f"<think>reasoning about the article...</think>{VALID_JSON}"
    with patch.object(classifier, "get_settings", _fake_settings), \
         patch.object(classifier, "_call_backend", return_value=wrapped):
        result = classifier.classify(_article())

    assert result is not None
    assert result.event_type == "earnings_surprise"


def test_retry_recovers_from_junk_first_response():
    with patch.object(classifier, "get_settings", _fake_settings), \
         patch.object(
             classifier,
             "_call_backend",
             side_effect=["this is not json at all", VALID_JSON],
         ) as mock_call:
        result = classifier.classify(_article())

    assert result is not None
    assert result.event_type == "earnings_surprise"
    assert mock_call.call_count == 2


def test_returns_none_after_two_failures():
    with patch.object(classifier, "get_settings", _fake_settings), \
         patch.object(
             classifier,
             "_call_backend",
             side_effect=["junk one", "junk two"],
         ) as mock_call:
        result = classifier.classify(_article())

    assert result is None
    assert mock_call.call_count == 2


def test_invalid_event_type_rejected_then_retried():
    invalid = (
        '{"event_type": "not_a_real_type", "direction": "bullish", '
        '"reason": "x", "magnitude_pct": null}'
    )
    with patch.object(classifier, "get_settings", _fake_settings), \
         patch.object(
             classifier,
             "_call_backend",
             side_effect=[invalid, VALID_JSON],
         ) as mock_call:
        result = classifier.classify(_article())

    assert result is not None
    assert result.event_type == "earnings_surprise"
    assert mock_call.call_count == 2


def test_invalid_materiality_rejected_then_retried():
    invalid = (
        '{"event_type": "earnings_surprise", "direction": "bullish", '
        '"reason": "x", "magnitude_pct": null, '
        '"materiality_score": 1.5, "impact_horizon": "1_3_days"}'
    )
    with patch.object(classifier, "get_settings", _fake_settings), \
         patch.object(
             classifier,
             "_call_backend",
             side_effect=[invalid, VALID_JSON],
         ) as mock_call:
        result = classifier.classify(_article())

    assert result is not None
    assert result.materiality_score == 0.88
    assert mock_call.call_count == 2


def test_backend_unreachable_returns_none_without_retry():
    with patch.object(classifier, "get_settings", _fake_settings), \
         patch.object(classifier, "_call_backend", return_value=None) as mock_call:
        result = classifier.classify(_article())

    assert result is None
    # Only one _call_llm invocation (the initial attempt) — no retry call is
    # made once the backend itself is unreachable, since there's no raw
    # output to retry parsing.
    assert mock_call.call_count == 1


def _settings(inference_backend="gemini", gemini_key="g-key", groq_key="q-key"):
    return types.SimpleNamespace(
        inference_backend=inference_backend,
        gemini_api_key=gemini_key,
        groq_api_key=groq_key,
        ollama_url="http://localhost:11434/v1",
        ollama_model="qwen3:8b",
    )


def test_caller_chain_defaults_to_groq_for_throughput():
    """Gemini classifies better but allows 20 requests/day on the free tier;
    Groq allows 1000. Both numbers read live from the providers on 2026-09-17.
    With Gemini leading, the pipeline could read 20 of the ~200 filings that
    arrive each day."""
    names = [c[0] for c in classifier._caller_chain(_settings(inference_backend=""))]
    assert names == ["groq", "gemini", "ollama"]


def test_caller_chain_gemini_backend_forces_gemini_first():
    names = [c[0] for c in classifier._caller_chain(_settings(inference_backend="gemini"))]
    assert names == ["gemini", "groq", "ollama"]


def test_caller_chain_groq_backend_forces_groq_first():
    names = [c[0] for c in classifier._caller_chain(_settings(inference_backend="groq"))]
    assert names == ["groq", "gemini", "ollama"]


def test_caller_chain_ollama_backend_forces_ollama_first():
    names = [c[0] for c in classifier._caller_chain(_settings(inference_backend="ollama"))]
    assert names == ["ollama", "groq", "gemini"]


# ── Groq's ceiling is tokens per minute, not calls per minute ────────────────


def test_groq_throttle_paces_on_observed_token_usage(no_groq_spacing):
    classifier.reset_cycle_state()
    classifier._groq_token_window.clear()
    assert classifier._throttle_groq() is True
    # A short filing costs far less than the assumed budget; recording the real
    # figure must free that headroom back up for the next call.
    classifier._record_groq_usage(900)
    assert classifier._groq_tokens_used() == 900
    classifier._groq_token_window.clear()


def test_groq_throttle_gives_up_rather_than_blocking_a_whole_cycle(no_groq_spacing):
    import time as _time

    classifier.reset_cycle_state()
    classifier._groq_token_window.clear()
    # A full minute's budget spent one second ago: waiting it out would stall
    # the cycle, so the filing goes back to the retry queue instead.
    classifier._groq_token_window.append(
        (_time.monotonic() - 1.0, classifier._GROQ_TPM_BUDGET)
    )
    assert classifier._throttle_groq() is False
    assert "groq" in classifier._rate_limited_backends
    assert "local throttle" in classifier._last_errors["groq"]
    classifier._groq_token_window.clear()
    classifier.reset_cycle_state()


def test_is_rate_limited_false_with_no_cloud_backends_configured():
    classifier.reset_cycle_state()
    with patch.object(classifier, "get_settings", lambda: _settings(gemini_key="", groq_key="")):
        assert classifier.is_rate_limited() is False


def test_is_rate_limited_only_true_when_all_configured_cloud_backends_limited():
    classifier.reset_cycle_state()
    with patch.object(classifier, "get_settings", lambda: _settings()):
        classifier._mark_rate_limited("gemini")
        assert classifier.is_rate_limited() is False  # groq still untried
        classifier._mark_rate_limited("groq")
        assert classifier.is_rate_limited() is True
    classifier.reset_cycle_state()


def test_reset_cycle_state_clears_rate_limit_tracking():
    classifier.reset_cycle_state()
    with patch.object(classifier, "get_settings", lambda: _settings()):
        classifier._mark_rate_limited("gemini")
        classifier._mark_rate_limited("groq")
        assert classifier.is_rate_limited() is True
        classifier.reset_cycle_state()
        assert classifier.is_rate_limited() is False


# ── unit-conversion guidance in the system prompt ────────────────────────────
# Nestle India's Q1 results alerted as "Rs 9,751.2 cr" when the true figure was
# Rs 975.1 cr: the filing reported in "(Rs. in million)" and the model copied
# the digits across while labelling them crore — a 10x overstatement. The prompt
# had no unit guidance at all, and every example used "Rs X cr", which actively
# pushed the model toward that label. These assert the guidance stays present;
# the conversion behaviour itself was verified against a live model across
# crore/lakh/million filings (all three resolve to Rs 975.1 cr).

def test_system_prompt_states_the_unit_conversion_factors():
    p = classifier._SYSTEM_PROMPT
    assert "1 crore = 10 million = 100 lakh" in p


def test_system_prompt_carries_the_million_worked_example():
    # The concrete 9,751.2 million -> Rs 975.1 cr case, so the model sees the
    # exact failure it got wrong rather than only an abstract rule.
    p = classifier._SYSTEM_PROMPT
    assert "9,751.2" in p and "975.1" in p


def test_system_prompt_requires_crore_output_and_forbids_bare_copying():
    p = classifier._SYSTEM_PROMPT.lower()
    assert "always express money in crore" in p
    assert "never copy the digits across unconverted" in p


# ── a broken backend must be loud, and must not be retried per article ───────
# The Groq fallback pointed at llama-3.3-70b-versatile long after Groq
# decommissioned it. Every call returned 404 model_not_found, and that was
# logged at DEBUG while production runs at INFO — so the fallback read as
# configured while doing nothing, and once Gemini's daily quota ran out there
# was no backend at all. Verified live 2026-09-17 against Groq's /models.


def test_a_404_marks_the_backend_dead_for_the_rest_of_the_cycle():
    classifier.reset_cycle_state()
    exc = RuntimeError("model_not_found")
    exc.status_code = 404
    with patch("openai.OpenAI", side_effect=exc):
        assert classifier._call_backend("groq", "u", "m", "k", []) is None
    assert "groq" in classifier._dead_backends
    classifier.reset_cycle_state()


def test_a_transient_error_does_not_mark_the_backend_dead():
    classifier.reset_cycle_state()
    with patch("openai.OpenAI", side_effect=TimeoutError("read timeout")):
        assert classifier._call_backend("gemini", "u", "m", "k", []) is None
    assert "gemini" not in classifier._dead_backends
    assert "gemini" in classifier._last_errors
    classifier.reset_cycle_state()


def test_dead_backends_count_toward_stopping_the_cycle():
    """is_rate_limited() drives the pipeline's early stop. While Groq 404'd it
    was never marked, so the pipeline kept feeding articles to a chain with no
    working backend and stored each one as a permanent classification_failed."""
    classifier.reset_cycle_state()
    with patch.object(classifier, "get_settings", lambda: _settings()):
        classifier._mark_rate_limited("gemini")
        assert classifier.is_rate_limited() is False
        classifier._note_backend_error("groq", "NotFoundError 404: model_not_found", dead=True)
        assert classifier.is_rate_limited() is True
    classifier.reset_cycle_state()


def test_failure_reason_names_every_backend_that_failed():
    classifier.reset_cycle_state()
    classifier._last_errors.clear()
    classifier._mark_rate_limited("gemini")
    classifier._note_backend_error("groq", "NotFoundError 404: model_not_found", dead=True)
    reason = classifier.classification_failure_reason()
    assert "gemini: 429" in reason and "groq: NotFoundError 404" in reason
    assert len(reason) <= 300
    classifier.reset_cycle_state()


def test_failure_reason_falls_back_to_a_plain_string_when_nothing_was_recorded():
    classifier.reset_cycle_state()
    classifier._last_errors.clear()
    assert classifier.classification_failure_reason().startswith("LLM classification failed")


def test_groq_gets_its_own_token_budget_for_thinking():
    # gpt-oss spends max_tokens on reasoning before the JSON starts; at the
    # shared 200 budget Groq rejects the call with json_validate_failed.
    assert classifier._GROQ_MAX_TOKENS > classifier._MAX_TOKENS
    assert "llama-3.3-70b-versatile" not in classifier._GROQ_MODEL


# ── negative catalysts need their own identity ───────────────────────────────
# Until 2026-09-17 the taxonomy had no member for a credit rating, a management
# change, an insolvency or a payment default, so the model filed them under
# whatever was nearest: insolvency/NCLT landed in regulatory_legal (22 of 43 as
# NEUTRAL) and resignation/demise in other (5 of 10 NEUTRAL). A neutral
# direction never alerts, so a CEO's death reached nobody. Behaviour verified
# live against gpt-oss-120b across eight filing shapes; these pin the prompt
# contract those depend on.


def test_negative_catalysts_have_their_own_event_types():
    from src.classification.schema import EventType
    from typing import get_args

    for event_type in ("credit_rating", "management_change", "insolvency", "default_payment"):
        assert event_type in get_args(EventType)


def test_prompt_separates_rating_agencies_from_brokers():
    # 43 of 46 delivered analyst_rating alerts were rating-agency filings.
    p = classifier._SYSTEM_PROMPT
    assert "CRISIL/ICRA/CARE" in p
    assert "not a debt rating agency" in p


def test_prompt_treats_a_reaffirmation_as_a_non_event():
    # The bucket mixed downgrades with reaffirmations (49 neutral / 43 bullish /
    # 8 bearish); a reaffirmation alerted as bullish is a non-event traded as
    # good news.
    p = classifier._SYSTEM_PROMPT
    assert "REAFFIRMATION at the same rating and outlook is a non-event" in p
    assert "materiality below 0.3" in p


def test_prompt_names_demise_as_bearish_and_orderly_succession_as_not():
    p = classifier._SYSTEM_PROMPT
    assert "Demise, or abrupt resignation, of a promoter/MD/CEO/CFO/Chairman -> bearish" in p
    assert "named successor" in p


def test_prompt_tells_the_model_not_to_reach_for_neutral():
    assert "Do NOT reach for neutral on a genuine catalyst" in classifier._SYSTEM_PROMPT
