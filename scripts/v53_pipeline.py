"""
v5.3 pipeline additions for download-invoices.py.

Kept in a separate module for readability — the main CLI (download-invoices.py)
orchestrates the existing v5.2 download path and delegates the new stages
here:

1. analyze_pdf_batch — concurrent LLM OCR + classify + city + plausibility check
2. rename_by_ocr    — rename PDFs to {date}_{vendor}_{category}.pdf
3. do_all_matching  — P1 remark / P2 date+amount / P3 v5.2 date-only fallback
4. write_missing_json — schema v1.0 with status / recommended_next_action
5. write_summary_csv  — UTF-8 BOM CSV with columns requested by user
6. zip_output        — atomic zip of output dir (allowlist: pdf/md/csv)
7. merge_supplemental — dedup by (msg_id, att_part_id), clean stale paths

No Gmail-specific logic here; this module stays testable with mock records.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.classify import classify_invoice
from core.llm_client import (
    LLMDisabledError,
    LLMError,
    get_client,
)
from core.llm_ocr import extract_from_bytes
from core.location import extract_city
from core.matching import (
    match_hotel_pairs,
    match_ride_hailing_pairs,
)
from core.validation import validate_ocr_plausibility


# =============================================================================
# Category labels for filenames / CSV
# =============================================================================

CATEGORY_LABELS: Dict[str, str] = {
    "HOTEL_INVOICE":       "酒店发票",
    "HOTEL_FOLIO":         "水单",
    "RIDEHAILING_INVOICE": "网约车发票",
    "RIDEHAILING_RECEIPT": "行程单",
    "TAXI":                "出租车发票",
    "TRAIN":               "火车票",
    "MEAL":                "餐饮",
    "MOBILE":              "话费",
    "TOLLS":               "通行费",
    "UNKNOWN":             "发票",
    "UNPARSED":            "⚠️ 需人工核查",
}

CATEGORY_ORDER: Dict[str, int] = {
    "HOTEL_FOLIO":         1,
    "HOTEL_INVOICE":       2,
    "MEAL":                3,
    "RIDEHAILING_INVOICE": 4,
    "RIDEHAILING_RECEIPT": 5,
    "TRAIN":               6,
    "TAXI":                7,
    "MOBILE":              8,
    "TOLLS":               9,
    "UNKNOWN":             10,
    "UNPARSED":            99,
}


# =============================================================================
# Filename sanitization — security against path traversal
# =============================================================================

_FILENAME_REPLACE_RE = re.compile(r'[/\\:*?"<>|\n\r\t]')
_FILENAME_DOTS_RE = re.compile(r"\.\.+")


def sanitize_filename(s: Optional[str], max_len: int = 80) -> str:
    """Make a string safe to embed in a filename.

    Strips NUL, path separators, and collapses .. sequences. Empty and
    None collapse to '未知商户'.
    """
    if not s:
        return "未知商户"
    s = s.replace("\0", "")
    s = _FILENAME_REPLACE_RE.sub("_", s)
    s = _FILENAME_DOTS_RE.sub("_", s)
    s = s.lstrip(".-_ ")
    s = s.strip()[:max_len]
    return s or "未知商户"


def normalize_date(d: Optional[str]) -> str:
    """LLM dates come as 'YYYY-MM-DD'. Collapse to 'YYYYMMDD' for filenames.

    Rejects calendar-invalid inputs like '2026-02-31' or '2026-13-05' — LLMs
    occasionally hallucinate these and the regex alone won't catch them.
    Returns '' on reject so callers fall back to the email internalDate.
    """
    if not d:
        return ""
    s = str(d).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m2 = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
        if not m2:
            return ""
        y, mo, day = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
    try:
        _dt.date(y, mo, day)
    except ValueError:
        return ""
    return f"{y:04d}{mo:02d}{day:02d}"


# make_unique_path lives in invoice_helpers.py; re-exported here so callers
# that already import v53_pipeline don't need a second import line.
from invoice_helpers import make_unique_path  # noqa: E402


# =============================================================================
# Step 6.5a — analyze_pdf_batch
# =============================================================================

def analyze_pdf_batch(
    records: List[Dict[str, Any]],
    *,
    max_workers: int = 2,
    use_llm: bool = True,
    logger: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    """Run LLM OCR + classify + city + plausibility for each valid record.

    Args:
        records: list of download records with `path`, `valid`, optional
                 `internal_date` (email internalDate ms).
        max_workers: ThreadPoolExecutor parallelism. Default 2 is safe for
                     Anthropic tier-1. Override with env LLM_OCR_CONCURRENCY.
        use_llm: pass False to skip LLM entirely (--no-llm mode).
        logger: optional object with .write or .print for progress output.

    Returns:
        {record_path: {ocr, category, city, error, used_fallback}}
    """
    # Construct client once (singleton will cache). Let auth / config errors
    # propagate so the caller can map them to EXIT_LLM_CONFIG with a clear
    # REMEDIATION line. Silent degradation to UNPARSED on auth failure was
    # worse than surfacing the real problem (user misreads 60 UNPARSED rows
    # as a data-quality issue rather than a missing env var).
    if use_llm:
        get_client()  # raises LLMAuthError / LLMConfigError on misconfig

    results: Dict[str, Dict[str, Any]] = {}
    to_analyze = [r for r in records if r.get("valid") and r.get("path")]

    # Anthropic's PDF limit is 32MB; OpenAI is 32MB; Bedrock is similar. Reject
    # oversize PDFs before read/base64 (which would balloon memory to ~5× the
    # file size across 2+ workers) and before a guaranteed-failing LLM call.
    MAX_PDF_BYTES = 32 * 1024 * 1024

    def analyze_one(record: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        path = record["path"]
        try:
            size = os.path.getsize(path)
        except OSError as e:
            return path, {
                "ocr": None, "category": "UNPARSED",
                "error": f"stat failed: {e}", "used_fallback": False,
            }
        if size > MAX_PDF_BYTES:
            return path, {
                "ocr": None, "category": "UNPARSED",
                "error": f"pdf_too_large: {size} bytes > {MAX_PDF_BYTES}",
                "used_fallback": False,
            }
        try:
            with open(path, "rb") as f:
                pdf_bytes = f.read()
        except OSError as e:
            return path, {
                "ocr": None, "category": "UNPARSED",
                "error": f"read failed: {e}", "used_fallback": False,
            }

        if not use_llm:
            return path, {
                "ocr": None, "category": "UNPARSED",
                "error": "llm_disabled", "used_fallback": False,
            }

        try:
            ocr = extract_from_bytes(pdf_bytes, filename_hint=os.path.basename(path))
        except LLMDisabledError:
            return path, {
                "ocr": None, "category": "UNPARSED",
                "error": "llm_disabled", "used_fallback": False,
            }
        except LLMError as e:
            # Expected LLM-layer failures (rate limit, server error, parse).
            # Keep the batch going; surface the record as UNPARSED for review.
            _log(logger, f"  ❌ OCR failed for {os.path.basename(path)}: {type(e).__name__}: {str(e)[:100]}")
            return path, {
                "ocr": None, "category": "UNPARSED",
                "error": f"{type(e).__name__}: {e}", "used_fallback": False,
            }
        except (ValueError, json.JSONDecodeError) as e:
            # LLM returned something we couldn't parse as JSON.
            _log(logger, f"  ❌ OCR parse failed for {os.path.basename(path)}: {type(e).__name__}: {str(e)[:100]}")
            return path, {
                "ocr": None, "category": "UNPARSED",
                "error": f"{type(e).__name__}: {e}", "used_fallback": False,
            }

        # Anti-hallucination
        email_date_ms = record.get("internal_date")
        email_dt = None
        if email_date_ms:
            try:
                email_dt = _dt.datetime.fromtimestamp(int(email_date_ms) / 1000)
            except (ValueError, TypeError):
                pass
        ocr = validate_ocr_plausibility(ocr, pdf_path=path, email_internal_date=email_dt)

        category = classify_invoice(ocr)
        try:
            city = extract_city(ocr, category)
        except Exception:
            city = ""

        return path, {
            "ocr": ocr, "category": category, "city": city,
            "error": None, "used_fallback": False,
        }

    # Thread-pool for concurrent LLM calls (singleton client is thread-safe).
    # Map future -> record so a crashed worker still produces an UNPARSED row
    # instead of silently vanishing from results.
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [(ex.submit(analyze_one, r), r) for r in to_analyze]
        for i, (fut, rec) in enumerate(futures, 1):
            try:
                path, analysis = fut.result()
            except Exception as e:
                # Truly unexpected — the analyze_one wrapper already catches
                # LLMError / ValueError / OSError. Surface as UNPARSED so
                # downstream still sees the record.
                path = rec.get("path", "")
                analysis = {
                    "ocr": None, "category": "UNPARSED",
                    "error": f"worker_crashed: {type(e).__name__}: {e}",
                    "used_fallback": False,
                }
                _log(logger, f"  ❌ worker crashed on {os.path.basename(path)}: {e}")
            results[path] = analysis
            if i % 5 == 0:
                _log(logger, f"  OCR progress: {i}/{len(to_analyze)}")

    return results


def _log(logger: Optional[Any], msg: str) -> None:
    """Emit to stdout and, if a file-like logger is passed, also to that file.

    Narrow exception so genuine programming errors (AttributeError on a bad
    logger shape, TypeError from a non-str msg) surface instead of being
    swallowed; only OSError (disk full, closed file) is expected here.
    """
    if logger is None:
        print(msg)
        return
    if hasattr(logger, "write"):
        try:
            logger.write(msg + "\n")
            logger.flush()
        except (OSError, ValueError):
            pass
    print(msg)


# =============================================================================
# Step 6.5b — rename_by_ocr
# =============================================================================

def rename_by_ocr(
    record: Dict[str, Any],
    analysis: Dict[str, Any],
    pdfs_dir: str,
) -> Dict[str, Any]:
    """Rename a downloaded PDF using OCR-extracted date+vendor+category.

    On success: record['path'] is updated, and ocr/category/vendor/etc are
    merged into record. On OCR failure: file is renamed UNPARSED_<msgid>_<old>.pdf
    and record.category is set to 'UNPARSED'.

    Returns the (mutated) record for chaining.
    """
    old_path = record.get("path")
    if not old_path or not os.path.exists(old_path):
        return record

    ocr = analysis.get("ocr")
    category = analysis.get("category", "UNPARSED")
    err = analysis.get("error")

    # Merge analysis into record regardless of path change
    record["ocr"] = ocr
    record["category"] = category
    record["city"] = analysis.get("city")

    if err or ocr is None:
        # UNPARSED: prefix file with UNPARSED_{msgid} so user sees it
        msg_id = record.get("message_id", "unknown")[:12]
        base = os.path.basename(old_path)
        new_name = sanitize_filename(f"UNPARSED_{msg_id}_{base}", max_len=200)
        # sanitize may have removed the .pdf — re-add
        if not new_name.endswith(".pdf"):
            new_name += ".pdf"
        new_path = make_unique_path(pdfs_dir, new_name)
        if new_path != old_path:
            os.rename(old_path, new_path)
            record["path"] = new_path
        record["vendor_name"] = record.get("merchant") or "未知"
        record["transaction_date"] = record.get("date", "")
        return record

    # Happy path: {YYYYMMDD}_{vendor}_{label}.pdf
    date_str = normalize_date(ocr.get("transactionDate")) or record.get("date", "")
    vendor = sanitize_filename(ocr.get("vendorName") or record.get("merchant") or "未知商户")
    label = CATEGORY_LABELS.get(category, "发票")
    new_filename = f"{date_str}_{vendor}_{label}.pdf"
    new_path = make_unique_path(pdfs_dir, new_filename)

    if new_path != old_path:
        os.rename(old_path, new_path)
        record["path"] = new_path

    record["vendor_name"] = vendor
    record["transaction_date"] = date_str
    return record


# =============================================================================
# Step 7 — do_all_matching (P1 remark / P2 date+amount / P3 v5.2 date-only)
# =============================================================================

def _to_float(val: Any) -> Optional[float]:
    """None-safe float. Returns None (not 0) for missing/invalid."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _to_matching_input(record: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a download record into the dict matching.py expects.

    matching.py uses 's3Key' for file-number extraction — we reuse it for
    the local path since the logic is the same (basename → (1)(2) digits).
    """
    ocr = record.get("ocr") or {}
    return {
        "s3Key":              record.get("path"),
        "transactionAmount":  _to_float(ocr.get("transactionAmount")),
        "transactionDate":    ocr.get("transactionDate"),
        "remark":             ocr.get("remark"),
        "balance":            _to_float(ocr.get("balance")),
        "checkOutDate":       ocr.get("checkOutDate"),
        "departureDate":      ocr.get("departureDate"),
        "confirmationNo":     ocr.get("confirmationNo"),
        "internalCodes":      ocr.get("internalCodes", []) or [],
        "totalAmount":        _to_float(ocr.get("totalAmount")),
        "_record":            record,
    }


def do_all_matching(downloaded: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Group records by category, match hotels (P1/P2), ride-hailing (amount),
    and fall back to v5.2 date-only for hotels that P1/P2 missed (P3).

    Returns a dict keyed by result bucket:
        hotel        -> {matched, unmatched_invoices, unmatched_folios}
        ridehailing  -> {matched, unmatched_invoices, unmatched_receipts}
        meal / train / taxi / mobile / tolls / unknown / unparsed -> [records]
    """
    by_cat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for d in downloaded:
        if not d.get("valid"):
            continue
        cat = d.get("category") or "UNPARSED"
        # When OCR failed or category is UNPARSED, skip matching
        if cat == "UNPARSED" or not d.get("ocr"):
            by_cat["UNPARSED"].append(d)
            continue
        by_cat[cat].append(_to_matching_input(d))

    # --- Hotel: P1 remark, P2 date+amount (from core.matching) ---
    hotel_primary = match_hotel_pairs(
        invoices=by_cat.get("HOTEL_INVOICE", []),
        folios=by_cat.get("HOTEL_FOLIO", []),
    )

    # --- P3 date-only v5.2 fallback ---
    unmatched_inv = list(hotel_primary["unmatched_invoices"])
    unmatched_fol = list(hotel_primary["unmatched_folios"])
    tier3: List[Dict[str, Any]] = []
    used_fol_idx: set[int] = set()
    for inv in unmatched_inv:
        for i, fol in enumerate(unmatched_fol):
            if i in used_fol_idx:
                continue
            inv_date = inv.get("transactionDate")
            fol_checkout = fol.get("checkOutDate") or fol.get("departureDate")
            if inv_date and fol_checkout and inv_date == fol_checkout:
                tier3.append({
                    "invoice": inv, "folio": fol,
                    "match_type": "date_only (v5.2 fallback)",
                    "confidence": "low",
                })
                used_fol_idx.add(i)
                break

    tier3_inv_ids = {id(m["invoice"]) for m in tier3}
    final_unmatched_inv = [i for i in unmatched_inv if id(i) not in tier3_inv_ids]
    final_unmatched_fol = [f for j, f in enumerate(unmatched_fol) if j not in used_fol_idx]

    hotel_final = {
        "matched": hotel_primary["matched"] + tier3,
        "unmatched_invoices": final_unmatched_inv,
        "unmatched_folios": final_unmatched_fol,
    }

    # --- Ride-hailing: amount match + file-number tiebreaker ---
    rh_result = match_ride_hailing_pairs(
        invoices=by_cat.get("RIDEHAILING_INVOICE", []),
        receipts=by_cat.get("RIDEHAILING_RECEIPT", []),
    )

    # --- Lists of raw records (not _to_matching_input shaped) ---
    def _raw(cat: str) -> List[Dict[str, Any]]:
        return [m["_record"] for m in by_cat.get(cat, [])]

    return {
        "hotel":       hotel_final,
        "ridehailing": rh_result,
        "meal":        _raw("MEAL"),
        "train":       _raw("TRAIN"),
        "taxi":        _raw("TAXI"),
        "mobile":      _raw("MOBILE"),
        "tolls":       _raw("TOLLS"),
        "unknown":     _raw("UNKNOWN"),
        "unparsed":    by_cat.get("UNPARSED", []),
    }


# =============================================================================
# Step 8a — write_summary_csv (UTF-8 BOM, None-safe)
# =============================================================================

CSV_COLUMNS = ["序号", "开票日期", "类别", "金额", "销售方", "备注", "文件名", "数据可信度"]


def write_summary_csv(path: str, all_valid_records: List[Dict[str, Any]]) -> int:
    """Write 发票汇总.csv with UTF-8 BOM (Excel-compatible).

    Rules:
    - None amounts become empty string (NOT 0.00) so Excel shows blank
    - Sort by transactionDate ascending, then by category order
    - UNPARSED rows always at bottom (category_order = 99)
    - 数据可信度: high (clean OCR), low (flagged by validate_ocr_plausibility),
      failed (OCR failed, UNPARSED)

    Returns row count written.
    """
    def sort_key(r: Dict[str, Any]):
        ocr = r.get("ocr") or {}
        date = ocr.get("transactionDate") or r.get("date", "") or "99999999"
        cat = r.get("category", "UNKNOWN")
        return (date, CATEGORY_ORDER.get(cat, 50))

    rows: List[List[Any]] = []
    for i, r in enumerate(sorted(all_valid_records, key=sort_key), 1):
        ocr = r.get("ocr") or {}
        cat = r.get("category", "UNKNOWN")

        # Amount: folios use balance, receipts use totalAmount, else transactionAmount.
        # Explicit None check (not `or`) so a legitimate 0.00 doesn't fall through —
        # e.g., prepaid folio with balance=0 should show 0.00, not be replaced by
        # transactionAmount.
        if cat == "HOTEL_FOLIO":
            amt = _to_float(ocr.get("balance"))
            if amt is None:
                amt = _to_float(ocr.get("transactionAmount"))
        elif cat == "RIDEHAILING_RECEIPT":
            amt = _to_float(ocr.get("totalAmount"))
            if amt is None:
                amt = _to_float(ocr.get("transactionAmount"))
        else:
            amt = _to_float(ocr.get("transactionAmount"))

        # Remarks assembled from category-specific fields
        remark_bits: List[str] = []
        if ocr.get("remark"):
            remark_bits.append(f"remark={ocr['remark']}")
        if ocr.get("confirmationNo"):
            remark_bits.append(f"confNo={ocr['confirmationNo']}")
        if ocr.get("phoneNumber"):
            remark_bits.append(f"phone={ocr['phoneNumber']}")
        if ocr.get("billingPeriod"):
            remark_bits.append(f"period={ocr['billingPeriod']}")
        if ocr.get("tripCount"):
            remark_bits.append(f"trips={ocr['tripCount']}")
        if ocr.get("trainNumber"):
            remark_bits.append(
                f"{ocr.get('departureStation','?')}→{ocr.get('arrivalStation','?')} "
                f"车次{ocr['trainNumber']}"
            )
        if ocr.get("_amountConfidence") == "low":
            remark_bits.append("⚠️金额可疑")
        if ocr.get("_dateConfidence") == "low":
            remark_bits.append("⚠️日期异常")
        if ocr.get("_vendorNameInvalid"):
            remark_bits.append("⚠️销售方未识别")

        # Confidence flag — any validation flag demotes the row, including
        # unidentified vendor. Finance filtering on confidence='high' should
        # never see a row with a ⚠️ warning in the remarks column.
        if cat == "UNPARSED" or not ocr:
            confidence = "failed"
        elif (
            ocr.get("_amountConfidence") == "low"
            or ocr.get("_dateConfidence") == "low"
            or ocr.get("_vendorNameInvalid")
        ):
            confidence = "low"
        else:
            confidence = "high"

        rows.append([
            i,
            ocr.get("transactionDate") or r.get("date", "") or "—",
            CATEGORY_LABELS.get(cat, "发票"),
            f"{amt:.2f}" if amt is not None else "",
            ocr.get("vendorName") or r.get("merchant") or "—",
            "; ".join(remark_bits) or "—",
            os.path.basename(r.get("path", "")),
            confidence,
        ])

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        w.writerows(rows)

    return len(rows)


# =============================================================================
# Step 8b — write_missing_json (schema v1.0)
# =============================================================================

MISSING_SCHEMA_VERSION = "1.0"
DEFAULT_ITERATION_CAP = 3


def _search_suggestion_for_item(
    kind: str,
    needed_for: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build a Gmail search suggestion for a missing-item kind.

    kind: one of 'hotel_folio', 'hotel_invoice', 'ridehailing_receipt',
          'ridehailing_invoice', 'extraction_failed'.
    needed_for: the OCR-extracted fields from the half we already have.
    """
    tx_date = needed_for.get("transactionDate") or needed_for.get("checkOutDate")
    vendor = needed_for.get("vendorName") or needed_for.get("hotelName") or ""

    # Extract brand keyword from vendor name for query (best-effort)
    brand_keywords = []
    from core.matching import HOTEL_BRAND_KEYWORDS
    for kw in HOTEL_BRAND_KEYWORDS:
        if kw.lower() in vendor.lower():
            brand_keywords.append(kw)
            if len(brand_keywords) >= 2:
                break

    def _shift(date_str: Optional[str], days: int) -> Optional[str]:
        if not date_str:
            return None
        try:
            d = _dt.date.fromisoformat(date_str[:10])
            return (d + _dt.timedelta(days=days)).isoformat().replace("-", "/")
        except ValueError:
            return None

    if kind == "hotel_folio":
        # Need the folio that matches this invoice
        start = _shift(tx_date, -2)
        end = _shift(tx_date, 2)
        brand_part = " ".join(brand_keywords) if brand_keywords else ""
        return {
            "query": f'水单 OR folio OR "Guest Folio" {brand_part}'.strip(),
            "date_range_start": start or "",
            "date_range_end": end or "",
            "priority": "high",
        }

    if kind == "hotel_invoice":
        # Invoice typically arrives 3-10 days after checkout
        start = _shift(tx_date, 0)
        end = _shift(tx_date, 10)
        brand_part = " ".join(brand_keywords) if brand_keywords else ""
        return {
            "query": f"发票 (住宿 OR 房费) {brand_part}".strip(),
            "date_range_start": start or "",
            "date_range_end": end or "",
            "priority": "high",
        }

    if kind == "ridehailing_receipt":
        start = _shift(tx_date, -2)
        end = _shift(tx_date, 2)
        return {
            "query": "行程报销 OR 行程单 滴滴 OR 高德 OR 曹操 OR 首汽",
            "date_range_start": start or "",
            "date_range_end": end or "",
            "priority": "medium",
        }

    if kind == "ridehailing_invoice":
        # Ride-hailing invoices may be issued later, month-end
        start = _shift(tx_date, 0)
        end = _shift(tx_date, 30)
        return {
            "query": "发票 (滴滴 OR 高德 OR 网约车 OR 客运服务)",
            "date_range_start": start or "",
            "date_range_end": end or "",
            "priority": "medium",
        }

    # extraction_failed: no search helps; human must check
    return None


def _compute_convergence_hash(items: List[Dict[str, Any]]) -> str:
    """Hash sorted (type, needed_for) tuples to detect round-over-round
    convergence (items unchanged → no progress → stop).

    Including ``type`` means transitions like hotel_folio → extraction_failed
    on the same filename register as a change, not a false convergence.
    """
    keys = sorted(
        (item.get("type", ""), item.get("needed_for", ""))
        for item in items
    )
    blob = json.dumps(keys, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def write_missing_json(
    path: str,
    *,
    batch_dir: str,
    iteration: int,
    iteration_cap: int = DEFAULT_ITERATION_CAP,
    matching_result: Dict[str, Any],
    unparsed_records: List[Dict[str, Any]],
    previous_convergence_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Build missing.json from do_all_matching output and write to disk.

    Returns the dict that was written (for caller to use / log).

    Schema version 1.0:
      - schema_version, generated_at, iteration, iteration_cap
      - status: converged | needs_retry | max_iterations_reached | user_action_required
      - recommended_next_action: run_supplemental | stop | ask_user
      - convergence_hash: sha256(sorted needed_for keys)
      - items: list of missing artifacts with per-item search suggestions
    """
    items: List[Dict[str, Any]] = []

    # Hotel mismatches
    hotel = matching_result.get("hotel", {})
    for inv in hotel.get("unmatched_invoices", []):
        ocr = (inv.get("_record") or {}).get("ocr") or {}
        needed_for = os.path.basename((inv.get("_record") or {}).get("path", "") or "")
        items.append({
            "type": "hotel_folio",
            "needed_for": needed_for,
            "expected_date": ocr.get("transactionDate"),
            "expected_merchant": ocr.get("vendorName"),
            "expected_amount": ocr.get("transactionAmount"),
            "remark_from_invoice": ocr.get("remark"),
            "hint": (
                f"发票 remark={ocr.get('remark')!r} 未在任何水单的 "
                f"confirmationNo / internalCodes 中出现"
            ),
            "search_suggestion": _search_suggestion_for_item("hotel_folio", ocr),
        })
    for fol in hotel.get("unmatched_folios", []):
        ocr = (fol.get("_record") or {}).get("ocr") or {}
        needed_for = os.path.basename((fol.get("_record") or {}).get("path", "") or "")
        items.append({
            "type": "hotel_invoice",
            "needed_for": needed_for,
            "expected_date": ocr.get("checkOutDate") or ocr.get("departureDate"),
            "expected_merchant": ocr.get("hotelName") or ocr.get("vendorName"),
            "expected_amount": ocr.get("balance") or ocr.get("transactionAmount"),
            "hint": "水单已收但无同日/匹配 confirmationNo 的酒店发票。发票常晚于退房 3-7 天开出",
            "search_suggestion": _search_suggestion_for_item("hotel_invoice", ocr),
        })

    # Ride-hailing mismatches
    rh = matching_result.get("ridehailing", {})
    for inv in rh.get("unmatched_invoices", []):
        ocr = (inv.get("_record") or {}).get("ocr") or {}
        needed_for = os.path.basename((inv.get("_record") or {}).get("path", "") or "")
        items.append({
            "type": "ridehailing_receipt",
            "needed_for": needed_for,
            "expected_date": ocr.get("transactionDate"),
            "expected_amount": ocr.get("transactionAmount"),
            "hint": "同金额发票无匹配行程单",
            "search_suggestion": _search_suggestion_for_item("ridehailing_receipt", ocr),
        })
    for rec in rh.get("unmatched_receipts", []):
        ocr = (rec.get("_record") or {}).get("ocr") or {}
        needed_for = os.path.basename((rec.get("_record") or {}).get("path", "") or "")
        items.append({
            "type": "ridehailing_invoice",
            "needed_for": needed_for,
            "expected_date": ocr.get("transactionDate"),
            "expected_amount": ocr.get("totalAmount"),
            "hint": "行程单无匹配金额发票。网约车发票可能月底统一开",
            "search_suggestion": _search_suggestion_for_item("ridehailing_invoice", ocr),
        })

    # Unparsed records
    for r in unparsed_records:
        items.append({
            "type": "extraction_failed",
            "needed_for": os.path.basename(r.get("path", "")),
            "hint": f"LLM OCR 失败或被跳过：{r.get('error', 'unknown')}. 人工核查 PDF 是否损坏/是否发票",
            "search_suggestion": None,
        })

    # Convergence + status
    convergence_hash = _compute_convergence_hash(items)
    has_converged_vs_previous = (
        previous_convergence_hash is not None
        and convergence_hash == previous_convergence_hash
    )

    has_extraction_failed = any(item["type"] == "extraction_failed" for item in items)
    non_failed_items = [i for i in items if i["type"] != "extraction_failed"]

    if not items:
        status = "converged"
        recommended_next_action = "stop"
    elif has_converged_vs_previous:
        status = "converged"
        recommended_next_action = "stop"
    elif iteration >= iteration_cap:
        status = "max_iterations_reached"
        recommended_next_action = "ask_user"
    elif not non_failed_items and has_extraction_failed:
        status = "user_action_required"
        recommended_next_action = "ask_user"
    else:
        status = "needs_retry"
        recommended_next_action = "run_supplemental"

    payload: Dict[str, Any] = {
        "schema_version": MISSING_SCHEMA_VERSION,
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "iteration": iteration,
        "iteration_cap": iteration_cap,
        "status": status,
        "recommended_next_action": recommended_next_action,
        "convergence_hash": convergence_hash,
        "batch_dir": batch_dir,
        "items": items,
    }

    # Atomic write
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return payload


# =============================================================================
# Step 9 — zip_output (atomic write + allowlist)
# =============================================================================

ZIP_ALLOWED_SUFFIXES = {".pdf", ".md", ".csv"}
ZIP_PREFIX = "发票打包_"


def zip_output(
    output_dir: str,
    *,
    dest_dir: Optional[str] = None,
    now: Optional[_dt.datetime] = None,
) -> str:
    """Create 发票打包_YYYYMMDD-HHMMSS.zip from output_dir.

    - Allowlist: only .pdf, .md, .csv (internal state JSONs excluded)
    - Self-exclusion: previous 发票打包_*.zip files not embedded
    - Atomic write: .tmp then os.replace
    - Manifest check: at least 1 CSV + 1 report MD + N PDFs
    - Default dest_dir is the parent of output_dir (so the zip sits alongside it)

    Returns the path of the created zip.
    """
    now = now or _dt.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    dest_dir = dest_dir or os.path.dirname(os.path.abspath(output_dir))
    zip_name = f"{ZIP_PREFIX}{stamp}.zip"
    final_path = os.path.join(dest_dir, zip_name)
    tmp_path = final_path + ".tmp"

    pdf_count = csv_count = md_count = 0

    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(output_dir):
            for fn in files:
                suffix = os.path.splitext(fn)[1].lower()
                if suffix not in ZIP_ALLOWED_SUFFIXES:
                    continue
                # Don't embed previous zips. Tighten to .zip suffix so a legit
                # PDF whose LLM-extracted vendor happens to begin with 发票打包_
                # isn't silently dropped from the output.
                if fn.startswith(ZIP_PREFIX) and fn.endswith(".zip"):
                    continue
                # Refuse symlinks so an attacker-placed symlink inside output_dir
                # can't exfiltrate files from outside the tree via the zip.
                if os.path.islink(os.path.join(root, fn)):
                    continue
                fp = os.path.join(root, fn)
                arcname = os.path.relpath(fp, output_dir)
                z.write(fp, arcname)
                if suffix == ".pdf":
                    pdf_count += 1
                elif suffix == ".csv":
                    csv_count += 1
                elif suffix == ".md":
                    md_count += 1

    # Manifest sanity check — at least the report (md) and summary (csv)
    if csv_count == 0 or md_count == 0:
        os.remove(tmp_path)
        raise RuntimeError(
            f"zip 完整性检查失败: csv={csv_count} md={md_count} "
            f"(expected >= 1 of each). 检查 output_dir 是否完整：{output_dir}"
        )

    os.replace(tmp_path, final_path)
    return final_path


# =============================================================================
# Supplemental merge (dedup by msg_id + attachment_part_id)
# =============================================================================

def merge_supplemental_downloads(
    step4_path: str,
    new_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge new download records into an existing step4_downloaded.json.

    Dedup key: (message_id, attachment_part_id) — stable across renames,
    unlike filename which mutates during rename_by_ocr.

    Also prunes records whose `path` no longer exists on disk (user may
    have deleted a PDF between runs).

    Writes merged state back atomically; backs up existing file to .bak.
    Returns the merged list.
    """
    if not os.path.exists(step4_path):
        # Fresh supplemental — treat as initial
        return new_records

    # Read + backup
    with open(step4_path, "r", encoding="utf-8") as f:
        existing = json.load(f)

    shutil.copy(step4_path, step4_path + ".bak")

    old_downloaded = existing.get("downloaded", []) if isinstance(existing, dict) else existing

    # Prune stale paths
    alive = [r for r in old_downloaded if r.get("path") and os.path.exists(r["path"])]
    pruned = len(old_downloaded) - len(alive)

    # Build dedup key set
    def _key(r: Dict[str, Any]) -> tuple[str, str]:
        return (r.get("message_id", ""), str(r.get("attachment_part_id", "")))

    seen = {_key(r) for r in alive}
    fresh = [r for r in new_records if _key(r) not in seen]

    merged = alive + fresh

    # Atomic write back (preserve failed/skipped sections if present)
    payload: Any
    if isinstance(existing, dict):
        payload = dict(existing)
        payload["downloaded"] = merged
        payload["_merge_info"] = {
            "pruned_stale": pruned,
            "added_fresh": len(fresh),
            "merged_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    else:
        payload = merged

    tmp = step4_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, step4_path)
    return merged
