#!/usr/bin/env python3
"""Offline replay: compare old vs new classify_invoice on cached OCR results.

Unit 1 Verification artifact (committed). Scans ~/.cache/gmail-invoice-downloader/
ocr/*.json, runs both the legacy classify (fallthrough → 'UNKNOWN', 1-field
narrow gate with balance) and the v5.7 classify (fallthrough → 'IGNORED',
>=2-of-4 narrow gate without balance), prints the diff set with PDF paths
resolved via sha256 reverse lookup against ~/invoices/**/pdfs/*.pdf.

Usage:
    python3 scripts/dev/replay_classify.py

Interpretation:
- empty diff → no behavior change on cached samples; keep script for next
  snapshot sync.
- diff contains HOTEL_FOLIO → IGNORED on legitimate folios → freeze those
  samples as pytest fixtures under tests/fixtures/ocr/legitimate_folios/
  and tighten narrow gate if needed.
- diff contains HOTEL_FOLIO → IGNORED on SaaS invoices (Termius / Anthropic
  / OpenRouter etc.) → expected; Unit 0 + Unit 1 worked.
"""

import argparse
import glob
import hashlib
import json
import os
import pathlib
import sys
from typing import Any, Dict, List

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from core.classify import (  # noqa: E402
    classify_invoice as classify_new,
    is_chinese_invoice_document,
    is_hotel_folio_by_doctype,
    is_hotel_folio_by_fields,
    is_hotel_service,
    is_meal_service,
    is_mobile_service,
    is_ridehailing_receipt,
    is_ridehailing_service,
    is_taxi_invoice_by_doctype,
    is_tolls_service,
    is_train_ticket,
)


def classify_legacy(invoice: Dict[str, Any]) -> str:
    """Snapshot of the pre-v5.7 classify_invoice (fallthrough = UNKNOWN,
    docType narrow gate disabled — just docType keyword match).

    Orchestration inlined so the replay runs against a known baseline
    even after the real classify.py's classify_invoice has moved on.
    Helper functions (is_hotel_folio_*, is_*_service, etc.) are imported
    live — if one of them tightens, the "legacy" replay moves with the
    helper, which is an accepted cost noted in the v5.7 plan. Do NOT
    import classify_invoice itself from core.classify here.
    """
    service_type = invoice.get('serviceType', '') or ''
    doc_type = invoice.get('docType', '') or ''
    invoice_code = invoice.get('invoiceCode', '') or ''
    vendor_name = invoice.get('vendorName', '') or ''
    tax_id = invoice.get('vendorTaxId', '') or ''
    is_chinese_invoice = invoice.get('isChineseInvoice')

    category = None
    if is_ridehailing_receipt(doc_type):
        category = 'RIDEHAILING_RECEIPT'
    if not category and is_taxi_invoice_by_doctype(doc_type):
        category = 'TAXI'
    if not category and is_train_ticket(doc_type):
        category = 'TRAIN'
    if not category and is_hotel_folio_by_fields(invoice):
        category = 'HOTEL_FOLIO'
    # Legacy: docType-only (no narrow gate)
    if not category and is_hotel_folio_by_doctype(doc_type):
        category = 'HOTEL_FOLIO'
    if not category and invoice_code and len(invoice_code) == 12 and invoice_code.isdigit():
        category = 'TAXI'
    if not category and is_hotel_service(service_type):
        category = 'HOTEL_INVOICE' if is_chinese_invoice_document(is_chinese_invoice, tax_id) else 'HOTEL_FOLIO'
    if not category and is_ridehailing_service(service_type):
        category = 'RIDEHAILING_INVOICE' if is_chinese_invoice_document(is_chinese_invoice, tax_id) else 'RIDEHAILING_RECEIPT'
    if not category and is_meal_service(service_type):
        category = 'MEAL'
    if not category and is_mobile_service(service_type):
        category = 'MOBILE'
    if not category and is_tolls_service(service_type, vendor_name):
        category = 'TOLLS'
    if not category:
        category = 'UNKNOWN'  # legacy fallthrough
    return category


def build_sha_lookup() -> Dict[str, List[str]]:
    """Scan ~/invoices/**/pdfs/*.pdf and build sha256[:16] -> [path] map.

    setdefault([]).append handles cross-quarter duplicate downloads
    (same PDF pulled in Q1 and Q2 batches).
    """
    sha_to_path: Dict[str, List[str]] = {}
    root = os.path.expanduser("~/invoices")
    if not os.path.exists(root):
        return sha_to_path
    for pdf in glob.glob(os.path.join(root, "**", "pdfs", "*.pdf"), recursive=True):
        try:
            with open(pdf, "rb") as f:
                sha16 = hashlib.sha256(f.read()).hexdigest()[:16]
            sha_to_path.setdefault(sha16, []).append(pdf)
        except OSError:
            continue
    return sha_to_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        default=os.path.expanduser("~/.cache/gmail-invoice-downloader/ocr"),
    )
    args = parser.parse_args()

    if not os.path.exists(args.cache_dir):
        print(f"Cache dir not found: {args.cache_dir}", file=sys.stderr)
        return 2

    print("Building sha256 → pdf_path lookup from ~/invoices/**/pdfs/*.pdf ...",
          file=sys.stderr)
    sha_to_path = build_sha_lookup()
    print(f"  indexed {len(sha_to_path)} unique PDFs", file=sys.stderr)

    cache_files = sorted(glob.glob(os.path.join(args.cache_dir, "*.json")))
    print(f"Replaying {len(cache_files)} OCR cache entries ...", file=sys.stderr)

    diffs: List[tuple] = []
    for cache_file in cache_files:
        sha16 = pathlib.Path(cache_file).stem
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        ocr = payload.get("ocr")
        if not ocr:
            continue
        old_cat = classify_legacy(ocr)
        new_cat = classify_new(ocr)
        if old_cat != new_cat:
            paths = sha_to_path.get(sha16, ["<orphan OCR cache (PDF not in ~/invoices)>"])
            diffs.append((old_cat, new_cat, sha16, paths))

    if not diffs:
        print("No classification diffs. Keeping replay script for next snapshot sync.")
        return 0

    diffs.sort(key=lambda d: (d[0], d[1], d[2]))
    print(f"\n{len(diffs)} classification diffs:\n")
    for old_cat, new_cat, sha16, paths in diffs:
        print(f"{old_cat} → {new_cat}  (sha={sha16})")
        for p in paths:
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
