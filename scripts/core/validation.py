"""
OCR plausibility validation — protection against LLM hallucination.

When the LLM returns well-formed JSON with wrong numbers (e.g., dropping a
digit from 1280.00 to 128.00), downstream matching appears to succeed and
the CSV ships wrong data to finance. This module cross-checks LLM output
against independent signals.

Checks:
  1. Amount vs pdftotext — extract all decimal amounts from PDF text; the
     LLM's `transactionAmount` should be within 10% of at least one of them.
  2. Date vs email — `transactionDate` must be within ±90 days of the
     email's `internalDate`. Outside that window is almost certainly wrong.

Failures set `_amountConfidence = "low"` or `_dateConfidence = "low"` on
the ocr dict rather than discarding — callers decide whether to route the
record to UNPARSED or surface a warning in the CSV.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional


# Regex: matches ¥1,234.56 / 1234.56 / ￥1,234 etc.
_AMOUNT_RE = re.compile(r"(?:[¥￥]|\brmb\s*)?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{1,2})")


def _extract_amounts_from_pdf(pdf_path: str) -> List[float]:
    """Run pdftotext and scan for decimal amounts. Returns deduped list."""
    if not shutil.which("pdftotext"):
        return []
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            return []
        text = result.stdout.decode("utf-8", errors="ignore")
    except (subprocess.TimeoutExpired, OSError):
        return []

    amounts: List[float] = []
    for match in _AMOUNT_RE.finditer(text):
        raw = match.group(1).replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        # Filter noise: tax IDs, invoice numbers, phone numbers
        if val < 0.01 or val > 10_000_000:
            continue
        # Skip integer amounts that look like IDs (no decimal point AND > 9999)
        if "." not in raw and val > 9999:
            continue
        amounts.append(val)
    return amounts


def _amount_matches_any(llm_amount: float, page_amounts: List[float], tol_pct: float = 0.10) -> bool:
    """True iff llm_amount is within tol_pct of any amount found on the page."""
    if not page_amounts:
        # No PDF signal — can't validate, benefit of the doubt
        return True
    for pa in page_amounts:
        if pa == 0:
            continue
        if abs(llm_amount - pa) / abs(pa) <= tol_pct:
            return True
    return False


def _parse_ocr_date(s: str) -> Optional[_dt.date]:
    """Parse YYYY-MM-DD. Returns None if unparseable."""
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s.strip())
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def validate_ocr_plausibility(
    ocr: Dict[str, Any],
    pdf_path: Optional[str] = None,
    email_internal_date: Optional[_dt.datetime] = None,
    *,
    amount_tolerance_pct: float = 0.10,
    date_window_days: int = 90,
) -> Dict[str, Any]:
    """
    Annotate `ocr` dict with confidence flags. Mutates and returns the dict.

    Args:
        ocr: OCR output from extract_from_bytes (has transactionAmount etc.)
        pdf_path: path to the source PDF (for pdftotext cross-check).
                  If omitted, amount check is skipped.
        email_internal_date: the email's internalDate as datetime (CST).
                  If omitted, date window check is skipped.

    Side-effects on ocr:
        _amountConfidence: "low" if LLM amount diverges from PDF amounts by
                           more than 10%.
        _dateConfidence: "low" if LLM date falls outside ±90 days of email.
    """
    # --- Amount plausibility ---
    llm_amount = ocr.get("transactionAmount")
    # For hotel folios the primary number is balance
    if llm_amount is None:
        llm_amount = ocr.get("balance")

    if pdf_path and llm_amount is not None:
        try:
            llm_amount_f = float(llm_amount)
        except (ValueError, TypeError):
            # LLM returned a non-numeric amount (e.g. "1,280.00元"). We can't
            # cross-check against the PDF, so flag it low-confidence so the
            # downstream CSV marks the row as suspect instead of claiming
            # "high confidence" by default.
            ocr["_amountConfidence"] = "low"
            ocr["_amountCheckSkipped"] = f"non-numeric: {llm_amount!r}"
        else:
            page_amounts = _extract_amounts_from_pdf(pdf_path)
            if not _amount_matches_any(llm_amount_f, page_amounts, amount_tolerance_pct):
                ocr["_amountConfidence"] = "low"
                ocr["_amountPageValues"] = page_amounts[:10]  # for debugging

    # --- Date plausibility ---
    tx_date = _parse_ocr_date(ocr.get("transactionDate", ""))
    if tx_date and email_internal_date is not None:
        try:
            email_date = email_internal_date.date() if hasattr(email_internal_date, "date") else email_internal_date
            delta = abs((tx_date - email_date).days)
            if delta > date_window_days:
                ocr["_dateConfidence"] = "low"
                ocr["_dateDeltaDays"] = delta
        except (AttributeError, TypeError):
            pass

    return ocr
