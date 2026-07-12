"""
Download an exchange filing's PDF attachment and extract its text.

This is what lets the tool see the *content* of a filing (results numbers, order
value, rating, dividend) instead of just NSE's generic category tag — the same
thing the pro platforms do. Text-layer PDFs extract cleanly; scanned/image-only
PDFs yield nothing and are skipped (we fall back to the category). Fails soft:
any download/parse problem returns None, never raises.
"""

from __future__ import annotations

import io
import logging
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _headers_for(url: str) -> dict:
    # Referer set to the file's own origin so both NSE and BSE archive hosts
    # accept the request.
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}/" if parts.netloc else ""
    return {"User-Agent": _USER_AGENT, "Referer": origin}

_MAX_PDF_BYTES = 15 * 1024 * 1024  # skip anything larger — avoids slow downloads
_MAX_PAGES = 3                     # first pages carry the material content
_MAX_CHARS = 2000                  # cap fed to the LLM (~500 tokens; Groq TPM budget)
_TIMEOUT = 20


def _download(url: str, session: requests.Session | None) -> bytes | None:
    getter = session.get if session is not None else requests.get
    try:
        resp = getter(url, headers=_headers_for(url), timeout=_TIMEOUT, stream=True)
        resp.raise_for_status()
        chunks = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            chunks.extend(chunk)
            if len(chunks) > _MAX_PDF_BYTES:
                logger.debug("pdf_extract: %s exceeds size cap, skipping", url)
                return None
        return bytes(chunks)
    except Exception as exc:
        logger.debug("pdf_extract: download failed for %s: %s", url, exc)
        return None


def extract_pdf_text(url: str, session: requests.Session | None = None) -> str | None:
    """Return up to _MAX_CHARS of cleaned text from the first pages of the PDF at
    `url`, or None if it can't be downloaded/parsed or has no text layer."""
    if not url or not url.lower().startswith("http"):
        return None

    data = _download(url, session)
    if not data:
        return None

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for page in reader.pages[:_MAX_PAGES]:
            parts.append(page.extract_text() or "")
        text = " ".join(" ".join(parts).split())
    except Exception as exc:
        logger.debug("pdf_extract: parse failed for %s: %s", url, exc)
        return None

    if len(text) < 30:  # scanned/image-only PDF — no usable text layer
        return None
    return text[:_MAX_CHARS]
