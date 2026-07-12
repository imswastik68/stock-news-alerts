"""Tests for LLM output parsing/validation and retry logic — the most fragile
part of the pipeline, per the project README's stated trade-off (local/free
models are less reliable at strict JSON than hosted frontier APIs)."""

from __future__ import annotations

import types
from unittest.mock import patch

from src.classification import classifier
from src.ingestion.common import RawArticle
from datetime import datetime, timezone

VALID_JSON = (
    '{"event_type": "earnings_surprise", "direction": "bullish", '
    '"reason": "EPS beat consensus by 12%", "magnitude_pct": 12.0, '
    '"materiality_score": 0.88, "impact_horizon": "1_3_days"}'
)


def _fake_settings():
    return types.SimpleNamespace(
        inference_backend="ollama",
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
        source="google_news",
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
