"""
LLM-based invoice OCR extraction.

Adapted from reimbursement-helper/backend/agent/utils/bedrock_ocr.py
(commit a0e8515). Key differences:

- Takes PDF bytes directly (no S3 round trip). Callers pass bytes read
  from local disk.
- Uses the provider-agnostic `llm_client` adapter (Anthropic default,
  Bedrock optional, none = disabled).
- Adds on-disk SHA256 cache at ~/.cache/gmail-invoice-downloader/ocr/
  so re-runs of the same PDF batch cost $0.
- Keeps the buyer/seller validation logic from the original.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from .llm_client import (
    LLMClient,
    LLMDisabledError,
    extract_with_retry,
    get_client,
)
from .prompts import get_ocr_prompt


# =============================================================================
# Response parsing
# =============================================================================

def parse_llm_response(response: str) -> Dict[str, Any]:
    """
    Extract JSON from an LLM response string.

    Handles plain JSON, ```json ... ``` markdown blocks, and JSON with
    surrounding commentary.

    Raises:
        ValueError: empty response.
        json.JSONDecodeError: malformed JSON.
    """
    if not response or not response.strip():
        raise ValueError("Empty response from LLM")

    content = response.strip()

    if "```json" in content:
        content = content.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in content:
        parts = content.split("```")
        if len(parts) >= 2:
            content = parts[1].strip()

    if not content.startswith("{"):
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            content = match.group(0)

    return json.loads(content)


# =============================================================================
# Vendor validation (buyer/seller confusion fix)
# =============================================================================

BUYER_KEYWORDS = [
    "亚马逊", "amazon", "Amazon", "AMAZON",
    "世纪卓越", "华越博信", "神州祥龙", "宁夏通云",
]


def validate_and_fix_vendor_info(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    When the LLM mistakes the buyer (left side of VAT invoice) as the vendor
    (right side), recover via seller/hotel/buyer-reversed fallbacks.

    Mutates and returns data with vendorName/vendorTaxId corrected.
    Sets `_vendorNameInvalid = True` if nothing rescuable.
    """
    vendor_name = (data.get("vendorName") or "")
    vendor_tax_id = (data.get("vendorTaxId") or "")
    seller_name = (data.get("sellerName") or "")
    seller_tax_id = (data.get("sellerTaxId") or "")
    hotel_name = (data.get("hotelName") or "")

    # Missing vendor entirely (LLM returned null or empty). Try seller / hotel
    # fallbacks before giving up; if nothing works, mark invalid so downstream
    # CSV shows low confidence instead of defaulting to "high".
    if not vendor_name:
        if seller_name:
            data["vendorName"] = seller_name
            if seller_tax_id:
                data["vendorTaxId"] = seller_tax_id
            return data
        if hotel_name:
            data["vendorName"] = hotel_name
            return data
        data["_vendorNameInvalid"] = True
        return data

    is_buyer = any(kw in vendor_name for kw in BUYER_KEYWORDS)
    if not is_buyer:
        return data

    # Strategy 1: use seller fields
    if seller_name and not any(kw in seller_name for kw in BUYER_KEYWORDS):
        data["vendorName"] = seller_name
        if seller_tax_id:
            data["vendorTaxId"] = seller_tax_id
        return data

    # Strategy 2: hotels — use hotelName
    if hotel_name and not any(kw in hotel_name for kw in BUYER_KEYWORDS):
        data["vendorName"] = hotel_name
        return data

    # Strategy 3: LLM reversed buyer/seller
    buyer_name = (data.get("buyerName") or "")
    if buyer_name and not any(kw in buyer_name for kw in BUYER_KEYWORDS):
        data["vendorName"] = buyer_name
        buyer_tax_id = (data.get("buyerTaxId") or "")
        if buyer_tax_id:
            data["vendorTaxId"] = buyer_tax_id
        return data

    # Strategy 4: nothing to fall back on
    data["vendorName"] = ""
    data["vendorTaxId"] = ""
    data["_vendorNameInvalid"] = True
    return data


# =============================================================================
# On-disk cache
# =============================================================================

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "gmail-invoice-downloader" / "ocr"


def _cache_path(pdf_bytes: bytes, cache_dir: Path) -> Path:
    digest = hashlib.sha256(pdf_bytes).hexdigest()[:16]
    return cache_dir / f"{digest}.json"


def _cache_read(pdf_bytes: bytes, cache_dir: Path) -> Optional[Dict[str, Any]]:
    path = _cache_path(pdf_bytes, cache_dir)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("ocr")
    except (json.JSONDecodeError, OSError):
        # Corrupt cache entry — pretend it doesn't exist, caller will re-analyze
        return None


def _cache_write(
    pdf_bytes: bytes, ocr_data: Dict[str, Any], cache_dir: Path
) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = _cache_path(pdf_bytes, cache_dir)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"ocr": ocr_data, "schema_version": "1.0"},
                f, ensure_ascii=False,
            )
        os.replace(tmp, path)
    except OSError:
        # Cache write failure is non-fatal
        pass


# =============================================================================
# Main extraction entry point
# =============================================================================

def extract_from_bytes(
    pdf_bytes: bytes,
    *,
    filename_hint: str = "unknown.pdf",
    llm_client: Optional[LLMClient] = None,
    use_cache: bool = True,
    cache_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Extract invoice fields from PDF bytes via LLM.

    Args:
        pdf_bytes: raw PDF content.
        filename_hint: logged for debugging; doesn't affect the prompt.
        llm_client: override for tests. Default uses the singleton.
        use_cache: set False to force a fresh call.
        cache_dir: override cache location (tests).

    Returns:
        Dict with transactionDate, transactionAmount, vendorName, etc.
        See prompts.py for the full field list.

    Raises:
        LLMError subclasses on provider failure.
        ValueError / JSONDecodeError on unparseable LLM response.
    """
    cache_dir = cache_dir or DEFAULT_CACHE_DIR

    if use_cache:
        cached = _cache_read(pdf_bytes, cache_dir)
        if cached is not None:
            return cached

    client = llm_client or get_client()
    if getattr(client, "provider_name", "") == "none":
        raise LLMDisabledError("LLM extraction skipped (provider=none)")

    prompt = get_ocr_prompt()
    response_text = extract_with_retry(pdf_bytes, prompt, client=client)

    data = parse_llm_response(response_text)
    data = validate_and_fix_vendor_info(data)

    if use_cache:
        _cache_write(pdf_bytes, data, cache_dir)

    return data
