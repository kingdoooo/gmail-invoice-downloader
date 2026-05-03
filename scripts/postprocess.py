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
import shlex
import shutil
import sys
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.classify import classify_invoice
from core.llm_client import (
    LLMConfigError,
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
from core.validation import _parse_ocr_date, validate_ocr_plausibility


# =============================================================================
# Currency symbols (v5.8 Unit A.3)
# =============================================================================
# Maps ISO-4217 three-letter codes to display prefixes used in §IGNORED MD
# and the OpenClaw chat summary's IGNORED line. Unknown codes fall back to
# "{CODE} " so weird LLM output never silently becomes ¥.

_CURRENCY_SYMBOLS = {
    "CNY": "¥",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "HKD": "HK$",
}


def currency_symbol(code) -> str:
    """Return the display prefix for an ISO-4217 code.

    None / empty / lowercase are all normalized. Unknown codes return
    `"{UPPER} "` (trailing space preserved so the caller can concatenate
    directly with the amount).
    """
    key = (code or "CNY").upper()
    if key in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[key]
    return key + " "


# =============================================================================
# Category labels for filenames / CSV
# =============================================================================

CATEGORY_LABELS: Dict[str, str] = {
    "HOTEL":               "酒店",
    "HOTEL_INVOICE":       "酒店发票",
    "HOTEL_FOLIO":         "水单",
    "RIDEHAILING":         "网约车",
    "RIDEHAILING_INVOICE": "网约车发票",
    "RIDEHAILING_RECEIPT": "行程单",
    "TAXI":                "出租车发票",
    "TRAIN":               "火车票",
    "MEAL":                "餐饮",
    "MOBILE":              "话费",
    "TOLLS":               "通行费",
    "UNKNOWN":             "发票",
    "UNPARSED":            "⚠️ 需人工核查",
    # v5.7 Unit 3: IGNORED records get IGNORED_{domain}_{base}.pdf
    # filenames but CATEGORY_ORDER is deliberately NOT extended — IGNORED
    # records do not enter aggregation rows, so the default get(..., 50)
    # fallback suffices. Registering CATEGORY_ORDER["IGNORED"] would risk
    # interacting with TestCategoryConstants's UNPARSED=99 invariant.
    "IGNORED":             "已忽略",
}

# HOTEL / RIDEHAILING are v5.3 merged-row categories (invoice+folio collapsed
# to one row for finance). They sort before the per-document children so
# merged rows lead each section.
CATEGORY_ORDER: Dict[str, int] = {
    "HOTEL":               0,
    "HOTEL_FOLIO":         1,
    "HOTEL_INVOICE":       2,
    "RIDEHAILING":         3,
    "RIDEHAILING_INVOICE": 4,
    "RIDEHAILING_RECEIPT": 5,
    "MEAL":                6,
    "TRAIN":               7,
    "TAXI":                8,
    "MOBILE":              9,
    "TOLLS":               10,
    "UNKNOWN":             11,
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
# that already import postprocess don't need a second import line.
from invoice_helpers import make_unique_path  # noqa: E402


# =============================================================================
# Step 6.5a — analyze_pdf_batch
# =============================================================================

def analyze_pdf_batch(
    records: List[Dict[str, Any]],
    *,
    max_workers: Optional[int] = None,
    use_llm: bool = True,
    logger: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    """Run LLM OCR + classify + city + plausibility for each valid record.

    Args:
        records: list of download records with `path`, `valid`, optional
                 `internal_date` (email internalDate ms).
        max_workers: ThreadPoolExecutor parallelism.
                     If None: reads env LLM_OCR_CONCURRENCY, defaults to 5.
                     Explicit kwarg > env var > default.
                     Bedrock (default provider) has generous concurrency
                     budgets; 5 is safe. Anthropic tier-1 users should set
                     LLM_OCR_CONCURRENCY=2 (or a smaller number).
                     Invalid env var raises LLMConfigError (exit 3) rather
                     than silently defaulting.
        use_llm: pass False to skip LLM entirely (--no-llm mode).
        logger: optional object with .write or .print for progress output.

    Returns:
        {record_path: {ocr, category, city, error, used_fallback}}
    """
    # v5.5: resolve max_workers from explicit kwarg > env var > default.
    # Fail fast on invalid env — silent fallback would mask a config typo
    # (e.g. LLM_OCR_CONCURRENCY=10x) and quietly run at default concurrency.
    if max_workers is None:
        env = os.environ.get("LLM_OCR_CONCURRENCY", "").strip()
        if env:
            try:
                parsed = int(env)
                if parsed < 1:
                    raise ValueError(f"must be >= 1, got {parsed}")
                max_workers = parsed
            except ValueError as e:
                raise LLMConfigError(
                    f"invalid LLM_OCR_CONCURRENCY={env!r}: {e}"
                ) from None
        else:
            max_workers = 5

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

    # v5.7 Unit 3: IGNORED branch — non-reimbursable receipts (SaaS
    # subscriptions, marketing receipts) get IGNORED_{domain-label}_{base}.pdf
    # so the user can visually spot them in output_dir without them
    # polluting CSV/zip/missing.json.items[].
    if category == "IGNORED":
        sender_email = record.get("sender_email", "") or ""
        # Extract domain label: "billing@termius.com" → "termius"
        # Handle pathological "foo@bar@baz.com" by splitting on first '@'.
        after_at = sender_email.split("@", 1)[-1] if "@" in sender_email else sender_email
        domain_label = after_at.split(".")[0] or "unknown"
        sender_short = sanitize_filename(domain_label, max_len=20) or "unknown"

        base = os.path.basename(old_path)
        new_name = sanitize_filename(f"IGNORED_{sender_short}_{base}", max_len=200)
        if not new_name.endswith(".pdf"):
            new_name += ".pdf"
        new_path = make_unique_path(pdfs_dir, new_name)
        if new_path != old_path:
            try:
                os.rename(old_path, new_path)
                record["path"] = new_path
            except OSError as e:
                # Hard-disk / permission error: degrade the record through
                # the UNPARSED pipeline so it still reaches the user in
                # report / CSV / zip. Delegate to a recursive call with a
                # synthesized UNPARSED analysis so the file actually gets
                # the UNPARSED_ visible prefix AND the error text is
                # written to record["error"] where write_report_md reads
                # it. Writing to the local `analysis["error"]` would lose
                # the message because the caller does not read it back.
                err_text = f"IGNORED rename failed: {e}"
                record["error"] = err_text
                return rename_by_ocr(
                    record,
                    {"category": "UNPARSED", "error": err_text, "ocr": None},
                    pdfs_dir,
                )
        record["vendor_name"] = record.get("merchant") or "未知"
        record["transaction_date"] = record.get("date", "")
        return record

    # Happy path: {YYYYMMDD}_{vendor}_{label}.pdf
    # v5.5: HOTEL_FOLIO prefers departureDate over transactionDate. On
    # fresh OCR (post-v5.5 prompt) the two are equal for folios. On stale
    # OCR cache the preference keeps the filename tied to checkout, not
    # check-in, which matches the matcher's actual key.
    if category == "HOTEL_FOLIO":
        date_str = (
            normalize_date(ocr.get("departureDate"))
            or normalize_date(ocr.get("transactionDate"))
            or record.get("date", "")
        )
    else:
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
# Step 6.5 — OCR-content dedup
#
# The initial Gmail scan can attach the same invoice PDF to multiple messages,
# and --supplemental reruns can re-pull attachments the initial run already had.
# Both paths currently produce byte-identical or business-identical duplicates
# that land in pdfs/ with ` (1)` suffixes. Those duplicates get OCR'd,
# classified, and then the 1-to-1 matcher reports the losers as "unmatched"
# orphans. Collapse them here, BEFORE matching runs.
# =============================================================================

_DEDUP_KEYED_CATEGORIES = frozenset({
    "HOTEL_INVOICE", "MEAL", "MOBILE", "TOLLS",
    "RIDEHAILING_INVOICE", "TAXI", "TRAIN",
})
_NO_DEDUP_CATEGORIES = frozenset({"UNKNOWN", "UNPARSED"})


def _normalize_hotel_name(name: Optional[str]) -> str:
    if not name:
        return ""
    return re.sub(r"\s+", "", name).lower()


def _dedup_key_for(record: Dict[str, Any]) -> Optional[tuple]:
    """Return the OCR-business key for a record, or None if dedup shouldn't apply.

    Key format (category-specific):
      HOTEL_INVOICE / MEAL / MOBILE / TOLLS / RIDEHAILING_INVOICE / TAXI / TRAIN:
          ("inv", invoiceNo)                         — when OCR extracted invoiceNo
      HOTEL_FOLIO:
          ("folio:conf", confirmationNo)             — when confirmationNo present
          ("folio:dates", hotel, arrival, departure) — fallback when confirmationNo missing
                                                       (both arrivalDate AND departureDate
                                                        must be present; otherwise skip)
      otherwise: None (no business key; SHA256 fallback will run separately)
    """
    cat = record.get("category") or ""
    ocr = record.get("ocr") or {}

    if cat == "HOTEL_FOLIO":
        conf = ocr.get("confirmationNo")
        if conf:
            return ("folio:conf", str(conf))
        arrival = ocr.get("arrivalDate") or ocr.get("checkInDate")
        departure = ocr.get("departureDate") or ocr.get("checkOutDate") or ocr.get("checkoutDate")
        hotel = _normalize_hotel_name(ocr.get("hotelName") or ocr.get("sellerName") or ocr.get("vendorName"))
        if arrival and departure and hotel:
            return ("folio:dates", hotel, str(arrival), str(departure))
        return None

    if cat in _DEDUP_KEYED_CATEGORIES:
        inv_no = ocr.get("invoiceNo")
        if inv_no:
            return ("inv", str(inv_no))
        return None

    return None


def _sha256_of(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _dedup_by_ocr_business_key(
    downloaded: List[Dict[str, Any]],
    *,
    delete_losers: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Collapse records that share a business identifier (OCR-driven).

    Two passes:
      1. Business-key pass (_dedup_key_for) — invoiceNo / confirmationNo /
         (hotel, arrival, departure) fallback.
      2. SHA256 pass on the survivors — catches byte-identical forwarded-email
         copies that passed pass 1 (e.g. MEAL records where no invoiceNo was
         extracted and the upstream emails forwarded the same PDF).

    Tie-breaker: keep the entry with the shortest basename (no ` (1)` suffix).
    Ties beyond that: keep the earliest `internal_date`.

    When ``delete_losers`` is True, physically unlinks loser files from disk so
    later CSV/report/zip stages never see them. The returned ``removed`` list
    always contains the full record dict of each loser.
    """
    # Skip records that never entered matching (invalid / explicit no-dedup / no OCR)
    def _participates(r: Dict[str, Any]) -> bool:
        if not r.get("valid"):
            return False
        cat = r.get("category") or "UNPARSED"
        return cat not in _NO_DEDUP_CATEGORIES

    pass_through: List[Dict[str, Any]] = [r for r in downloaded if not _participates(r)]
    active: List[Dict[str, Any]] = [r for r in downloaded if _participates(r)]

    # Sorting helpers for deterministic tie-break
    def _sort_score(r: Dict[str, Any]) -> tuple:
        basename = Path(r.get("path") or "").name
        return (len(basename), r.get("internal_date") or "", basename)

    # -------- Pass 1: business key --------
    by_key: Dict[tuple, List[Dict[str, Any]]] = {}
    unkeyed: List[Dict[str, Any]] = []
    for r in active:
        k = _dedup_key_for(r)
        if k is None:
            unkeyed.append(r)
        else:
            by_key.setdefault(k, []).append(r)

    kept_p1: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    for k, group in by_key.items():
        group_sorted = sorted(group, key=_sort_score)
        kept_p1.append(group_sorted[0])
        removed.extend(group_sorted[1:])

    # -------- Pass 2: SHA256 on survivors + unkeyed --------
    pass2_input = kept_p1 + unkeyed
    by_sha: Dict[str, List[Dict[str, Any]]] = {}
    no_hash: List[Dict[str, Any]] = []
    for r in pass2_input:
        p = r.get("path")
        if not p:
            no_hash.append(r)
            continue
        sha = _sha256_of(p)
        if sha is None:
            no_hash.append(r)
        else:
            by_sha.setdefault(sha, []).append(r)

    kept_final: List[Dict[str, Any]] = list(no_hash)
    for _, group in by_sha.items():
        group_sorted = sorted(group, key=_sort_score)
        kept_final.append(group_sorted[0])
        removed.extend(group_sorted[1:])

    # Physical delete of losers
    if delete_losers:
        for r in removed:
            p = r.get("path")
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass  # surfacing a delete failure would bring down the pipeline; report instead

    # Rebuild final list preserving original pass-through ordering first,
    # then matched records in stable order
    # Active-side dedup complete; reassemble preserving original download order
    kept_ids = {id(r) for r in kept_final}
    pass_ids = {id(r) for r in pass_through}
    final = [r for r in downloaded if id(r) in kept_ids or id(r) in pass_ids]
    return final, removed


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
    # Collapse OCR-business duplicates BEFORE matching so the 1-to-1 matcher
    # never sees two records for the same invoice / folio.
    downloaded, dedup_removed = _dedup_by_ocr_business_key(downloaded)
    if dedup_removed:
        print(f"  dedup: removed {len(dedup_removed)} duplicate record(s)", flush=True)
        for r in dedup_removed:
            print(f"    - {Path(r.get('path') or '').name}", flush=True)

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
                # v5.5: expose folio arrival/departure on the P3 match
                # record so the report writer can render the OCR dates
                # reviewers actually care about (filename date may be
                # email-internalDate-derived, potentially weeks off).
                fol_rec = fol.get("_record") or {}
                fol_ocr = fol_rec.get("ocr") or {}
                tier3.append({
                    "invoice": inv, "folio": fol,
                    "match_type": "date_only (v5.2 fallback)",
                    "confidence": "low",
                    "folio_arrival_date": fol_ocr.get("arrivalDate"),
                    "folio_departure_date": (
                        fol_ocr.get("departureDate")
                        or fol_ocr.get("checkOutDate")
                    ),
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
        "dedup_removed": dedup_removed,
    }


# =============================================================================
# Step 7.5 — build_aggregation (single source of truth for CSV/MD/message)
# =============================================================================

# DEC-7: explicit whitelist. worst_of() raises on unknown values so a future
# matching.py addition (e.g. "medium") fails loud in tests instead of silently
# collapsing to "high".
VALID_CONFIDENCES = frozenset({"high", "low", "failed"})

# Rank map: higher rank = worse.
_CONFIDENCE_RANK: Dict[str, int] = {"high": 0, "low": 1, "failed": 2}


def worst_of(*values: str) -> str:
    """Return the worst confidence across ``values``.

    Raises ValueError if any value is not in VALID_CONFIDENCES.
    """
    if not values:
        raise ValueError("worst_of() requires at least one value")
    worst = values[0]
    for v in values:
        if v not in VALID_CONFIDENCES:
            raise ValueError(
                f"unknown confidence {v!r}; allowed: {sorted(VALID_CONFIDENCES)}"
            )
        if _CONFIDENCE_RANK[v] > _CONFIDENCE_RANK[worst]:
            worst = v
    return worst


@dataclass
class MergedRow:
    """One row in the aggregated summary.

    Consumed by write_summary_csv, write_report_md, print_openclaw_summary —
    all three writers read the same object so the rendered numbers never
    diverge.
    """
    category:     str                       # HOTEL / RIDEHAILING / HOTEL_INVOICE / ...
    date:         Optional[str]             # "YYYY-MM-DD" or None
    amount:       Optional[Decimal]         # None when unknown (not 0)
    vendor:       str                       # "—" when unknown
    primary_file: str                       # basename
    paired_file:  Optional[str] = None      # basename of partner PDF, if paired
    paired_kind:  Optional[str] = None      # "水单" / "行程单" / None
    confidence:   str = "high"              # ∈ VALID_CONFIDENCES
    remark_flags: List[str] = field(default_factory=list)


def _to_decimal(val: Any) -> Optional[Decimal]:
    """Convert to Decimal via the str() bridge to avoid binary float residue.

    Mirrors _to_float's None-safe semantics: None / "" / unconvertible → None.
    Used only inside build_aggregation — _to_float stays the project's sole
    numeric coercion entry point for matching inputs.
    """
    f = _to_float(val)
    if f is None:
        return None
    return Decimal(str(f))


def _confidence_for_record(record: Dict[str, Any]) -> str:
    """Derive row confidence from an OCR record, mirroring write_summary_csv's
    historical logic. UNPARSED / missing OCR → "failed".
    """
    ocr = record.get("ocr") or {}
    if record.get("category") == "UNPARSED" or not ocr:
        return "failed"
    if (
        ocr.get("_amountConfidence") == "low"
        or ocr.get("_dateConfidence") == "low"
        or ocr.get("_vendorNameInvalid")
    ):
        return "low"
    return "high"


def _collect_flags(ocr: Dict[str, Any]) -> List[str]:
    """Pull remark / warning bits from an OCR dict for the row's 备注 column."""
    bits: List[str] = []
    if ocr.get("remark"):
        bits.append(f"remark={ocr['remark']}")
    if ocr.get("confirmationNo"):
        bits.append(f"confNo={ocr['confirmationNo']}")
    if ocr.get("phoneNumber"):
        bits.append(f"phone={ocr['phoneNumber']}")
    if ocr.get("billingPeriod"):
        bits.append(f"period={ocr['billingPeriod']}")
    if ocr.get("tripCount"):
        bits.append(f"trips={ocr['tripCount']}")
    if ocr.get("trainNumber"):
        bits.append(
            f"{ocr.get('departureStation','?')}→{ocr.get('arrivalStation','?')} "
            f"车次{ocr['trainNumber']}"
        )
    if ocr.get("_amountConfidence") == "low":
        bits.append("⚠️金额可疑")
    if ocr.get("_dateConfidence") == "low":
        bits.append("⚠️日期异常")
    if ocr.get("_vendorNameInvalid"):
        bits.append("⚠️销售方未识别")
    return bits


def _amount_for_category(ocr: Dict[str, Any], category: str) -> Optional[Decimal]:
    """Category-specific amount extraction (mirrors legacy write_summary_csv rules)."""
    if category == "HOTEL_FOLIO":
        amt = _to_decimal(ocr.get("balance"))
        if amt is None:
            amt = _to_decimal(ocr.get("transactionAmount"))
        return amt
    if category == "RIDEHAILING_RECEIPT":
        amt = _to_decimal(ocr.get("totalAmount"))
        if amt is None:
            amt = _to_decimal(ocr.get("transactionAmount"))
        return amt
    return _to_decimal(ocr.get("transactionAmount"))


def build_aggregation(
    matching_result: Dict[str, Any],
    valid_records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Collapse matching_result into one aggregation dict consumed by every writer.

    Returns:
        {
            "rows":          List[MergedRow]  -- all rows, sorted (category, date)
            "subtotals":     Dict[cat, Decimal]
            "grand_total":   Decimal  (0.00 when nothing aggregates)
            "low_conf":      {"count": int, "amount": Decimal}
            "unmatched":     {hotel_invoices, hotel_folios, rh_invoices, rh_receipts}
            "voucher_count": int  -- rows excluding UNPARSED
        }

    Fail-fast on malformed matching_result: missing "hotel"/"ridehailing" keys
    raise KeyError. No defensive defaults — upstream bugs should not be masked.
    """
    # v5.7 Unit 3: Guard against silent regression — main() splits IGNORED
    # records out before calling build_aggregation. If any leak through,
    # aggregation completeness assertions later in the function will fire
    # from a confusing "accounted == len(valid_records)" mismatch; assert
    # here with a clearer message.
    assert not any(
        r.get("category") == "IGNORED"
        for r in valid_records
    ), "IGNORED leaked past main() split — Unit 3 filter broke"
    # DEC-2: scope rounding locally so build_aggregation can be called from
    # any context without mutating Decimal's global rounding mode.
    with localcontext() as ctx:
        ctx.rounding = ROUND_HALF_UP
        ctx.prec = 28

        rows: List[MergedRow] = []

        # -- Hotel matched pairs → HOTEL merged rows ------------------------
        hotel = matching_result["hotel"]
        for pair in hotel["matched"]:
            inv = pair["invoice"]
            fol = pair["folio"]
            inv_rec = inv["_record"]
            fol_rec = fol["_record"]
            inv_ocr = inv_rec.get("ocr") or {}

            amount = _to_decimal(inv.get("transactionAmount"))
            remark_flags = _collect_flags(inv_ocr)
            if amount is None:
                remark_flags.append("⚠️发票金额缺失")

            # P3 demotes the row to low even if invoice OCR is clean.
            p3_low = "low" if pair.get("confidence") == "low" else "high"
            conf = worst_of(_confidence_for_record(inv_rec), p3_low)

            rows.append(MergedRow(
                category="HOTEL",
                date=fol.get("checkOutDate") or fol.get("departureDate"),
                amount=amount,
                vendor=(
                    inv_ocr.get("vendorName")
                    or inv_rec.get("merchant")
                    or "—"
                ),
                primary_file=os.path.basename(inv_rec.get("path", "")),
                paired_file=os.path.basename(fol_rec.get("path", "")),
                paired_kind="水单",
                confidence=conf,
                remark_flags=remark_flags,
            ))

        # -- Ride-hailing matched pairs → RIDEHAILING merged rows ----------
        rh = matching_result["ridehailing"]
        for pair in rh["matched"]:
            inv = pair["invoice"]
            rec = pair["receipt"]
            inv_rec = inv["_record"]
            rec_rec = rec["_record"]
            inv_ocr = inv_rec.get("ocr") or {}

            amount = _to_decimal(inv.get("transactionAmount"))
            remark_flags = _collect_flags(inv_ocr)
            if amount is None:
                remark_flags.append("⚠️发票金额缺失")

            conf = _confidence_for_record(inv_rec)

            rows.append(MergedRow(
                category="RIDEHAILING",
                date=inv.get("transactionDate") or inv_rec.get("date"),
                amount=amount,
                vendor=(
                    inv_ocr.get("vendorName")
                    or inv_rec.get("merchant")
                    or "—"
                ),
                primary_file=os.path.basename(inv_rec.get("path", "")),
                paired_file=os.path.basename(rec_rec.get("path", "")),
                paired_kind="行程单",
                confidence=conf,
                remark_flags=remark_flags,
            ))

        # -- Unmatched per-document rows (keep original category labels) ----
        def _single_row(rec: Dict[str, Any], category: str) -> MergedRow:
            ocr = rec.get("ocr") or {}
            amount = _amount_for_category(ocr, category)
            remark_flags = _collect_flags(ocr)
            if amount is None and category not in {"UNPARSED", "UNKNOWN"}:
                remark_flags.append("⚠️发票金额缺失")
            return MergedRow(
                category=category,
                date=(
                    ocr.get("transactionDate")
                    or ocr.get("checkOutDate")
                    or ocr.get("departureDate")
                    or rec.get("date")
                ),
                amount=amount,
                vendor=(
                    ocr.get("vendorName")
                    or ocr.get("hotelName")
                    or rec.get("merchant")
                    or "—"
                ),
                primary_file=os.path.basename(rec.get("path", "")),
                confidence=_confidence_for_record(rec),
                remark_flags=remark_flags,
            )

        for inv in hotel["unmatched_invoices"]:
            rows.append(_single_row(inv["_record"], "HOTEL_INVOICE"))
        for fol in hotel["unmatched_folios"]:
            rows.append(_single_row(fol["_record"], "HOTEL_FOLIO"))
        for inv in rh["unmatched_invoices"]:
            rows.append(_single_row(inv["_record"], "RIDEHAILING_INVOICE"))
        for r in rh["unmatched_receipts"]:
            rows.append(_single_row(r["_record"], "RIDEHAILING_RECEIPT"))

        # -- Raw-record buckets (meal/train/taxi/mobile/tolls/unknown) ------
        for bucket, cat in [
            ("meal",    "MEAL"),
            ("train",   "TRAIN"),
            ("taxi",    "TAXI"),
            ("mobile",  "MOBILE"),
            ("tolls",   "TOLLS"),
            ("unknown", "UNKNOWN"),
        ]:
            for rec in matching_result.get(bucket, []):
                rows.append(_single_row(rec, cat))

        # -- Unparsed bucket -----------------------------------------------
        for rec in matching_result.get("unparsed", []):
            rows.append(_single_row(rec, "UNPARSED"))

        # Completeness: every valid_record must surface in exactly one row
        # (merged rows consume 2 records). Guards against silent record-loss
        # during future refactors.
        accounted = sum(2 if r.paired_file else 1 for r in rows)
        assert accounted == len(valid_records), (
            f"aggregation accounts for {accounted} records but received "
            f"{len(valid_records)} valid — someone dropped or double-counted "
            f"a record"
        )

        # -- Sort: category first, then date -------------------------------
        def _sort_key(r: MergedRow):
            return (
                CATEGORY_ORDER.get(r.category, 50),
                r.date or "99999999",
            )
        rows.sort(key=_sort_key)

        # -- Subtotals / grand total (None-safe) ---------------------------
        subtotals: Dict[str, Decimal] = {}
        for r in rows:
            if r.amount is None:
                continue
            subtotals[r.category] = (
                subtotals.get(r.category, Decimal("0")) + r.amount
            )
        subtotals = {
            cat: amt.quantize(Decimal("0.01"))
            for cat, amt in subtotals.items()
        }

        if subtotals:
            grand_total = sum(subtotals.values(), Decimal("0")).quantize(
                Decimal("0.01")
            )
        else:
            grand_total = Decimal("0.00")

        # -- Low-confidence count / amount ---------------------------------
        low_count = 0
        low_amount = Decimal("0")
        for r in rows:
            if r.confidence == "low":
                low_count += 1
                if r.amount is not None:
                    low_amount += r.amount
        low_amount = low_amount.quantize(Decimal("0.01"))

        # -- Unmatched counts ---------------------------------------------
        unmatched = {
            "hotel_invoices": len(hotel["unmatched_invoices"]),
            "hotel_folios":   len(hotel["unmatched_folios"]),
            "rh_invoices":    len(rh["unmatched_invoices"]),
            "rh_receipts":    len(rh["unmatched_receipts"]),
        }

        voucher_count = sum(1 for r in rows if r.category != "UNPARSED")

        return {
            "rows":          rows,
            "subtotals":     subtotals,
            "grand_total":   grand_total,
            "low_conf":      {"count": low_count, "amount": low_amount},
            "unmatched":     unmatched,
            "voucher_count": voucher_count,
        }


# =============================================================================
# Step 10 — print_openclaw_summary (stdout-only OpenClaw chat summary)
# =============================================================================

_MISSING_STATUSES = frozenset({"stop", "run_supplemental", "ask_user"})

# v5.6 Agent-facing sentinels. See SKILL.md § Presenting Results to the User
# and docs/brainstorms/2026-05-02-chat-message-and-attachments-sentinels-requirements.md
CHAT_MESSAGE_START_SENTINEL = "CHAT_MESSAGE_START"
CHAT_MESSAGE_END_SENTINEL = "CHAT_MESSAGE_END"
CHAT_ATTACHMENTS_PREFIX = "CHAT_ATTACHMENTS: "

_ATTACHMENT_CAPTIONS = {
    "zip": "报销包",
    "md":  "报告",
    "csv": "明细",
}


def print_openclaw_summary(
    aggregation: Dict[str, Any],
    *,
    output_dir: str,
    zip_path: Optional[str],
    csv_path: str,
    md_path: str,
    log_path: str,
    missing_status: str,
    date_range: Tuple[str, str],
    writer: Callable[[str], None] = print,
    ignored_count: int = 0,  # v5.7 Unit 4
) -> None:
    """Render a ≤20-line summary to stdout (and optionally run.log via writer).

    R15-R17 / R16a+R16b templates. ``writer`` defaults to ``print`` so the
    function works unaided; main() passes ``say`` to mirror the summary into
    ``run.log`` before ``log.close()``.

    ``zip_path=None`` degrades gracefully (DEC-6) when zip_output failed —
    the CSV/MD lines still render so the user isn't left without paths.

    Raises ValueError if ``missing_status`` is not one of
    ``_MISSING_STATUSES`` (DEC-7: fail-fast on unknown enum).
    """
    if missing_status not in _MISSING_STATUSES:
        raise ValueError(
            f"unknown missing_status {missing_status!r}; "
            f"allowed: {sorted(_MISSING_STATUSES)}"
        )

    # Agent contract: everything between START and END is the verbatim
    # user-facing summary. Emitted on every code path (R16a + R16b).
    writer(CHAT_MESSAGE_START_SENTINEL)

    unmatched = aggregation["unmatched"]
    voucher_count = aggregation["voucher_count"]
    low = aggregation["low_conf"]
    unmatched_any = any(v > 0 for v in unmatched.values())
    has_rows = bool(aggregation["rows"])

    # R16b: empty Gmail search — no rows, no unmatched warnings.
    if voucher_count == 0 and not unmatched_any and not has_rows:
        writer(
            f"ℹ️ 本次未下载到凭证 — 日期范围：{date_range[0]} → {date_range[1]}"
        )
        writer(
            "   可能原因：关键词未覆盖 / 日期区间无邮件 / "
            "learned_exclusions.json 过滤过严"
        )
        writer(f"   检查：{os.path.abspath(log_path)}")
        writer(CHAT_MESSAGE_END_SENTINEL)
        return

    # R16a: non-empty template.
    dagger = " †" if low["count"] > 0 else ""

    # 1. Title
    writer(f"📄 发票报销包 — {date_range[0]} → {date_range[1]}")
    # 2. Blank
    writer("")
    # 3. Grand total
    writer(
        f"✅ 共 {voucher_count} 份凭证，合计 "
        f"¥{aggregation['grand_total']:.2f}{dagger}"
    )
    # 4. Per-category breakdown (only categories with rows)
    per_cat_counts: Dict[str, int] = {}
    for r in aggregation["rows"]:
        per_cat_counts[r.category] = per_cat_counts.get(r.category, 0) + 1
    for cat in sorted(per_cat_counts.keys(),
                      key=lambda c: CATEGORY_ORDER.get(c, 50)):
        count = per_cat_counts[cat]
        subtotal = aggregation["subtotals"].get(cat)
        label = CATEGORY_LABELS.get(cat, cat)
        if subtotal is not None:
            writer(f"  • {label} {count} 份    ¥{subtotal:.2f}")
        else:
            # No amount (UNPARSED or None-amount rows) → warn instead
            writer(f"  • ⚠️ {label} {count} 份 — 需人工核查")
    # 5. Blank
    writer("")
    # 6. Unmatched warnings (only N>0)
    if unmatched["hotel_invoices"] > 0:
        writer(f"⚠️ {unmatched['hotel_invoices']} 张酒店发票无对应水单")
    if unmatched["hotel_folios"] > 0:
        writer(f"⚠️ {unmatched['hotel_folios']} 份水单无对应发票")
    if unmatched["rh_invoices"] > 0:
        writer(f"⚠️ {unmatched['rh_invoices']} 张网约车发票无行程单")
    if unmatched["rh_receipts"] > 0:
        writer(f"⚠️ {unmatched['rh_receipts']} 份行程单无发票")
    # 7. Low-confidence footnote
    if low["count"] > 0:
        writer(
            f"† 其中 {low['count']} 项金额存疑（可信度=low，合计 "
            f"¥{low['amount']:.2f}），请人工复核"
        )
    # 8. Blank
    writer("")
    # 9. Next action
    abs_md = os.path.abspath(md_path)
    if missing_status == "stop":
        writer("👉 下一步：可以提交报销 — 打开上面 zip")
    elif missing_status == "run_supplemental":
        abs_out = os.path.abspath(output_dir)
        writer("👉 下一步：建议补搜 —")
        writer(
            f"    python3 scripts/download-invoices.py --supplemental "
            f"--start {date_range[0]} --end {date_range[1]} "
            f"--output {shlex.quote(abs_out)}"
        )
    else:  # ask_user
        writer(f"👉 下一步：需人工核查 — 见 {abs_md} 末尾「⚠️ 需人工核查」区")
    # 9.5. v5.7 Unit 4: IGNORED count line (only if any were filtered)
    if ignored_count:
        writer(
            f"📭 已忽略 {ignored_count} 张非报销票据"
            f"（详见下载报告.md §已忽略的非报销票据）"
        )
    # 10. Blank
    writer("")
    # 11. Deliverables
    abs_csv = os.path.abspath(csv_path)
    if zip_path is None:
        writer("📦 报销包：未生成（打包失败，见 run.log 末尾）")
    else:
        abs_zip = os.path.abspath(zip_path)
        writer(f"📦 报销包（提交这个）: {abs_zip}")
    writer(f"  明细: {abs_csv}   |   报告: {abs_md}")
    writer("")
    writer("💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？")
    writer("   直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。")
    writer(CHAT_MESSAGE_END_SENTINEL)

    # Agent contract: declare deliverables for the current chat. Order is
    # zip → MD → CSV. When zip_output failed (DEC-6), the zip entry is
    # omitted but MD + CSV are still declared. R16b's early return skips
    # this block entirely — no attachments are announced on empty result.
    attachments = []
    if zip_path is not None:
        attachments.append({
            "path": os.path.abspath(zip_path),
            "caption": _ATTACHMENT_CAPTIONS["zip"],
        })
    attachments.append({
        "path": os.path.abspath(md_path),
        "caption": _ATTACHMENT_CAPTIONS["md"],
    })
    attachments.append({
        "path": os.path.abspath(csv_path),
        "caption": _ATTACHMENT_CAPTIONS["csv"],
    })
    writer(CHAT_ATTACHMENTS_PREFIX + json.dumps(
        {"files": attachments},
        ensure_ascii=False,
        separators=(",", ":"),
    ))


# =============================================================================
# Step 8a — write_summary_csv (UTF-8 BOM, None-safe)
# =============================================================================

CSV_COLUMNS = [
    "序号", "开票日期", "类别", "金额", "销售方",
    "备注", "主文件", "配对凭证", "数据可信度",
]


def write_summary_csv(path: str, aggregation: Dict[str, Any]) -> int:
    """Write 发票汇总.csv (UTF-8 BOM, Excel-compatible) from aggregation dict.

    Layout:
    - 9 columns (see CSV_COLUMNS): adds 主文件/配对凭证 split + moves to 9th col.
    - Detail rows sorted by (CATEGORY_ORDER, date) — category first.
    - Blank separator row, then one 小计 row per category (only categories that
      have a subtotal), then a 总计 row.
    - None amounts render as empty string so Excel SUM ignores them.
    - 序号 column on subtotal/total rows: "—" (tombstone, not a number).

    Returns count of detail rows written (excludes subtotal/total rows).
    """
    rows_out: List[List[Any]] = []
    for i, row in enumerate(aggregation["rows"], 1):
        paired_cell = (
            f"{row.paired_kind}: {row.paired_file}"
            if row.paired_file and row.paired_kind
            else ""
        )
        rows_out.append([
            i,
            row.date or "—",
            CATEGORY_LABELS.get(row.category, "发票"),
            f"{row.amount:.2f}" if row.amount is not None else "",
            row.vendor or "—",
            "; ".join(row.remark_flags) or "—",
            row.primary_file,
            paired_cell,
            row.confidence,
        ])

    # Subtotal + grand total rows. Only emit subtotals for categories that
    # actually have an aggregated amount (skip pure UNPARSED runs cleanly).
    subtotal_rows: List[List[Any]] = []
    for cat in sorted(aggregation["subtotals"].keys(),
                      key=lambda c: CATEGORY_ORDER.get(c, 50)):
        amt = aggregation["subtotals"][cat]
        subtotal_rows.append([
            "—", "", f"{CATEGORY_LABELS.get(cat, cat)} 小计",
            f"{amt:.2f}", "", "", "", "", "",
        ])
    total_row = [
        "—", "", "总计",
        f"{aggregation['grand_total']:.2f}",
        "", "", "", "", "",
    ]

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        w.writerows(rows_out)
        # Blank separator row (all 9 cells empty) between detail + summary.
        w.writerow([""] * len(CSV_COLUMNS))
        w.writerows(subtotal_rows)
        w.writerow(total_row)

    return len(rows_out)


# =============================================================================
# Step 8b — write_missing_json (schema v1.0)
# =============================================================================

MISSING_SCHEMA_VERSION = "1.0"
DEFAULT_ITERATION_CAP = 3
REASON_OUT_OF_RANGE = "business_date_out_of_range"


def _parse_cli_ymd(s: str) -> Optional[_dt.date]:
    """Convert CLI-format YYYY/MM/DD or YYYY-MM-DD to a date.

    Returns None on empty or unparseable input. Callers default-in items
    that can't be evaluated so we never silently drop them.
    """
    if not s:
        return None
    return _parse_ocr_date(s.replace("/", "-"))


def _is_out_of_range(business_date: str, run_start: str, run_end: str) -> bool:
    """True iff business_date is strictly outside [run_start, run_end).

    Boundary: start inclusive, end exclusive (matches Gmail `before:` semantics
    used by CLI --end). Default-in on any parse failure — we don't filter
    items we can't evaluate.
    """
    d = _parse_ocr_date(business_date) if business_date else None
    s = _parse_cli_ymd(run_start)
    e = _parse_cli_ymd(run_end)
    if d is None or s is None or e is None:
        return False
    return d < s or d >= e


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
    run_start_date: str = "",     # v5.5 — CLI-format YYYY/MM/DD
    run_end_date: str = "",       # v5.5 — CLI-format YYYY/MM/DD
) -> Dict[str, Any]:
    """Build missing.json from do_all_matching output and write to disk.

    Returns the dict that was written (for caller to use / log).

    Schema version 1.0:
      - schema_version, generated_at, iteration, iteration_cap
      - status: converged | needs_retry | max_iterations_reached | user_action_required
      - recommended_next_action: run_supplemental | stop | ask_user
      - convergence_hash: sha256(sorted needed_for keys)
      - items: list of missing artifacts with per-item search suggestions
      - out_of_range_items: additive v5.5 — items whose OCR business_date falls
        outside [run_start_date, run_end_date). Not counted toward status or
        convergence_hash — Agents skip these rather than chase into adjacent
        quarters.
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

    # v5.5 — route cross-quarter items to out_of_range_items[]
    # Business date by type (item.type describes what's MISSING; the
    # business date comes from what we HAVE):
    out_of_range_items: List[Dict[str, Any]] = []
    kept_items: List[Dict[str, Any]] = []
    for it in items:
        if it["type"] in (
            "hotel_folio",
            "hotel_invoice",
            "ridehailing_receipt",
            "ridehailing_invoice",
        ):
            bdate = it.get("expected_date")
        else:
            # extraction_failed / unknown_platform / unknown types never filtered
            bdate = None

        if bdate and _is_out_of_range(bdate, run_start_date, run_end_date):
            it2 = dict(it)
            it2["business_date"] = bdate
            it2["reason"] = REASON_OUT_OF_RANGE
            out_of_range_items.append(it2)
        else:
            kept_items.append(it)
    items = kept_items

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
        "out_of_range_items": out_of_range_items,   # v5.5 addition
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
    include_pdf_paths: Optional[set] = None,
) -> str:
    """Create 发票打包_YYYYMMDD-HHMMSS.zip from output_dir.

    - Allowlist: only .pdf, .md, .csv (internal state JSONs excluded)
    - Self-exclusion: previous 发票打包_*.zip files not embedded
    - Atomic write: .tmp then os.replace
    - Manifest check: at least 1 CSV + 1 report MD + N PDFs
    - Default dest_dir is the parent of output_dir (so the zip sits alongside it)

    ``include_pdf_paths`` (v5.7.1 fix): when provided, only PDFs whose absolute
    path is in the set are packaged. This prevents leftover PDFs from previous
    runs (files accumulated in ``output_dir/pdfs/`` across batches) from
    leaking into the deliverable zip. Callers pass the union of this run's
    matching_result + ignored_records + unparsed paths. When None, legacy
    behavior preserves (scan every .pdf in the tree, minus prefix filters) —
    keeps existing agent-contract tests and third-party tooling compatible.

    IGNORED_ and 发票打包_ prefix filters still apply even when
    ``include_pdf_paths`` is set (defense in depth).

    Returns the path of the created zip.
    """
    now = now or _dt.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    dest_dir = dest_dir or os.path.dirname(os.path.abspath(output_dir))
    zip_name = f"{ZIP_PREFIX}{stamp}.zip"
    final_path = os.path.join(dest_dir, zip_name)
    tmp_path = final_path + ".tmp"

    # Resolve whitelist to absolute paths for safe comparison against os.walk.
    whitelist_abs: Optional[set] = None
    if include_pdf_paths is not None:
        whitelist_abs = {os.path.abspath(p) for p in include_pdf_paths}

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
                # v5.7 Unit 4: IGNORED_ prefix files are non-reimbursable
                # receipts (SaaS subscriptions, marketing). They stay in
                # output_dir for the user's audit trail but don't enter
                # the deliverable zip. UNPARSED_ files still zip (user
                # needs to see failed-to-parse receipts).
                if fn.startswith("IGNORED_"):
                    continue
                # Refuse symlinks so an attacker-placed symlink inside output_dir
                # can't exfiltrate files from outside the tree via the zip.
                if os.path.islink(os.path.join(root, fn)):
                    continue
                fp = os.path.join(root, fn)
                # v5.7.1 fix: apply include whitelist only to PDFs. CSV/MD
                # are this-run artifacts by construction (freshly written by
                # write_report_md / write_summary_csv) — whitelisting them
                # would force callers to duplicate the writer paths.
                if suffix == ".pdf" and whitelist_abs is not None:
                    if os.path.abspath(fp) not in whitelist_abs:
                        continue
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

    # Second dedup pass: OCR-business key. A supplemental run can still pull a
    # different message_id whose attachment is business-identical to something
    # the initial run already stored (the same 瑞幸 invoice forwarded through
    # two emails, or Hilton re-sending the same folio). Collapse those too.
    merged, biz_removed = _dedup_by_ocr_business_key(merged)

    # Atomic write back (preserve failed/skipped sections if present)
    payload: Any
    if isinstance(existing, dict):
        payload = dict(existing)
        payload["downloaded"] = merged
        payload["_merge_info"] = {
            "pruned_stale": pruned,
            "added_fresh": len(fresh),
            "dedup_removed": len(biz_removed),
            "merged_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    else:
        payload = merged

    tmp = step4_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, step4_path)
    return merged
