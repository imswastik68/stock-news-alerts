"""Tests for the exchange-category -> impact-tier filter — the core high-signal
gate that keeps only stock-moving filings and drops procedural compliance noise."""

from __future__ import annotations

from src.scoring.impact import DROP, HIGH, MEDIUM, category_impact, is_high_impact_category


def test_high_impact_catalyst_categories():
    for cat in [
        "Awarding of order(s)/contract(s)",
        "Acquisition",
        "Financial Results",
        "Raising of Funds",
        "Credit Rating",
        "Buyback",
        "Dividend",
        "Penalty/Fine imposed by regulator",
    ]:
        assert category_impact(cat) == HIGH, cat
        assert is_high_impact_category(cat)


def test_procedural_categories_dropped():
    for cat in [
        "Certificate under SEBI (Depositories and Participants) Regulations, 2018",
        "Closure of Trading Window",
        "Newspaper Publication",
        "Compliance Certificate under Regulation 7(3)",
        "Reconciliation of Share Capital Audit Report",
        "Statement of Investor Complaints",
    ]:
        assert category_impact(cat) == DROP, cat


def test_medium_categories():
    for cat in [
        "Change in Directors (non-KMP appointment)",
        "Analysts/Institutional Investor Meet/Con. Call Updates",
        "Agreement",
    ]:
        assert category_impact(cat) == MEDIUM, cat


def test_catalyst_keyword_overrides_drop_marker():
    # "Notice of ... Buyback" contains a drop marker ("notice of") but the strong
    # catalyst keyword must win so a real buyback isn't discarded.
    assert category_impact("Notice of Buyback of Equity Shares") == HIGH


def test_unknown_category_defaults_to_medium_not_dropped():
    # Never silently drop an unrecognized category — let the LLM judge it.
    assert category_impact("Some Brand New Category Type") == MEDIUM


def test_empty_category_is_medium():
    assert category_impact("") == MEDIUM
