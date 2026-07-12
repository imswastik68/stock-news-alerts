"""
Impact-tier filter for exchange filings — the "seriously impact the stock" gate.

NSE/BSE tag every filing with a category (`desc`). Most categories are
procedural compliance noise ("Certificate under SEBI Regulations", "Trading
Window", "Newspaper Publication") that never move a price; a minority are
genuine catalysts (order wins, results, M&A, fund raising, rating changes,
KMP exits, penalties). This module maps a category string to one of:

  "high"   — strong catalyst; always worth an LLM read + likely an alert
  "medium" — possibly material; let the LLM judge
  "drop"   — procedural noise; never classified, never alerted

Matching is keyword-based (substring, case-insensitive) because the exact
category text varies. This is the single biggest lever for keeping only
high-probability, stock-moving news — it runs BEFORE the LLM, so procedural
filings never even cost a classification call.

Like the confidence table, these tiers are a documented heuristic, not a
backtested model.
"""

from __future__ import annotations

HIGH = "high"
MEDIUM = "medium"
DROP = "drop"

# Checked in order; first keyword found in the (lowercased) category wins.
_HIGH_KEYWORDS = [
    "award of order", "awarding of order", "order(s)", "contract",
    "acquisition", "acquire", "merger", "amalgamation", "demerger",
    "scheme of arrangement", "slump sale", "stake",
    "financial result", "quarterly result", "audited result", "unaudited result",
    "raising of funds", "fund raising", "fund-raising", "preferential",
    "qualified institution", "qip", "rights issue", "further public offer",
    "credit rating", "rating",
    "buyback", "buy back", "buy-back",
    "bonus", "stock split", "sub-division", "sub division",
    "dividend",
    "open offer", "delisting", "voluntary delisting",
    "one time settlement", "one-time settlement", "resolution plan",
    "insolvency", "nclt", "winding up", "liquidation",
    "penalty", "fine", "penal", "prosecution", "search", "raid", "freezing",
    "fraud", "default",
    "joint venture", "strategic", "memorandum of understanding",
    "investor presentation", "earnings call", "concall transcript",
    "fire", "force majeure", "disruption", "shutdown of",
    "managing director", "chief executive", "chief financial",
    "ceo", "cfo", "resignation of managing", "appointment of managing",
]

_MEDIUM_KEYWORDS = [
    "outcome of board meeting", "board meeting",
    "litigation", "dispute", "order impacting", "regulatory",
    "resignation", "appointment", "change in director", "cessation",
    "allotment", "esop", "esos", "esps", "warrant",
    "agreement", "supply", "expansion", "commencement", "commercial production",
    "capacity", "plant", "investment", "subsidiary", "wholly owned",
    "update", "clarification", "press release", "media release",
    "analyst", "investor meet", "institutional investor",
]

# Narrow on purpose: these categories are procedural REGARDLESS of PDF content, so
# we skip them before downloading/reading the attachment. Everything else — even
# generic-looking "Newspaper Publication" / "General Updates" / "Shareholders
# meeting" — is NOT dropped, because those routinely wrap the actual results,
# dividend, or rating inside the PDF (which the pipeline now reads). The LLM then
# assigns low materiality to the ones that turn out to be genuinely procedural.
_DROP_KEYWORDS = [
    "certificate under sebi (depositories", "regulation 74", "regulation 40(9)",
    "regulation 7(3)", "regulation 7 (3)",
    "reconciliation of share capital",
    "trading window", "closure of trading window",
    "compliance certificate", "compliance report",
    "investor complaint", "investor grievance", "grievance redressal",
    "loss of share", "duplicate share", "issue of duplicate",
    "postal ballot", "scrutinizer", "voting results",
    "sub-broker", "sub broker",
]


def category_impact(category: str) -> str:
    """Map an exchange category string to 'high' / 'medium' / 'drop'.
    Unknown categories default to 'medium' so nothing genuinely material is
    silently dropped just because it has an unrecognized label."""
    if not category:
        return MEDIUM
    text = category.lower()

    for kw in _DROP_KEYWORDS:
        if kw in text:
            # A drop keyword can still be overridden by a strong catalyst keyword
            # in the same category (e.g. "Notice of ... Buyback").
            if any(hk in text for hk in _HIGH_KEYWORDS):
                return HIGH
            return DROP
    for kw in _HIGH_KEYWORDS:
        if kw in text:
            return HIGH
    for kw in _MEDIUM_KEYWORDS:
        if kw in text:
            return MEDIUM
    return MEDIUM


def is_high_impact_category(category: str) -> bool:
    return category_impact(category) == HIGH
