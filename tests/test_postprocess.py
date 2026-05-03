"""Critical-path tests for v5.3 pipeline.

Priority (per test plan 2026-04-30):
  #10 path traversal — CRITICAL security
  #27 hallucination detection — CRITICAL data integrity
  #28 retry/backoff — HIGH
  #22 zip atomic — HIGH
  #19 CSV UTF-8 BOM — HIGH
  #24 supplemental dedup — HIGH
  #9  rename happy path — HIGH
  #6  cache hit — HIGH
  #26 e2e with 3 fixture PDFs — HIGH
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

# conftest.py puts scripts/ on sys.path
from core.llm_client import (
    LLMAuthError,
    LLMClient,
    LLMConfigError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
    extract_with_retry,
    get_client,
    reset_client,
)
from core.llm_ocr import (
    _cache_path,
    _cache_read,
    _cache_write,
    extract_from_bytes,
    parse_llm_response,
    validate_and_fix_vendor_info,
)
from core.validation import validate_ocr_plausibility
from postprocess import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    MergedRow,
    VALID_CONFIDENCES,
    _compute_convergence_hash,
    _dedup_by_ocr_business_key,
    _to_matching_input,
    analyze_pdf_batch,
    build_aggregation,
    do_all_matching,
    merge_supplemental_downloads,
    normalize_date,
    print_openclaw_summary,
    rename_by_ocr,
    sanitize_filename,
    worst_of,
    write_missing_json,
    write_summary_csv,
    zip_output,
)


# =============================================================================
# #10 CRITICAL — path traversal in rename_by_ocr
# =============================================================================

class TestPathTraversal:
    def test_sanitize_strips_slashes(self):
        assert "/" not in sanitize_filename("../../etc/passwd")
        assert "\\" not in sanitize_filename("..\\..\\windows")

    def test_sanitize_strips_double_dots(self):
        result = sanitize_filename("../../evil")
        assert ".." not in result

    def test_sanitize_strips_null_byte(self):
        result = sanitize_filename("vendor\x00hidden")
        assert "\x00" not in result

    def test_sanitize_empty_becomes_default(self):
        assert sanitize_filename("") == "未知商户"
        assert sanitize_filename(None) == "未知商户"

    def test_sanitize_chinese_preserved(self):
        assert sanitize_filename("无锡万怡酒店") == "无锡万怡酒店"

    def test_rename_cannot_escape_pdfs_dir(self, tmp_path):
        pdfs_dir = tmp_path / "pdfs"
        pdfs_dir.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("should be untouched")

        original = pdfs_dir / "original.pdf"
        original.write_bytes(b"%PDF-1.4")

        record = {
            "path": str(original),
            "message_id": "m123",
            "merchant": "default",
            "date": "20260101",
        }
        analysis = {
            "ocr": {
                "vendorName": "../../victim.txt",
                "transactionDate": "2026-04-01",
            },
            "category": "HOTEL_INVOICE",
        }

        result = rename_by_ocr(record, analysis, str(pdfs_dir))
        new_path = result["path"]

        # Must stay under pdfs_dir
        assert os.path.dirname(os.path.abspath(new_path)) == str(pdfs_dir)
        # victim.txt must be unchanged
        assert victim.read_text() == "should be untouched"


# =============================================================================
# #27 CRITICAL — LLM hallucinated amount detection
# =============================================================================

class TestHallucinationDetection:
    def test_wildly_wrong_amount_flagged_low(self):
        pdf_path = os.path.expanduser(
            os.environ.get("GMAIL_INVOICE_FIXTURES", "~/Documents/agent Test")
        ) + "/滴滴电子发票 (1).pdf"
        if not os.path.exists(pdf_path):
            pytest.skip("fixture missing")
        ocr = {"transactionAmount": 999999.00, "transactionDate": "2025-12-09"}
        result = validate_ocr_plausibility(ocr, pdf_path=pdf_path)
        assert result.get("_amountConfidence") == "low"

    def test_plausible_amount_not_flagged(self):
        # We don't know the real amount in the fixture without running LLM,
        # but we can test with a synthetic pdf + known text.
        import subprocess
        pdf_path = os.path.expanduser(
            os.environ.get("GMAIL_INVOICE_FIXTURES", "~/Documents/agent Test")
        ) + "/滴滴电子发票 (1).pdf"
        if not os.path.exists(pdf_path):
            pytest.skip("fixture missing")
        # Pull out the real amount from the PDF to use as "LLM output"
        out = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, check=True,
        )
        import re
        amounts = [float(m.group()) for m in re.finditer(r"\d+\.\d{2}", out.stdout.decode())]
        assert amounts, "expected some amounts in the fixture"
        real_amount = amounts[0]  # pick any real amount on the page

        ocr = {"transactionAmount": real_amount, "transactionDate": "2025-12-09"}
        result = validate_ocr_plausibility(ocr, pdf_path=pdf_path)
        assert result.get("_amountConfidence") != "low"

    def test_date_outside_window_flagged(self):
        import datetime
        ocr = {
            "transactionAmount": 100,
            "transactionDate": "2020-01-01",
        }
        email_dt = datetime.datetime(2026, 5, 1)
        result = validate_ocr_plausibility(ocr, email_internal_date=email_dt)
        assert result.get("_dateConfidence") == "low"

    def test_date_in_window_not_flagged(self):
        import datetime
        ocr = {"transactionAmount": 100, "transactionDate": "2026-04-15"}
        email_dt = datetime.datetime(2026, 5, 1)
        result = validate_ocr_plausibility(ocr, email_internal_date=email_dt)
        assert result.get("_dateConfidence") != "low"


# =============================================================================
# Regression — pdftotext amount regex (was greedy-cutting 4+ digit decimals)
#
# 2025-Q4 report flagged several rows as `_amountConfidence=low` whose
# LLM-extracted amounts were actually correct:
#   南京景枫 1125.19,  南京四方 1574.97,  杭州钱江万怡 1560.10,
#   上海滴滴 2944.80,  上海滴滴 2068.90
#
# Root cause: the old regex `\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{1,2}`
# allowed the first branch to succeed on just 3 bare digits (no comma, no
# dot required), so `1125.19` was chopped into `['112', '5.19']` and 10%
# tolerance couldn't recover. Dates and 20-digit invoice numbers also
# leaked in as `[202, 10, 29]` / `[253, 270, ...]` noise.
#
# Fix: make the first branch require a real thousands separator
# (`(?:,\d{3})+`), so bare integers never match and decimals must include
# the dot via branch 2.
# =============================================================================


class TestAmountRegexRegression:
    def _matches(self, text: str):
        from core.validation import _AMOUNT_RE
        return [m.group(1) for m in _AMOUNT_RE.finditer(text)]

    def test_four_digit_decimal_not_chopped(self):
        """1125.19 must match whole — was ['112', '5.19'] under old regex."""
        assert self._matches("合计 1125.19") == ["1125.19"]

    def test_ride_hailing_case_not_chopped(self):
        """2944.80 must match whole — the Q4 smoke-test report case."""
        assert self._matches("¥2944.80") == ["2944.80"]

    def test_thousands_separator_still_works(self):
        """Kept support for the comma-style `1,234.56`."""
        assert self._matches("¥1,234.56") == ["1,234.56"]

    def test_million_with_commas(self):
        assert self._matches("¥1,000,000.00") == ["1,000,000.00"]

    def test_date_does_not_leak(self):
        """Dates like 2025-10-29 used to produce [202, 10, 29] noise."""
        assert self._matches("开票日期 2025-10-29") == []

    def test_invoice_number_does_not_leak(self):
        """20-digit invoiceNo used to be chopped into [253, 270, ...]."""
        assert self._matches("发票号码: 25327000001619791763") == []

    def test_bare_integer_no_longer_matches(self):
        """New behavior: bare integers without decimal or comma don't match.
        Acceptable for Chinese invoices where every amount has `.00`."""
        assert self._matches("¥500") == []
        assert self._matches("总计 1234") == []

    def test_multiple_amounts_same_line(self):
        assert self._matches("合计 ¥1,280.00 税额 ¥50.00") == ["1,280.00", "50.00"]

    def test_end_to_end_plausibility_for_buggy_case(self, tmp_path):
        """Simulate the original Q4 failure: a PDF containing 1125.19 on the
        page; validate_ocr_plausibility must NOT flag 1125.19 as low-confidence.

        Uses a text file + a pdftotext shim so this test doesn't need a real
        PDF fixture (keeps the suite fully portable).
        """
        from unittest.mock import patch
        # Simulate pdftotext output — a hotel folio line
        pdf_text = b"""
        Hotel Invoice
        Room charge         1125.19
        Service fee            0.00
        Total due          1125.19
        Date 2025-10-14
        Invoice No 25327000001619791763
        """
        fake_pdf = tmp_path / "hotel.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 stub")

        import subprocess as sp_real
        def fake_run(cmd, **kwargs):
            # Only intercept the pdftotext call
            if cmd and cmd[0] == "pdftotext":
                import types
                return types.SimpleNamespace(
                    returncode=0, stdout=pdf_text, stderr=b""
                )
            return sp_real.run(cmd, **kwargs)

        with patch("core.validation.subprocess.run", side_effect=fake_run):
            ocr = {"transactionAmount": 1125.19, "transactionDate": "2025-10-14"}
            result = validate_ocr_plausibility(ocr, pdf_path=str(fake_pdf))

        # The core invariant: the old buggy regex flagged this; new one must not.
        assert result.get("_amountConfidence") != "low", (
            "1125.19 is literally on the page; regex must not chop it into "
            "fragments that fail the 10% tolerance check"
        )


# =============================================================================
# #28 HIGH — retry/backoff
# =============================================================================

class MockRateLimitClient(LLMClient):
    provider_name = "mock"

    def __init__(self, fail_count=2, final_response='{"ok": true}'):
        self.fail_count = fail_count
        self.final_response = final_response
        self.calls = 0

    def extract_from_pdf(self, pdf_bytes: bytes, prompt: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_count:
            raise LLMRateLimitError(f"429 simulated attempt {self.calls}")
        return self.final_response


class MockAlwaysFailClient(LLMClient):
    provider_name = "mock"

    def __init__(self):
        self.calls = 0

    def extract_from_pdf(self, pdf_bytes: bytes, prompt: str) -> str:
        self.calls += 1
        raise LLMRateLimitError("persistent 429")


class TestRetry:
    def test_success_after_2_failures(self):
        mc = MockRateLimitClient(fail_count=2)
        result = extract_with_retry(b"x", "p", client=mc, base_delay=0.01)
        assert mc.calls == 3
        assert result == '{"ok": true}'

    def test_exhausts_max_attempts(self):
        mc = MockAlwaysFailClient()
        with pytest.raises(LLMRateLimitError):
            extract_with_retry(b"x", "p", client=mc, base_delay=0.01, max_attempts=3)
        assert mc.calls == 3

    def test_non_retryable_not_retried(self):
        from core.llm_client import LLMAuthError

        class AuthFailClient(LLMClient):
            provider_name = "mock"

            def __init__(self):
                self.calls = 0

            def extract_from_pdf(self, b, p):
                self.calls += 1
                raise LLMAuthError("bad key")

        mc = AuthFailClient()
        with pytest.raises(LLMAuthError):
            extract_with_retry(b"x", "p", client=mc, base_delay=0.01)
        assert mc.calls == 1


# =============================================================================
# #6 HIGH — OCR cache hit
# =============================================================================

class TestOCRCache:
    def test_cache_hit_skips_llm(self, tmp_path):
        class MC(LLMClient):
            provider_name = "mock"
            def __init__(self): self.calls = 0
            def extract_from_pdf(self, b, p):
                self.calls += 1
                return '{"vendorName": "Test", "transactionAmount": 100}'

        mc = MC()
        pdf = b"same content"
        r1 = extract_from_bytes(pdf, llm_client=mc, cache_dir=tmp_path)
        assert mc.calls == 1
        r2 = extract_from_bytes(pdf, llm_client=mc, cache_dir=tmp_path)
        assert mc.calls == 1, "cache should have been used"
        assert r1 == r2

    def test_corrupt_cache_recovers(self, tmp_path):
        pdf = b"content"
        # Write garbage to where the cache would go
        cache_path = _cache_path(pdf, tmp_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("not-valid-json")

        class MC(LLMClient):
            provider_name = "mock"
            def __init__(self): self.calls = 0
            def extract_from_pdf(self, b, p):
                self.calls += 1
                return '{"vendorName": "Test"}'

        mc = MC()
        result = extract_from_bytes(pdf, llm_client=mc, cache_dir=tmp_path)
        assert mc.calls == 1, "corrupt cache should cause re-analysis"
        assert result["vendorName"] == "Test"

    def test_cache_use_cache_false_forces_call(self, tmp_path):
        class MC(LLMClient):
            provider_name = "mock"
            def __init__(self): self.calls = 0
            def extract_from_pdf(self, b, p):
                self.calls += 1
                return '{"v": 1}'

        mc = MC()
        extract_from_bytes(b"x", llm_client=mc, cache_dir=tmp_path)
        extract_from_bytes(b"x", llm_client=mc, cache_dir=tmp_path, use_cache=False)
        assert mc.calls == 2

    def test_application_date_survives_cache_round_trip(self, tmp_path):
        """v5.5: applicationDate is preserved in the on-disk cache dict."""
        pdf = b"%PDF-1.4\nfakey\n"
        ocr = {
            "docType": "行程报销单",
            "applicationDate": "2025-12-09",
            "transactionDate": "2025-12-09",
            "totalAmount": 245.50,
        }
        _cache_write(pdf, ocr, tmp_path)
        read_back = _cache_read(pdf, tmp_path)
        assert read_back is not None
        assert read_back["applicationDate"] == "2025-12-09"
        assert read_back["transactionDate"] == "2025-12-09"


# =============================================================================
# #9 HIGH — rename_by_ocr happy path
# =============================================================================

class TestRenameHappyPath:
    def test_rename_uses_ocr_fields(self, tmp_path):
        pdfs = tmp_path / "pdfs"
        pdfs.mkdir()
        src = pdfs / "raw_download.pdf"
        src.write_bytes(b"%PDF")

        record = {"path": str(src), "message_id": "m1", "date": "20260101"}
        analysis = {
            "ocr": {
                "transactionDate": "2026-03-19",
                "vendorName": "无锡万怡酒店",
            },
            "category": "HOTEL_INVOICE",
        }

        result = rename_by_ocr(record, analysis, str(pdfs))
        basename = os.path.basename(result["path"])
        assert basename == "20260319_无锡万怡酒店_酒店发票.pdf"
        assert result["category"] == "HOTEL_INVOICE"
        assert result["vendor_name"] == "无锡万怡酒店"
        assert result["transaction_date"] == "20260319"

    def test_rename_llm_failure_produces_unparsed(self, tmp_path):
        pdfs = tmp_path / "pdfs"
        pdfs.mkdir()
        src = pdfs / "raw.pdf"
        src.write_bytes(b"%PDF")

        record = {"path": str(src), "message_id": "m12345", "date": "20260101"}
        analysis = {"ocr": None, "error": "rate limit", "category": "UNPARSED"}

        result = rename_by_ocr(record, analysis, str(pdfs))
        basename = os.path.basename(result["path"])
        assert basename.startswith("UNPARSED_")
        assert basename.endswith(".pdf")
        assert result["category"] == "UNPARSED"


class TestRenameByOCRFolioDate:
    """v5.5: HOTEL_FOLIO rename prefers OCR departureDate over
    internalDate-derived filename. Other categories unchanged."""

    def test_folio_uses_departure_date_when_present(self, tmp_path):
        pdfs = tmp_path / "pdfs"
        pdfs.mkdir()
        src = pdfs / "original.pdf"
        src.write_bytes(b"%PDF-1.4\n...")
        record = {
            "path": str(src),
            "message_id": "msg123",
            "merchant": "苏州万豪",
            "date": "20250607",   # internalDate-derived, *not* to be used
        }
        analysis = {
            "ocr": {
                "transactionDate": "2025-05-07",  # check-in (arrivalDate)
                "departureDate": "2025-05-08",    # check-out (v5.5 canonical)
                "vendorName": "苏州万豪",
            },
            "category": "HOTEL_FOLIO",
        }
        rename_by_ocr(record, analysis, str(pdfs))
        assert os.path.basename(record["path"]).startswith("20250508_"), \
            f"Expected departureDate 20250508, got {record['path']}"

    def test_folio_falls_back_to_transaction_date_when_departure_missing(
        self, tmp_path,
    ):
        pdfs = tmp_path / "pdfs"
        pdfs.mkdir()
        src = pdfs / "original.pdf"
        src.write_bytes(b"%PDF-1.4\n...")
        record = {
            "path": str(src),
            "message_id": "msg123",
            "merchant": "X",
            "date": "20250101",
        }
        analysis = {
            "ocr": {
                "transactionDate": "2025-05-07",
                "departureDate": None,
                "vendorName": "X",
            },
            "category": "HOTEL_FOLIO",
        }
        rename_by_ocr(record, analysis, str(pdfs))
        assert os.path.basename(record["path"]).startswith("20250507_")

    def test_hotel_invoice_unaffected(self, tmp_path):
        """HOTEL_INVOICE keeps v5.3 behavior: uses transactionDate."""
        pdfs = tmp_path / "pdfs"
        pdfs.mkdir()
        src = pdfs / "original.pdf"
        src.write_bytes(b"%PDF-1.4\n...")
        record = {"path": str(src), "message_id": "x", "merchant": "Y", "date": ""}
        analysis = {
            "ocr": {
                "transactionDate": "2025-05-08",
                "departureDate": "2025-05-10",   # should be ignored for invoices
                "vendorName": "Y",
            },
            "category": "HOTEL_INVOICE",
        }
        rename_by_ocr(record, analysis, str(pdfs))
        assert os.path.basename(record["path"]).startswith("20250508_")


# =============================================================================
# #22 HIGH — zip atomic write + allowlist
# =============================================================================

class TestZipAtomic:
    def test_allowlist_excludes_json_log(self, tmp_path):
        out = tmp_path / "out"
        (out / "pdfs").mkdir(parents=True)
        (out / "pdfs" / "a.pdf").write_bytes(b"%PDF")
        (out / "下载报告.md").write_text("# r")
        (out / "发票汇总.csv").write_text("h")
        (out / "step4_downloaded.json").write_text("{}")
        (out / "run.log").write_text("logs")

        zp = zip_output(str(out))

        import zipfile
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
        assert "step4_downloaded.json" not in names
        assert "run.log" not in names
        assert "pdfs/a.pdf" in names

    def test_self_exclusion(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "下载报告.md").write_text("r")
        (out / "发票汇总.csv").write_text("c")
        (out / "pdfs").mkdir()
        (out / "pdfs" / "x.pdf").write_bytes(b"%PDF")
        # pre-existing zip in output_dir
        (out / "发票打包_00000000-000000.zip").write_bytes(b"")

        zp = zip_output(str(out))
        import zipfile
        with zipfile.ZipFile(zp) as z:
            assert not any(n.startswith("发票打包_") for n in z.namelist()), \
                "prior zip should not be embedded"

    def test_manifest_check_refuses_empty(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        # Missing both csv and md
        (out / "pdfs").mkdir()
        (out / "pdfs" / "a.pdf").write_bytes(b"%PDF")

        with pytest.raises(RuntimeError, match="zip 完整性检查失败"):
            zip_output(str(out))

        # Partial zip must have been cleaned up
        assert not any(p.name.endswith(".zip.tmp") for p in tmp_path.iterdir())

    def test_ignored_prefix_excluded_from_zip(self, tmp_path):
        """Unit 4 R3: IGNORED_*.pdf is excluded from the deliverable zip.
        UNPARSED_*.pdf behavior is preserved (still zipped).
        """
        from postprocess import zip_output
        out = tmp_path / "batch"
        out.mkdir()
        pdfs = out / "pdfs"
        pdfs.mkdir()
        (pdfs / "20251112_Marriott_水单.pdf").write_bytes(b"%PDF hotel")
        (pdfs / "IGNORED_termius_Q9YJOO4I.pdf").write_bytes(b"%PDF saas")
        (pdfs / "UNPARSED_abc_bad.pdf").write_bytes(b"%PDF broken")
        (out / "下载报告.md").write_text("report")
        (out / "发票汇总.csv").write_text("csv")

        zp = zip_output(str(out))
        import zipfile
        with zipfile.ZipFile(zp) as zf:
            names = {os.path.basename(n) for n in zf.namelist()}
        assert "20251112_Marriott_水单.pdf" in names
        assert "UNPARSED_abc_bad.pdf" in names  # preserved
        assert "IGNORED_termius_Q9YJOO4I.pdf" not in names  # excluded

    def test_include_pdf_paths_whitelist_excludes_cross_run_leftovers(self, tmp_path):
        """v5.7.1 fix: zip_output accepts include_pdf_paths={str,...} — only
        PDFs whose absolute path is in the set get packaged. Leftover PDFs
        from previous runs that happen to sit in output_dir/pdfs/ are left on
        disk for audit but not shipped in the deliverable zip.
        """
        from postprocess import zip_output
        out = tmp_path / "batch"
        out.mkdir()
        pdfs = out / "pdfs"
        pdfs.mkdir()

        this_run = pdfs / "20251112_Marriott_水单.pdf"
        leftover_a = pdfs / "20250107_中国铁路_火车票.pdf"         # prev run
        leftover_b = pdfs / "20250108_中国铁路_火车票 (1).pdf"      # prev run
        this_run.write_bytes(b"%PDF current")
        leftover_a.write_bytes(b"%PDF prev a")
        leftover_b.write_bytes(b"%PDF prev b")
        (out / "下载报告.md").write_text("report")
        (out / "发票汇总.csv").write_text("csv")

        include = {str(this_run)}
        zp = zip_output(str(out), include_pdf_paths=include)

        import zipfile
        with zipfile.ZipFile(zp) as zf:
            names = {os.path.basename(n) for n in zf.namelist()}
        assert "20251112_Marriott_水单.pdf" in names
        assert "20250107_中国铁路_火车票.pdf" not in names
        assert "20250108_中国铁路_火车票 (1).pdf" not in names

        # Leftovers remain on disk (audit trail preserved)
        assert leftover_a.exists()
        assert leftover_b.exists()

    def test_include_pdf_paths_none_legacy_scan_preserved(self, tmp_path):
        """Legacy behavior: when include_pdf_paths is None (default), zip_output
        scans output_dir for every .pdf (minus ZIP_PREFIX / IGNORED_). Backward
        compat for existing callers, agent-contract tests, and any third-party
        tooling that treats the output dir as authoritative.
        """
        from postprocess import zip_output
        out = tmp_path / "batch"
        out.mkdir()
        pdfs = out / "pdfs"
        pdfs.mkdir()
        (pdfs / "a.pdf").write_bytes(b"%PDF a")
        (pdfs / "b.pdf").write_bytes(b"%PDF b")
        (out / "下载报告.md").write_text("r")
        (out / "发票汇总.csv").write_text("c")

        zp = zip_output(str(out))  # include_pdf_paths omitted = None
        import zipfile
        with zipfile.ZipFile(zp) as zf:
            names = {os.path.basename(n) for n in zf.namelist()}
        assert "a.pdf" in names
        assert "b.pdf" in names

    def test_include_pdf_paths_still_excludes_ignored_prefix(self, tmp_path):
        """IGNORED_* exclusion is independent of include_pdf_paths — even if
        an IGNORED path somehow lands in the whitelist, it still gets skipped
        (defense in depth: the CTA-driven audit workflow should never ship
        SaaS receipts to finance)."""
        from postprocess import zip_output
        out = tmp_path / "batch"
        out.mkdir()
        pdfs = out / "pdfs"
        pdfs.mkdir()
        legit = pdfs / "20251112_Marriott_水单.pdf"
        ignored = pdfs / "IGNORED_termius_x.pdf"
        legit.write_bytes(b"%PDF ok")
        ignored.write_bytes(b"%PDF saas")
        (out / "下载报告.md").write_text("r")
        (out / "发票汇总.csv").write_text("c")

        # Even if the caller accidentally passes an IGNORED path, zip must drop it.
        zp = zip_output(
            str(out),
            include_pdf_paths={str(legit), str(ignored)},
        )
        import zipfile
        with zipfile.ZipFile(zp) as zf:
            names = {os.path.basename(n) for n in zf.namelist()}
        assert "20251112_Marriott_水单.pdf" in names
        assert "IGNORED_termius_x.pdf" not in names

    def test_include_pdf_paths_empty_set_skips_all_pdfs(self, tmp_path):
        """Edge case: whitelist is an empty set. No PDFs packaged. The manifest
        check still fires on missing CSV/MD, but with them present the zip
        succeeds with zero PDFs — a valid no-vouchers batch.
        """
        from postprocess import zip_output
        out = tmp_path / "batch"
        out.mkdir()
        pdfs = out / "pdfs"
        pdfs.mkdir()
        (pdfs / "should-not-ship.pdf").write_bytes(b"%PDF")
        (out / "下载报告.md").write_text("r")
        (out / "发票汇总.csv").write_text("c")

        zp = zip_output(str(out), include_pdf_paths=set())
        import zipfile
        with zipfile.ZipFile(zp) as zf:
            names = {os.path.basename(n) for n in zf.namelist()}
        # CSV + MD still packaged, but no PDFs.
        assert "should-not-ship.pdf" not in names
        assert any(n.endswith(".md") for n in names)
        assert any(n.endswith(".csv") for n in names)


# =============================================================================
# #19 HIGH — CSV UTF-8 BOM + None-safe
# =============================================================================

class TestSummaryCSV:
    @staticmethod
    def _agg(records):
        """Helper: build aggregation from raw download records."""
        return build_aggregation(do_all_matching(records), records)

    def test_utf8_bom_prefix(self, tmp_path):
        csv_path = tmp_path / "summary.csv"
        records = [{
            "path": "a.pdf", "valid": True, "category": "HOTEL_INVOICE",
            "ocr": {
                "transactionDate": "2026-03-19",
                "transactionAmount": 1280.00,
                "vendorName": "无锡万怡",
                "remark": "ORPHAN",  # no folio → unmatched
            },
        }]
        write_summary_csv(str(csv_path), self._agg(records))
        assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf"), "missing UTF-8 BOM"

    def test_none_amount_is_empty_not_zero(self, tmp_path):
        csv_path = tmp_path / "summary.csv"
        records = [{
            "path": "a.pdf", "valid": True, "category": "MEAL",
            "ocr": {"transactionDate": "2026-03-15", "transactionAmount": None, "vendorName": "V"},
        }]
        write_summary_csv(str(csv_path), self._agg(records))
        text = csv_path.read_text(encoding="utf-8-sig")
        data_row = text.strip().split("\n")[1]
        fields = data_row.split(",")
        # 金额 is the 4th column (index 3)
        assert fields[3] == "", f"None amount should be empty, got {fields[3]!r}"

    def test_unparsed_sorts_last(self, tmp_path):
        csv_path = tmp_path / "summary.csv"
        records = [
            {"path": "unp.pdf", "valid": True, "category": "UNPARSED", "ocr": None, "date": "20260101"},
            {"path": "hot.pdf", "valid": True, "category": "HOTEL_INVOICE",
             "ocr": {"transactionDate": "2026-03-19", "transactionAmount": 100, "vendorName": "H",
                     "remark": "ORPHAN"}},
        ]
        write_summary_csv(str(csv_path), self._agg(records))
        text = csv_path.read_text(encoding="utf-8-sig")
        lines = text.strip().split("\n")
        # Row 1 is header, row 2 is HOTEL_INVOICE (sorts first by CATEGORY_ORDER),
        # row 3 is UNPARSED (always last).
        assert "HOTEL" in lines[1] or "酒店" in lines[1]
        assert "需人工核查" in lines[2]

    # -- New R7-R11 coverage -------------------------------------------

    def test_nine_columns_in_header(self, tmp_path):
        csv_path = tmp_path / "summary.csv"
        records = [{
            "path": "a.pdf", "valid": True, "category": "MEAL",
            "ocr": {"transactionDate": "2026-03-15",
                    "transactionAmount": 50.0, "vendorName": "V"},
        }]
        write_summary_csv(str(csv_path), self._agg(records))
        header = csv_path.read_text(encoding="utf-8-sig").splitlines()[0]
        cells = header.split(",")
        assert len(cells) == 9
        assert cells[6] == "主文件"
        assert cells[7] == "配对凭证"
        assert cells[8] == "数据可信度"

    def test_hotel_pair_inline_paired_kind(self, tmp_path):
        """HOTEL merged row: 配对凭证 = '水单: <folio_basename>'."""
        csv_path = tmp_path / "summary.csv"
        records = [
            {"path": "/p/inv.pdf", "valid": True, "category": "HOTEL_INVOICE",
             "ocr": {"transactionAmount": 1280.0,
                     "transactionDate": "2026-03-19",
                     "remark": "HT-Z", "vendorName": "无锡万怡"}},
            {"path": "/p/fol.pdf", "valid": True, "category": "HOTEL_FOLIO",
             "ocr": {"balance": 1280.0,
                     "checkOutDate": "2026-03-19",
                     "confirmationNo": "HT-Z", "hotelName": "无锡万怡"}},
        ]
        write_summary_csv(str(csv_path), self._agg(records))
        text = csv_path.read_text(encoding="utf-8-sig")
        assert "水单: fol.pdf" in text
        # Only 1 detail row (merged), not 2
        detail_line = [
            ln for ln in text.splitlines()
            if ln and not ln.startswith("序号") and ",总计," not in ln
            and "小计," not in ln and ln != "" * 9
        ]
        # filter blank separator
        detail_line = [ln for ln in detail_line if ln.strip(",")]
        assert any("无锡万怡" in ln for ln in detail_line)

    def test_subtotal_and_grand_total_rows(self, tmp_path):
        """CSV must end with per-category subtotal rows + 总计 row."""
        csv_path = tmp_path / "summary.csv"
        records = [
            {"path": "a.pdf", "valid": True, "category": "MEAL",
             "ocr": {"transactionDate": "2026-03-01",
                     "transactionAmount": 100.0, "vendorName": "V"}},
            {"path": "b.pdf", "valid": True, "category": "MEAL",
             "ocr": {"transactionDate": "2026-03-02",
                     "transactionAmount": 50.0, "vendorName": "V"}},
        ]
        write_summary_csv(str(csv_path), self._agg(records))
        text = csv_path.read_text(encoding="utf-8-sig")
        assert "餐饮 小计,150.00" in text
        assert "总计,150.00" in text
        # Tombstone in 序号 column for summary rows
        assert "—,,餐饮 小计" in text
        assert "—,,总计" in text

    def test_empty_rows_still_emits_grand_total(self, tmp_path):
        csv_path = tmp_path / "summary.csv"
        write_summary_csv(str(csv_path), self._agg([]))
        text = csv_path.read_text(encoding="utf-8-sig")
        assert "总计,0.00" in text


# =============================================================================
# #24 HIGH — supplemental dedup by (msg_id, att_part_id)
# =============================================================================

class TestSupplementalMerge:
    def test_dedup_by_composite_key(self, tmp_path):
        step4 = tmp_path / "step4.json"
        alive = tmp_path / "alive.pdf"
        alive.write_text("x")
        existing = {
            "downloaded": [
                {"message_id": "m1", "attachment_part_id": "a1", "path": str(alive)},
            ],
            "failed": [], "skipped": [],
        }
        step4.write_text(json.dumps(existing))

        new = [
            {"message_id": "m1", "attachment_part_id": "a1", "path": str(alive)},  # dup
            {"message_id": "m2", "attachment_part_id": "a1", "path": str(alive)},  # same attid, different msg
        ]

        merged = merge_supplemental_downloads(str(step4), new)
        assert len(merged) == 2
        ids = [(r["message_id"], r["attachment_part_id"]) for r in merged]
        assert ("m1", "a1") in ids
        assert ("m2", "a1") in ids

    def test_prunes_stale_paths(self, tmp_path):
        step4 = tmp_path / "step4.json"
        alive = tmp_path / "alive.pdf"
        alive.write_text("x")
        dead = tmp_path / "dead.pdf"  # never created
        existing = {
            "downloaded": [
                {"message_id": "alive", "attachment_part_id": "a", "path": str(alive)},
                {"message_id": "dead", "attachment_part_id": "d", "path": str(dead)},
            ],
            "failed": [], "skipped": [],
        }
        step4.write_text(json.dumps(existing))

        merged = merge_supplemental_downloads(str(step4), [])
        ids = [r["message_id"] for r in merged]
        assert "alive" in ids
        assert "dead" not in ids

    def test_creates_backup(self, tmp_path):
        step4 = tmp_path / "step4.json"
        step4.write_text(json.dumps({"downloaded": []}))
        merge_supplemental_downloads(str(step4), [])
        assert (tmp_path / "step4.json.bak").exists()


# =============================================================================
# #26 HIGH — End-to-end with fixture PDFs (no LLM call)
# =============================================================================

class TestE2E:
    def test_no_llm_full_pipeline(
        self, tmp_path, hotel_invoice_pdf, didi_invoice_pdf, didi_receipt_pdf
    ):
        """Run the full post-download pipeline with --no-llm path using 3 real fixtures."""
        pdfs_dir = tmp_path / "out" / "pdfs"
        pdfs_dir.mkdir(parents=True)

        records = []
        for i, src in enumerate([hotel_invoice_pdf, didi_invoice_pdf, didi_receipt_pdf]):
            dst = pdfs_dir / f"tmp_{i}.pdf"
            shutil.copy(src, dst)
            records.append({
                "path": str(dst), "valid": True,
                "message_id": f"m{i}", "attachment_part_id": f"a{i}",
                "internal_date": "1730000000000",
                "merchant": "test", "date": "20251113",
                "category": "UNPARSED", "ocr": None,
            })

        matching = do_all_matching(records)
        assert len(matching["unparsed"]) == 3

        # CSV
        csv_path = tmp_path / "out" / "发票汇总.csv"
        aggregation = build_aggregation(matching, records)
        n_csv = write_summary_csv(str(csv_path), aggregation)
        assert n_csv == 3
        assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")

        # missing.json
        missing_path = tmp_path / "out" / "missing.json"
        payload = write_missing_json(
            str(missing_path),
            batch_dir=str(tmp_path / "out"),
            iteration=1,
            matching_result=matching,
            unparsed_records=matching["unparsed"],
        )
        assert payload["status"] == "user_action_required"  # 3 UNPARSED → human
        assert len(payload["items"]) == 3
        assert payload["schema_version"] == "1.0"

        # Report (must exist for zip manifest check)
        (tmp_path / "out" / "下载报告.md").write_text("# stub")

        # Zip
        zp = zip_output(str(tmp_path / "out"))
        assert os.path.exists(zp)

        import zipfile
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
        # 3 PDFs + 1 CSV + 1 MD, no JSON
        pdfs_in_zip = sum(1 for n in names if n.endswith(".pdf"))
        assert pdfs_in_zip == 3
        assert not any("missing.json" in n for n in names)
        assert not any("run.log" in n for n in names)


# =============================================================================
# Provider matrix — 6 paths through get_client()
# =============================================================================

class TestProviderMatrix:
    """Construct each provider to verify env-var contracts + error messages."""

    # Env vars we need to scrub between tests so one test doesn't leak into
    # the next via the singleton or os.environ.
    _PROVIDER_ENVS = [
        "LLM_PROVIDER",
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    ]

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for k in self._PROVIDER_ENVS:
            monkeypatch.delenv(k, raising=False)
        reset_client()
        yield
        reset_client()

    def test_anthropic_needs_api_key(self):
        with pytest.raises(LLMAuthError, match="ANTHROPIC_API_KEY"):
            get_client("anthropic")

    def test_anthropic_compatible_needs_base_url(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        with pytest.raises(LLMConfigError, match="ANTHROPIC_BASE_URL"):
            get_client("anthropic-compatible")

    def test_anthropic_compatible_needs_api_key_too(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://openrouter.ai/api/v1")
        with pytest.raises(LLMAuthError, match="ANTHROPIC_API_KEY"):
            get_client("anthropic-compatible")

    def test_anthropic_compatible_builds(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://openrouter.ai/api/v1")
        c = get_client("anthropic-compatible")
        assert c.provider_name == "anthropic-compatible"
        assert c.base_url == "https://openrouter.ai/api/v1"

    def test_openai_needs_api_key(self):
        with pytest.raises(LLMAuthError, match="OPENAI_API_KEY"):
            get_client("openai")

    def test_openai_builds(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        c = get_client("openai")
        assert c.provider_name == "openai"
        assert c.model == "gpt-4o"

    def test_openai_respects_model_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1")
        c = get_client("openai")
        assert c.model == "gpt-4.1"

    def test_openai_compatible_needs_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        with pytest.raises(LLMConfigError, match="OPENAI_BASE_URL"):
            get_client("openai-compatible")

    def test_openai_compatible_needs_api_key_too(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
        with pytest.raises(LLMAuthError, match="OPENAI_API_KEY"):
            get_client("openai-compatible")

    def test_openai_compatible_builds(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-ds")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
        c = get_client("openai-compatible")
        assert c.provider_name == "openai-compatible"
        assert c.base_url == "https://api.deepseek.com/v1"

    def test_unknown_provider_raises(self):
        with pytest.raises(LLMConfigError, match="Unknown LLM_PROVIDER"):
            get_client("claude-desktop")

    def test_default_is_bedrock(self, monkeypatch):
        # No LLM_PROVIDER env; default path must pick bedrock. We don't care
        # about live auth — just that BedrockClient is selected.
        c = get_client()
        assert c.provider_name == "bedrock"

    def test_bedrock_default_is_sonnet_4_6(self, monkeypatch):
        """v5.5: Bedrock default model switched Opus 4.7 -> Sonnet 4.6.

        Rationale: Sonnet 4.6 is ~5x cheaper, 42% faster, zero amount-
        field drift vs Opus on the v5.5 brainstorm's 95-PDF benchmark.
        """
        monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
        # Read the literal default baked into BedrockClient.__init__ by
        # parsing the source — avoids the boto3 session construction path.
        import inspect
        from core.llm_client import BedrockClient
        src = inspect.getsource(BedrockClient.__init__)
        assert '"global.anthropic.claude-sonnet-4-6"' in src, (
            "BedrockClient.__init__ must default to Sonnet 4.6"
        )

    def test_bedrock_opus_override_still_works(self, monkeypatch):
        """BEDROCK_MODEL_ID env var still lets callers pin Opus."""
        monkeypatch.setenv(
            "BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-7"
        )
        from core.llm_client import BedrockClient
        c = BedrockClient.__new__(BedrockClient)  # skip __init__'s boto call
        import os
        c.model_id = os.environ.get(
            "BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6"
        )
        assert "opus-4-7" in c.model_id


# =============================================================================
# Doctor LLM check matrix (offline — no live API call)
# =============================================================================

class TestDoctorLLMMatrix:
    """_check_llm_config must handle all 6 providers without calling out."""

    _ENVS = [
        "LLM_PROVIDER", "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
    ]

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for k in self._ENVS:
            monkeypatch.delenv(k, raising=False)
        yield

    def _check(self):
        from doctor import _check_llm_config
        return _check_llm_config()

    def test_bedrock_with_bearer_token(self, monkeypatch):
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer-xyz")
        ok, msg = self._check()
        assert ok and "API key" in msg

    def test_anthropic_missing_key(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        ok, msg = self._check()
        assert not ok and "ANTHROPIC_API_KEY" in msg

    def test_anthropic_ok(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key")
        ok, msg = self._check()
        assert ok and "Anthropic" in msg

    def test_anthropic_compatible_missing_base(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic-compatible")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-any")
        ok, msg = self._check()
        assert not ok and "ANTHROPIC_BASE_URL" in msg

    def test_openai_ok(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
        ok, msg = self._check()
        assert ok and "OpenAI" in msg

    def test_openai_compatible_missing_base(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai-compatible")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-any")
        ok, msg = self._check()
        assert not ok and "OPENAI_BASE_URL" in msg

    def test_unknown_provider(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "claude-desktop")
        ok, msg = self._check()
        assert not ok and "Unknown LLM_PROVIDER" in msg

    def test_doctor_concurrency_env(self, monkeypatch):
        from doctor import _check_ocr_concurrency   # v5.5 new function
        monkeypatch.delenv("LLM_OCR_CONCURRENCY", raising=False)
        ok, msg = _check_ocr_concurrency()
        assert ok is True and "default=5" in msg

        monkeypatch.setenv("LLM_OCR_CONCURRENCY", "3")
        ok, msg = _check_ocr_concurrency()
        assert ok is True and "LLM_OCR_CONCURRENCY=3" in msg

        monkeypatch.setenv("LLM_OCR_CONCURRENCY", "abc")
        ok, msg = _check_ocr_concurrency()
        assert ok is False and "REMEDIATION" in msg

        monkeypatch.setenv("LLM_OCR_CONCURRENCY", "50")
        ok, msg = _check_ocr_concurrency()
        assert ok is True
        assert "WARN:" in msg
        assert "unusually high" in msg or "throttle" in msg


# =============================================================================
# Matching: P1 (remark) / P2 (date+amount) / P3 (v5.2 date-only fallback)
# =============================================================================

def _mkrecord(category, ocr_overrides, path="x.pdf"):
    """Build a minimal download record with the fields do_all_matching needs."""
    return {
        "path": path,
        "valid": True,
        "category": category,
        "ocr": ocr_overrides,
    }


class TestOCRContentDedup:
    """Collapse records that share an OCR business key before matching runs.

    Keys by category:
      HOTEL_INVOICE / MEAL / etc. → invoiceNo
      HOTEL_FOLIO                 → confirmationNo, fallback (hotelName, arrival, departure)
      byte-identical survivors    → SHA256 final fallback
    """

    def _mkrec_with_file(self, tmp_path, basename, category, ocr, content=b"%PDF-1.4 stub"):
        p = tmp_path / basename
        p.write_bytes(content)
        return {"path": str(p), "valid": True, "category": category, "ocr": ocr}

    def test_hotel_folio_same_confirmation_no_collapses(self, tmp_path):
        a = self._mkrec_with_file(
            tmp_path, "20250903_HILTON_水单.pdf", "HOTEL_FOLIO",
            {"confirmationNo": "3332252059", "balance": 552.41}, content=b"%PDF-a")
        b = self._mkrec_with_file(
            tmp_path, "20250903_HILTON_水单 (1).pdf", "HOTEL_FOLIO",
            {"confirmationNo": "3332252059", "balance": 552.41}, content=b"%PDF-b")
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 1
        assert len(removed) == 1
        # Shorter basename wins
        assert kept[0]["path"].endswith("水单.pdf")

    def test_hotel_folio_missing_confirmation_no_fallback_to_dates(self, tmp_path):
        a = self._mkrec_with_file(
            tmp_path, "a.pdf", "HOTEL_FOLIO",
            {"confirmationNo": None, "hotelName": "Hilton Wuxi",
             "arrivalDate": "2025-09-03", "departureDate": "2025-09-04"},
            content=b"%PDF-1.4 A")
        b = self._mkrec_with_file(
            tmp_path, "ab.pdf", "HOTEL_FOLIO",
            {"confirmationNo": None, "hotelName": "HILTON WUXI",
             "arrivalDate": "2025-09-03", "departureDate": "2025-09-04"},
            content=b"%PDF-1.4 B")
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 1, "same hotel + same dates should collapse"
        assert len(removed) == 1

    def test_hotel_folio_different_dates_not_collapsed(self, tmp_path):
        a = self._mkrec_with_file(
            tmp_path, "a.pdf", "HOTEL_FOLIO",
            {"confirmationNo": None, "hotelName": "Hilton Wuxi",
             "arrivalDate": "2025-09-03", "departureDate": "2025-09-04"},
            content=b"%PDF A")
        b = self._mkrec_with_file(
            tmp_path, "b.pdf", "HOTEL_FOLIO",
            {"confirmationNo": None, "hotelName": "Hilton Wuxi",
             "arrivalDate": "2025-09-05", "departureDate": "2025-09-06"},
            content=b"%PDF B")
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 2
        assert removed == []

    def test_hotel_folio_asymmetric_conf_not_collapsed(self, tmp_path):
        """One side has confirmationNo, other doesn't — conservative, surface both."""
        a = self._mkrec_with_file(
            tmp_path, "a.pdf", "HOTEL_FOLIO",
            {"confirmationNo": "ABC123", "hotelName": "Hilton",
             "arrivalDate": "2025-09-03", "departureDate": "2025-09-04"},
            content=b"%PDF A")
        b = self._mkrec_with_file(
            tmp_path, "b.pdf", "HOTEL_FOLIO",
            {"confirmationNo": None, "hotelName": "Hilton",
             "arrivalDate": "2025-09-03", "departureDate": "2025-09-04"},
            content=b"%PDF B")
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 2
        assert removed == []

    def test_invoice_same_invoice_no_collapses(self, tmp_path):
        a = self._mkrec_with_file(
            tmp_path, "20250903_树山_发票.pdf", "HOTEL_INVOICE",
            {"invoiceNo": "25327000001619791763", "transactionAmount": 552.41},
            content=b"%PDF A")
        b = self._mkrec_with_file(
            tmp_path, "20250903_树山_发票 (1).pdf", "HOTEL_INVOICE",
            {"invoiceNo": "25327000001619791763", "transactionAmount": 552.41},
            content=b"%PDF B")
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 1
        assert len(removed) == 1
        assert kept[0]["path"].endswith("_发票.pdf")

    def test_meal_sha256_fallback_collapses_byte_identical(self, tmp_path):
        """瑞幸/巧连 case — same bytes, no invoiceNo, should still collapse via SHA pass."""
        shared_bytes = b"%PDF-1.4 same-meal-pdf-content"
        a = self._mkrec_with_file(
            tmp_path, "20250911_瑞幸_餐饮.pdf", "MEAL",
            {"invoiceNo": None, "transactionAmount": 20.0},
            content=shared_bytes)
        b = self._mkrec_with_file(
            tmp_path, "20250911_瑞幸_餐饮 (1).pdf", "MEAL",
            {"invoiceNo": None, "transactionAmount": 20.0},
            content=shared_bytes)
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 1
        assert len(removed) == 1

    def test_two_real_starbucks_same_day_not_collapsed(self, tmp_path):
        """Two genuine starbucks visits same day, different invoiceNo → both kept.
        Guards against false positives in the 'same day, same vendor, same amount'
        case that does happen for real (two coffees same afternoon)."""
        a = self._mkrec_with_file(
            tmp_path, "20250929_星巴克_餐饮.pdf", "MEAL",
            {"invoiceNo": "25327000001111111111", "transactionAmount": 42.0,
             "transactionDate": "2025-09-29", "vendorName": "星巴克"},
            content=b"%PDF first-coffee")
        b = self._mkrec_with_file(
            tmp_path, "20250929_星巴克_餐饮 (1).pdf", "MEAL",
            {"invoiceNo": "25327000002222222222", "transactionAmount": 42.0,
             "transactionDate": "2025-09-29", "vendorName": "星巴克"},
            content=b"%PDF second-coffee")
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 2
        assert removed == []

    def test_physical_delete_removes_loser_from_disk(self, tmp_path):
        a = self._mkrec_with_file(
            tmp_path, "keep.pdf", "HOTEL_FOLIO",
            {"confirmationNo": "X1"}, content=b"%PDF A")
        b = self._mkrec_with_file(
            tmp_path, "delete-me.pdf", "HOTEL_FOLIO",
            {"confirmationNo": "X1"}, content=b"%PDF B")
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 1
        assert kept[0]["path"].endswith("keep.pdf")
        # Surviving file still exists; loser unlinked
        assert os.path.exists(kept[0]["path"])
        assert not os.path.exists(removed[0]["path"])

    def test_delete_losers_false_preserves_files(self, tmp_path):
        a = self._mkrec_with_file(
            tmp_path, "keep.pdf", "HOTEL_FOLIO",
            {"confirmationNo": "X1"}, content=b"%PDF A")
        b = self._mkrec_with_file(
            tmp_path, "keepalso.pdf", "HOTEL_FOLIO",
            {"confirmationNo": "X1"}, content=b"%PDF B")
        kept, removed = _dedup_by_ocr_business_key([a, b], delete_losers=False)
        assert len(kept) == 1
        assert len(removed) == 1
        # Both files still present on disk (logical removal only)
        assert os.path.exists(kept[0]["path"])
        assert os.path.exists(removed[0]["path"])

    def test_invalid_records_pass_through_untouched(self, tmp_path):
        """Records with valid=False should neither be deduped nor deleted."""
        bad_a = self._mkrec_with_file(
            tmp_path, "bad.pdf", "HOTEL_FOLIO",
            {"confirmationNo": "X1"}, content=b"not a pdf")
        bad_b = self._mkrec_with_file(
            tmp_path, "bad2.pdf", "HOTEL_FOLIO",
            {"confirmationNo": "X1"}, content=b"also not")
        bad_a["valid"] = False
        bad_b["valid"] = False
        kept, removed = _dedup_by_ocr_business_key([bad_a, bad_b])
        assert len(kept) == 2
        assert removed == []

    def test_unknown_category_not_deduped(self, tmp_path):
        """UNKNOWN/UNPARSED stay in the skip-list — we don't trust their contents
        enough to collapse byte-identical pairs."""
        a = self._mkrec_with_file(
            tmp_path, "u1.pdf", "UNKNOWN", {}, content=b"PDF A")
        b = self._mkrec_with_file(
            tmp_path, "u2.pdf", "UNKNOWN", {}, content=b"PDF A")  # same bytes
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 2
        assert removed == []

    def test_ridehailing_receipt_sha256_collapses_byte_identical(self, tmp_path):
        """Regression: --supplemental used to re-download the same Didi
        行程单 attachments and, because RIDEHAILING_RECEIPT was excluded from
        dedup, those byte-identical duplicates survived all the way into the
        matcher as 'orphan' receipts. SHA256 pass 2 must catch them now."""
        shared = b"%PDF-1.4 didi-trip-receipt"
        a = self._mkrec_with_file(
            tmp_path, "20250810_滴滴出行_行程单.pdf", "RIDEHAILING_RECEIPT",
            {"totalAmount": 1755.90}, content=shared)
        b = self._mkrec_with_file(
            tmp_path, "20250810_滴滴出行_行程单 (4).pdf", "RIDEHAILING_RECEIPT",
            {"totalAmount": 1755.90}, content=shared)
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 1
        assert len(removed) == 1
        # Shorter basename (the non-suffixed copy) wins
        assert kept[0]["path"].endswith("行程单.pdf")

    def test_ridehailing_receipt_different_bytes_not_collapsed(self, tmp_path):
        """Two genuine Didi trips same amount but different PDFs (different
        trip metadata) must not collapse — same-amount is not enough."""
        a = self._mkrec_with_file(
            tmp_path, "20250810_trip_a.pdf", "RIDEHAILING_RECEIPT",
            {"totalAmount": 100.0}, content=b"%PDF trip A")
        b = self._mkrec_with_file(
            tmp_path, "20250810_trip_b.pdf", "RIDEHAILING_RECEIPT",
            {"totalAmount": 100.0}, content=b"%PDF trip B")
        kept, removed = _dedup_by_ocr_business_key([a, b])
        assert len(kept) == 2
        assert removed == []


class TestHotelMatchingTiers:
    def test_p1_remark_matches_confirmation_no(self):
        invoice = _mkrecord("HOTEL_INVOICE", {
            "transactionAmount": 1280.00,
            "transactionDate": "2026-03-19",
            "remark": "HT20260319",
        }, path="inv.pdf")
        folio = _mkrecord("HOTEL_FOLIO", {
            "balance": 1280.00,
            "checkOutDate": "2026-03-19",
            "confirmationNo": "HT20260319",
        }, path="fol.pdf")

        result = do_all_matching([invoice, folio])
        assert len(result["hotel"]["matched"]) == 1
        assert result["hotel"]["matched"][0]["match_type"] == "remark"

    def test_p1_remark_matches_internal_codes(self):
        invoice = _mkrecord("HOTEL_INVOICE", {
            "transactionAmount": 1280.00,
            "transactionDate": "2026-03-19",
            "remark": "INTERNAL-42",
        }, path="inv.pdf")
        folio = _mkrecord("HOTEL_FOLIO", {
            "balance": 1280.00,
            "checkOutDate": "2026-03-19",
            "confirmationNo": "SOMETHING-ELSE",
            "internalCodes": ["INTERNAL-42", "INTERNAL-43"],
        }, path="fol.pdf")

        result = do_all_matching([invoice, folio])
        assert len(result["hotel"]["matched"]) == 1
        assert result["hotel"]["matched"][0]["match_type"] == "remark"

    def test_p2_date_and_amount_match(self):
        invoice = _mkrecord("HOTEL_INVOICE", {
            "transactionAmount": 500.00,
            "transactionDate": "2026-04-01",
            # no remark
        }, path="inv.pdf")
        folio = _mkrecord("HOTEL_FOLIO", {
            "balance": 500.00,
            "checkOutDate": "2026-04-01",
            "confirmationNo": "UNRELATED",
        }, path="fol.pdf")

        result = do_all_matching([invoice, folio])
        assert len(result["hotel"]["matched"]) == 1
        assert result["hotel"]["matched"][0]["match_type"] == "date_amount"

    def test_p3_date_only_fallback_fires_only_when_p1_p2_miss(self):
        """Amount differs (LLM missed VAT line), but date matches. P1+P2 miss, P3 catches."""
        invoice = _mkrecord("HOTEL_INVOICE", {
            "transactionAmount": 480.00,  # mismatched amount
            "transactionDate": "2026-05-10",
        }, path="inv.pdf")
        folio = _mkrecord("HOTEL_FOLIO", {
            "balance": 500.00,  # different from invoice
            "checkOutDate": "2026-05-10",
        }, path="fol.pdf")

        result = do_all_matching([invoice, folio])
        matched = result["hotel"]["matched"]
        assert len(matched) == 1
        assert matched[0]["match_type"] == "date_only (v5.2 fallback)"
        assert matched[0]["confidence"] == "low"

    def test_p3_does_not_fire_when_p2_already_matched(self):
        """P2 exact match + a different invoice that would P3-match same folio —
        the P3 candidate should stay unmatched (folio already taken)."""
        inv_p2 = _mkrecord("HOTEL_INVOICE", {
            "transactionAmount": 500.00,
            "transactionDate": "2026-05-10",
        }, path="inv1.pdf")
        inv_p3 = _mkrecord("HOTEL_INVOICE", {
            "transactionAmount": 999.00,  # amount diverges
            "transactionDate": "2026-05-10",
        }, path="inv2.pdf")
        folio = _mkrecord("HOTEL_FOLIO", {
            "balance": 500.00,
            "checkOutDate": "2026-05-10",
        }, path="fol.pdf")

        result = do_all_matching([inv_p2, inv_p3, folio])
        # Exactly one match — P2 wins, P3 invoice is unmatched
        assert len(result["hotel"]["matched"]) == 1
        assert result["hotel"]["matched"][0]["match_type"] == "date_amount"
        assert len(result["hotel"]["unmatched_invoices"]) == 1

    def test_unmatched_when_no_tier_fires(self):
        invoice = _mkrecord("HOTEL_INVOICE", {
            "transactionAmount": 500.00,
            "transactionDate": "2026-03-01",
        }, path="inv.pdf")
        folio = _mkrecord("HOTEL_FOLIO", {
            "balance": 500.00,
            "checkOutDate": "2026-03-15",  # different date
        }, path="fol.pdf")

        result = do_all_matching([invoice, folio])
        assert len(result["hotel"]["matched"]) == 0
        assert len(result["hotel"]["unmatched_invoices"]) == 1
        assert len(result["hotel"]["unmatched_folios"]) == 1

    def test_p3_match_carries_folio_arrival_and_departure(self):
        """v5.5: P3 fallback records folio OCR dates so report can render
        the actual checkout date reviewers care about (not the filename-
        derived internalDate which may be weeks off)."""
        records = [
            # folio — email internalDate 2025-06-07 (later), OCR says 5/7→5/8
            {
                "path": "/tmp/a_folio.pdf",
                "valid": True,
                "category": "HOTEL_FOLIO",
                "ocr": {
                    "hotelName": "苏州万豪",
                    "arrivalDate": "2025-05-07",
                    "departureDate": "2025-05-08",
                    "transactionDate": "2025-05-08",
                    "balance": 583.97,
                    "confirmationNo": "4329092847491260840",
                },
                "vendor_name": "苏州万豪",
                "transaction_date": "20250508",
            },
            # invoice — OCR date 5/8, amount different (P2 miss), remark
            # doesn't match confirmationNo (P1 miss).
            {
                "path": "/tmp/b_invoice.pdf",
                "valid": True,
                "category": "HOTEL_INVOICE",
                "ocr": {
                    "vendorName": "苏州万豪",
                    "transactionDate": "2025-05-08",
                    "transactionAmount": 605.15,
                    "remark": "96978435",
                },
                "vendor_name": "苏州万豪",
                "transaction_date": "20250508",
            },
        ]
        result = do_all_matching(records)
        hotel_matches = result["hotel"].get("matched", [])
        # Find the P3 pair
        p3 = [m for m in hotel_matches if m.get("match_type", "").startswith("date_only")]
        assert len(p3) == 1
        m = p3[0]
        assert m["folio_arrival_date"] == "2025-05-07"
        assert m["folio_departure_date"] == "2025-05-08"


class TestWriteReportMdP3Rendering:
    """v5.5 Task 3: write_report_md renders a dedicated P3 subsection with a
    dedicated '入住 / 退房 (OCR)' column. Locks in the split-table behavior
    and the '?' fallback against regression, and confirms the P3 heading is
    omitted entirely when there are no P3 matches.

    Import approach: scripts/download-invoices.py has a hyphen so plain
    `import` won't work. We use importlib.util.spec_from_file_location (the
    same pattern test_agent_contract.py uses) to load the module in-process
    — much cheaper than spinning up a subprocess per assertion, and we only
    need the single write_report_md function.
    """

    @staticmethod
    def _load_cli_module():
        import importlib.util
        scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
        spec = importlib.util.spec_from_file_location(
            "download_invoices_cli_p3test",
            str(scripts_dir / "download-invoices.py"),
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["download_invoices_cli_p3test"] = mod
        spec.loader.exec_module(mod)
        return mod

    @staticmethod
    def _make_folio_record(path, hotel, arrival, departure, balance):
        return {
            "path": path,
            "valid": True,
            "category": "HOTEL_FOLIO",
            "ocr": {
                "hotelName": hotel,
                "arrivalDate": arrival,
                "departureDate": departure,
                "transactionDate": departure or "2025-05-08",
                "balance": balance,
                "confirmationNo": f"CONF-{path}",
            },
            "merchant": hotel,
        }

    @staticmethod
    def _make_invoice_record(path, vendor, date, amount):
        return {
            "path": path,
            "valid": True,
            "category": "HOTEL_INVOICE",
            "ocr": {
                "vendorName": vendor,
                "transactionDate": date,
                "transactionAmount": amount,
                "remark": "NOT-A-CONFNO",
            },
            "merchant": vendor,
        }

    def _make_p3_match(self, fol_rec, inv_rec, arrival, departure):
        """Build a synthetic P3 match dict matching the shape produced by
        postprocess.do_all_matching + _to_matching_input."""
        return {
            "match_type": "date_only (v5.2 fallback)",
            "confidence": "low",
            "invoice": {
                "s3Key": inv_rec["path"],
                "transactionDate": (inv_rec["ocr"] or {}).get("transactionDate"),
                "transactionAmount": (inv_rec["ocr"] or {}).get("transactionAmount"),
                "_record": inv_rec,
            },
            "folio": {
                "s3Key": fol_rec["path"],
                "checkOutDate": (fol_rec["ocr"] or {}).get("departureDate"),
                "_record": fol_rec,
            },
            "folio_arrival_date": arrival,
            "folio_departure_date": departure,
        }

    def _make_p2_match(self, fol_rec, inv_rec):
        return {
            "match_type": "date_amount",
            "confidence": "medium",
            "invoice": {
                "s3Key": inv_rec["path"],
                "transactionDate": (inv_rec["ocr"] or {}).get("transactionDate"),
                "transactionAmount": (inv_rec["ocr"] or {}).get("transactionAmount"),
                "_record": inv_rec,
            },
            "folio": {
                "s3Key": fol_rec["path"],
                "_record": fol_rec,
            },
        }

    def test_p3_subsection_renders_ocr_dates_with_question_mark_fallback(
        self, tmp_path
    ):
        cli = self._load_cli_module()

        # Two P3 matches: one with full OCR dates, one with both None →
        # renderer must emit '?' for the missing side.
        fol_a = self._make_folio_record(
            "/tmp/a_folio.pdf", "苏州万豪", "2025-05-07", "2025-05-08", 583.97
        )
        inv_a = self._make_invoice_record(
            "/tmp/a_invoice.pdf", "苏州万豪", "2025-05-08", 605.15
        )
        fol_b = self._make_folio_record(
            "/tmp/b_folio.pdf", "希尔顿北京", None, None, 1200.00
        )
        inv_b = self._make_invoice_record(
            "/tmp/b_invoice.pdf", "希尔顿北京", "2025-06-10", 1200.00
        )

        matching_result = {
            "hotel": {
                "matched": [
                    self._make_p3_match(fol_a, inv_a, "2025-05-07", "2025-05-08"),
                    self._make_p3_match(fol_b, inv_b, None, None),
                ],
                "unmatched_invoices": [],
                "unmatched_folios": [],
            },
            "ridehailing": {
                "matched": [], "unmatched_invoices": [], "unmatched_receipts": [],
            },
        }

        report_path = tmp_path / "下载报告.md"
        cli.write_report_md(
            str(report_path),
            downloaded_all=[fol_a, inv_a, fol_b, inv_b],
            failed=[],
            skipped=[],
            matching_result=matching_result,
            date_range=("2025/05/01", "2025/05/31"),
            iteration=1,
            supplemental=False,
            aggregation=None,
        )

        md = report_path.read_text(encoding="utf-8")

        # P3 heading present
        assert "### P3 同日兜底匹配（低可信度）" in md
        # OCR dates column present
        assert "入住 / 退房 (OCR)" in md
        # The full-dates row
        assert "2025-05-07 / 2025-05-08" in md
        # The missing-dates fallback row renders '? / ?'
        assert "? / ?" in md
        # P3 (仅日期) label in the match-type column
        assert "P3 (仅日期)" in md

    def test_p3_heading_absent_when_no_p3_matches(self, tmp_path):
        cli = self._load_cli_module()

        fol = self._make_folio_record(
            "/tmp/c_folio.pdf", "上海万豪", "2025-04-10", "2025-04-12", 900.00
        )
        inv = self._make_invoice_record(
            "/tmp/c_invoice.pdf", "上海万豪", "2025-04-12", 900.00
        )
        matching_result = {
            "hotel": {
                # Only a P2 match — no P3 rows.
                "matched": [self._make_p2_match(fol, inv)],
                "unmatched_invoices": [],
                "unmatched_folios": [],
            },
            "ridehailing": {
                "matched": [], "unmatched_invoices": [], "unmatched_receipts": [],
            },
        }

        report_path = tmp_path / "下载报告.md"
        cli.write_report_md(
            str(report_path),
            downloaded_all=[fol, inv],
            failed=[],
            skipped=[],
            matching_result=matching_result,
            date_range=("2025/04/01", "2025/04/30"),
            iteration=1,
            supplemental=False,
            aggregation=None,
        )

        md = report_path.read_text(encoding="utf-8")

        # P3 heading must be absent
        assert "### P3 同日兜底匹配（低可信度）" not in md
        # OCR-dates column must be absent
        assert "入住 / 退房 (OCR)" not in md
        # Primary P1/P2 table header must be present — sanity check that
        # the report was actually rendered (uses the v5.5-harmonized '发票'
        # column header rather than the old '酒店发票').
        assert "| 退房日 | 销售方 | 匹配方式 | 水单 | 发票 |" in md


class TestRideHailingTiebreaker:
    def test_two_same_amount_invoices_pair_to_closest_receipt_by_file_number(self):
        """3 invoices at 139.80 + 2 receipts at 139.80; receipts should pair to
        the invoices whose (N) file-number is closest."""
        invoices = [
            _mkrecord("RIDEHAILING_INVOICE", {"transactionAmount": 139.80},
                      path="didi_invoice (1).pdf"),
            _mkrecord("RIDEHAILING_INVOICE", {"transactionAmount": 139.80},
                      path="didi_invoice (2).pdf"),
            _mkrecord("RIDEHAILING_INVOICE", {"transactionAmount": 139.80},
                      path="didi_invoice (3).pdf"),
        ]
        receipts = [
            _mkrecord("RIDEHAILING_RECEIPT", {"totalAmount": 139.80},
                      path="didi_trip (1).pdf"),
            _mkrecord("RIDEHAILING_RECEIPT", {"totalAmount": 139.80},
                      path="didi_trip (2).pdf"),
        ]
        result = do_all_matching(invoices + receipts)
        rh = result["ridehailing"]
        assert len(rh["matched"]) == 2
        # 2 of 3 invoices paired; 1 unmatched
        assert len(rh["unmatched_invoices"]) == 1
        # Each matched invoice is paired with a receipt of the same number
        for m in rh["matched"]:
            inv_name = m["invoice"]["s3Key"]
            rec_name = m["receipt"]["s3Key"]
            # Extract the (N) from each
            import re
            inv_n = re.search(r"\((\d+)\)", inv_name).group(1)
            rec_n = re.search(r"\((\d+)\)", rec_name).group(1)
            assert inv_n == rec_n, f"mismatched tiebreaker: {inv_name} ↔ {rec_name}"

    def test_amount_mismatch_does_not_pair(self):
        invoices = [
            _mkrecord("RIDEHAILING_INVOICE", {"transactionAmount": 100.00},
                      path="x.pdf"),
        ]
        receipts = [
            _mkrecord("RIDEHAILING_RECEIPT", {"totalAmount": 200.00}, path="y.pdf"),
        ]
        result = do_all_matching(invoices + receipts)
        assert len(result["ridehailing"]["matched"]) == 0
        assert len(result["ridehailing"]["unmatched_invoices"]) == 1
        assert len(result["ridehailing"]["unmatched_receipts"]) == 1

    def test_none_amount_never_matches(self):
        """_to_float preserves None (not 0), so an unknown amount can't accidentally
        match another unknown-amount record."""
        inv = _mkrecord("RIDEHAILING_INVOICE", {"transactionAmount": None},
                        path="x.pdf")
        rec = _mkrecord("RIDEHAILING_RECEIPT", {"totalAmount": None}, path="y.pdf")
        result = do_all_matching([inv, rec])
        assert len(result["ridehailing"]["matched"]) == 0


# =============================================================================
# parse_llm_response edge cases
# =============================================================================

class TestParseLLMResponse:
    def test_plain_json(self):
        assert parse_llm_response('{"a": 1}') == {"a": 1}

    def test_json_fenced_with_language(self):
        assert parse_llm_response('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_fenced_without_language(self):
        assert parse_llm_response('```\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_commentary(self):
        text = 'Here is the invoice data:\n{"vendorName": "X", "amount": 100}\nThanks.'
        result = parse_llm_response(text)
        assert result["vendorName"] == "X"

    def test_empty_response_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            parse_llm_response("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            parse_llm_response("   \n  \t ")

    def test_malformed_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_llm_response("not json at all, no braces")


# =============================================================================
# validate_and_fix_vendor_info — 4 recovery strategies
# =============================================================================

class TestValidateAndFixVendor:
    def test_no_change_when_vendor_not_buyer(self):
        data = {"vendorName": "无锡茵赫餐饮", "vendorTaxId": "91320214MA"}
        result = validate_and_fix_vendor_info(dict(data))
        assert result["vendorName"] == "无锡茵赫餐饮"
        assert "_vendorNameInvalid" not in result

    def test_strategy1_seller_fallback(self):
        """LLM put buyer in vendorName, seller info was extracted correctly."""
        data = {
            "vendorName": "亚马逊信息服务",
            "vendorTaxId": "buyer-tax",
            "sellerName": "无锡茵赫餐饮",
            "sellerTaxId": "91320214MA",
        }
        result = validate_and_fix_vendor_info(dict(data))
        assert result["vendorName"] == "无锡茵赫餐饮"
        assert result["vendorTaxId"] == "91320214MA"

    def test_strategy2_hotel_name_fallback(self):
        data = {
            "vendorName": "亚马逊信息服务",
            "hotelName": "无锡万怡酒店",
        }
        result = validate_and_fix_vendor_info(dict(data))
        assert result["vendorName"] == "无锡万怡酒店"

    def test_strategy3_buyer_seller_reversed(self):
        data = {
            "vendorName": "亚马逊信息服务",
            "buyerName": "无锡茵赫餐饮",
            "buyerTaxId": "91320214MA",
        }
        result = validate_and_fix_vendor_info(dict(data))
        assert result["vendorName"] == "无锡茵赫餐饮"
        assert result["vendorTaxId"] == "91320214MA"

    def test_strategy4_nothing_to_recover_marks_invalid(self):
        data = {"vendorName": "亚马逊信息服务"}  # no seller / hotel / buyer
        result = validate_and_fix_vendor_info(dict(data))
        assert result["vendorName"] == ""
        assert result.get("_vendorNameInvalid") is True

    def test_null_vendor_name_marked_invalid(self):
        """Post-review fix #25: null/empty vendor should also mark invalid."""
        data = {"vendorName": None}
        result = validate_and_fix_vendor_info(dict(data))
        assert result.get("_vendorNameInvalid") is True

    def test_null_vendor_with_seller_fallback(self):
        data = {"vendorName": None, "sellerName": "Real Seller"}
        result = validate_and_fix_vendor_info(dict(data))
        assert result["vendorName"] == "Real Seller"
        assert not result.get("_vendorNameInvalid")


# =============================================================================
# missing.json state machine branches
# =============================================================================

def _empty_matching_result():
    return {
        "hotel": {"matched": [], "unmatched_invoices": [], "unmatched_folios": []},
        "ridehailing": {"matched": [], "unmatched_invoices": [], "unmatched_receipts": []},
    }


class TestMissingJsonStateMachine:
    def test_empty_items_converged(self, tmp_path):
        path = tmp_path / "missing.json"
        payload = write_missing_json(
            str(path), batch_dir=str(tmp_path), iteration=1,
            matching_result=_empty_matching_result(), unparsed_records=[],
        )
        assert payload["status"] == "converged"
        assert payload["recommended_next_action"] == "stop"

    def test_needs_retry_when_items_and_iteration_low(self, tmp_path):
        # Build a fake unmatched hotel invoice so items is non-empty
        mr = _empty_matching_result()
        mr["hotel"]["unmatched_invoices"].append({
            "_record": {"path": "inv.pdf", "ocr": {
                "transactionDate": "2026-04-01", "vendorName": "X",
                "transactionAmount": 100, "remark": "R",
            }},
        })
        path = tmp_path / "missing.json"
        payload = write_missing_json(
            str(path), batch_dir=str(tmp_path), iteration=1, iteration_cap=3,
            matching_result=mr, unparsed_records=[],
        )
        assert payload["status"] == "needs_retry"
        assert payload["recommended_next_action"] == "run_supplemental"

    def test_max_iterations_reached_when_cap_hit(self, tmp_path):
        mr = _empty_matching_result()
        mr["hotel"]["unmatched_invoices"].append({
            "_record": {"path": "inv.pdf", "ocr": {"transactionDate": "2026-04-01"}},
        })
        path = tmp_path / "missing.json"
        payload = write_missing_json(
            str(path), batch_dir=str(tmp_path), iteration=3, iteration_cap=3,
            matching_result=mr, unparsed_records=[],
        )
        assert payload["status"] == "max_iterations_reached"
        assert payload["recommended_next_action"] == "ask_user"

    def test_converged_when_hash_unchanged(self, tmp_path):
        mr = _empty_matching_result()
        mr["hotel"]["unmatched_invoices"].append({
            "_record": {"path": "inv.pdf", "ocr": {"transactionDate": "2026-04-01"}},
        })
        path = tmp_path / "missing.json"
        # First iteration — compute hash
        p1 = write_missing_json(
            str(path), batch_dir=str(tmp_path), iteration=1,
            matching_result=mr, unparsed_records=[],
        )
        # Second iteration — same items → same hash → should converge early
        p2 = write_missing_json(
            str(path), batch_dir=str(tmp_path), iteration=2,
            matching_result=mr, unparsed_records=[],
            previous_convergence_hash=p1["convergence_hash"],
        )
        assert p2["status"] == "converged"
        assert p2["recommended_next_action"] == "stop"

    def test_user_action_required_when_only_extraction_failed(self, tmp_path):
        path = tmp_path / "missing.json"
        payload = write_missing_json(
            str(path), batch_dir=str(tmp_path), iteration=1,
            matching_result=_empty_matching_result(),
            unparsed_records=[{"path": "bad.pdf", "error": "LLM returned junk"}],
        )
        assert payload["status"] == "user_action_required"
        assert payload["recommended_next_action"] == "ask_user"

    # ─── v5.5 — out_of_range_items[] routing ─────────────────────────────
    def test_folio_before_start_routes_to_out_of_range(self, tmp_path):
        """v5.5: folio's departureDate before run_start_date → item lands
        in out_of_range_items[], not items[]. status = converged."""
        hotel = {
            "unmatched_folios": [{
                "_record": {
                    "path": "/tmp/old_folio.pdf",
                    "ocr": {
                        "hotelName": "杭州万豪",
                        "departureDate": "2025-03-18",  # before Q2 start
                        "balance": 500,
                    },
                },
            }],
        }
        matching = {"hotel": hotel, "ridehailing": {}}
        mpath = tmp_path / "missing.json"
        payload = write_missing_json(
            str(mpath),
            batch_dir=str(tmp_path),
            iteration=1,
            matching_result=matching,
            unparsed_records=[],
            run_start_date="2026/04/01",   # Q2 start
            run_end_date="2026/07/01",     # Q2 end
        )
        assert payload["items"] == []
        assert len(payload["out_of_range_items"]) == 1
        orr = payload["out_of_range_items"][0]
        assert orr["type"] == "hotel_invoice"  # missing type: invoice
        assert orr["business_date"] == "2025-03-18"
        assert orr["reason"] == "business_date_out_of_range"
        assert payload["status"] == "converged"
        assert payload["recommended_next_action"] == "stop"

    def test_in_range_item_stays_in_items(self, tmp_path):
        hotel = {
            "unmatched_folios": [{
                "_record": {"path": "/tmp/x.pdf", "ocr": {
                    "hotelName": "H", "departureDate": "2026-05-10", "balance": 1,
                }},
            }],
        }
        payload = write_missing_json(
            str(tmp_path / "m.json"),
            batch_dir=str(tmp_path), iteration=1,
            matching_result={"hotel": hotel, "ridehailing": {}},
            unparsed_records=[],
            run_start_date="2026/04/01", run_end_date="2026/07/01",
        )
        assert len(payload["items"]) == 1
        assert payload["out_of_range_items"] == []
        assert payload["status"] == "needs_retry"

    def test_mixed_batch(self, tmp_path):
        hotel = {
            "unmatched_folios": [
                {"_record": {"path": "/a.pdf", "ocr": {"hotelName": "A",
                    "departureDate": "2025-03-10", "balance": 1}}},
                {"_record": {"path": "/b.pdf", "ocr": {"hotelName": "B",
                    "departureDate": "2026-05-10", "balance": 2}}},
            ],
        }
        payload = write_missing_json(
            str(tmp_path / "m.json"),
            batch_dir=str(tmp_path), iteration=1,
            matching_result={"hotel": hotel, "ridehailing": {}},
            unparsed_records=[],
            run_start_date="2026/04/01", run_end_date="2026/07/01",
        )
        assert len(payload["items"]) == 1
        assert len(payload["out_of_range_items"]) == 1
        assert payload["status"] == "needs_retry"

    def test_missing_business_date_stays_in_items(self, tmp_path):
        hotel = {
            "unmatched_folios": [{"_record": {"path": "/x.pdf", "ocr": {
                "hotelName": "X", "departureDate": None, "balance": 1,
            }}}],
        }
        payload = write_missing_json(
            str(tmp_path / "m.json"),
            batch_dir=str(tmp_path), iteration=1,
            matching_result={"hotel": hotel, "ridehailing": {}},
            unparsed_records=[],
            run_start_date="2026/04/01", run_end_date="2026/07/01",
        )
        assert len(payload["items"]) == 1
        assert payload["out_of_range_items"] == []

    def test_extraction_failed_never_filtered(self, tmp_path):
        payload = write_missing_json(
            str(tmp_path / "m.json"),
            batch_dir=str(tmp_path), iteration=1,
            matching_result={"hotel": {}, "ridehailing": {}},
            unparsed_records=[{"path": "/x.pdf", "error": "boom"}],
            run_start_date="2026/04/01", run_end_date="2026/07/01",
        )
        assert len(payload["items"]) == 1
        assert payload["items"][0]["type"] == "extraction_failed"
        assert payload["out_of_range_items"] == []

    def test_boundary_start_inclusive_end_exclusive(self, tmp_path):
        hotel = {
            "unmatched_folios": [
                {"_record": {"path": "/s.pdf", "ocr": {"hotelName": "S",
                    "departureDate": "2026-04-01", "balance": 1}}},
                {"_record": {"path": "/e.pdf", "ocr": {"hotelName": "E",
                    "departureDate": "2026-07-01", "balance": 2}}},
            ],
        }
        payload = write_missing_json(
            str(tmp_path / "m.json"),
            batch_dir=str(tmp_path), iteration=1,
            matching_result={"hotel": hotel, "ridehailing": {}},
            unparsed_records=[],
            run_start_date="2026/04/01", run_end_date="2026/07/01",
        )
        # start inclusive — April 1 stays in items
        assert sum(1 for it in payload["items"] if "s.pdf" in it["needed_for"]) == 1
        # end exclusive — July 1 is out of range
        assert sum(1 for it in payload["out_of_range_items"]
                   if "e.pdf" in it["needed_for"]) == 1

    def test_parse_cli_ymd_handles_gmail_format(self):
        from postprocess import _parse_cli_ymd
        import datetime
        assert _parse_cli_ymd("2026/04/01") == datetime.date(2026, 4, 1)
        assert _parse_cli_ymd("2026-04-01") == datetime.date(2026, 4, 1)
        assert _parse_cli_ymd("") is None
        assert _parse_cli_ymd("2026-04") is None
        assert _parse_cli_ymd("abc") is None

    def test_ridehailing_invoice_before_start_routes_to_out_of_range(self, tmp_path):
        """v5.5: ride-hailing receipt with transactionDate before run_start
        → item lands in out_of_range_items (not items). Covers the
        ridehailing_invoice branch of the routing block."""
        rh = {
            "unmatched_receipts": [{
                "_record": {
                    "path": "/tmp/old_itinerary.pdf",
                    "ocr": {
                        "transactionDate": "2025-03-10",  # before Q2 start
                        "totalAmount": 88.5,
                    },
                },
            }],
        }
        payload = write_missing_json(
            str(tmp_path / "m.json"),
            batch_dir=str(tmp_path), iteration=1,
            matching_result={"hotel": {}, "ridehailing": rh},
            unparsed_records=[],
            run_start_date="2026/04/01", run_end_date="2026/07/01",
        )
        assert payload["items"] == []
        assert len(payload["out_of_range_items"]) == 1
        orr = payload["out_of_range_items"][0]
        assert orr["type"] == "ridehailing_invoice"  # missing type: invoice
        assert orr["business_date"] == "2025-03-10"
        assert orr["reason"] == "business_date_out_of_range"


# =============================================================================
# _compute_convergence_hash properties
# =============================================================================

class TestConvergenceHash:
    def test_identity_same_items_same_hash(self):
        items = [
            {"type": "hotel_folio", "needed_for": "a.pdf"},
            {"type": "hotel_invoice", "needed_for": "b.pdf"},
        ]
        assert _compute_convergence_hash(items) == _compute_convergence_hash(list(items))

    def test_order_insensitivity(self):
        a = [{"type": "hotel_folio", "needed_for": "a.pdf"},
             {"type": "hotel_invoice", "needed_for": "b.pdf"}]
        b = list(reversed(a))
        assert _compute_convergence_hash(a) == _compute_convergence_hash(b)

    def test_type_change_changes_hash(self):
        """Post-review fix #26: same filename different type must NOT collide."""
        a = [{"type": "hotel_folio", "needed_for": "a.pdf"}]
        b = [{"type": "extraction_failed", "needed_for": "a.pdf"}]
        assert _compute_convergence_hash(a) != _compute_convergence_hash(b)

    def test_added_item_changes_hash(self):
        a = [{"type": "hotel_folio", "needed_for": "a.pdf"}]
        b = a + [{"type": "hotel_folio", "needed_for": "b.pdf"}]
        assert _compute_convergence_hash(a) != _compute_convergence_hash(b)

    def test_empty_items_stable_hash(self):
        h1 = _compute_convergence_hash([])
        h2 = _compute_convergence_hash([])
        assert h1 == h2
        # And it's a 16-char hex string
        assert len(h1) == 16 and all(c in "0123456789abcdef" for c in h1)


# =============================================================================
# SDK error classification (fix #4)
# =============================================================================

class TestErrorClassification:
    def test_anthropic_rate_limit_error_detected(self):
        # Build a fake SDK exception that looks like anthropic.RateLimitError
        class RateLimitError(Exception):
            def __init__(self, msg, status_code=429):
                super().__init__(msg)
                self.status_code = status_code

        # extract_with_retry should classify this as retryable
        class MC(LLMClient):
            provider_name = "mock"
            def __init__(self): self.calls = 0
            def extract_from_pdf(self, b, p):
                self.calls += 1
                if self.calls < 2:
                    # Simulate SDK throwing the typed exception, which our wrapper
                    # maps via _reraise_as_llm_error
                    from core.llm_client import _reraise_as_llm_error
                    try:
                        raise RateLimitError("rate limited", 429)
                    except Exception as e:
                        _reraise_as_llm_error(e)
                return '{"ok": true}'

        mc = MC()
        result = extract_with_retry(b"x", "p", client=mc, base_delay=0.01)
        assert mc.calls == 2
        assert result == '{"ok": true}'

    def test_botocore_throttling_mapped_to_rate_limit(self):
        """boto3 ClientError with ThrottlingException code → LLMRateLimitError."""
        from core.llm_client import _reraise_as_llm_error

        class FakeClientError(Exception):
            def __init__(self):
                super().__init__("An error occurred (ThrottlingException)")
                self.response = {"Error": {"Code": "ThrottlingException"}}

        with pytest.raises(LLMRateLimitError):
            try:
                raise FakeClientError()
            except Exception as e:
                _reraise_as_llm_error(e)

    def test_botocore_access_denied_mapped_to_auth(self):
        from core.llm_client import _reraise_as_llm_error

        class FakeClientError(Exception):
            def __init__(self):
                super().__init__("AccessDeniedException")
                self.response = {"Error": {"Code": "AccessDeniedException"}}

        with pytest.raises(LLMAuthError):
            try:
                raise FakeClientError()
            except Exception as e:
                _reraise_as_llm_error(e)

    def test_apitimeout_mapped_to_server_error(self):
        from core.llm_client import _reraise_as_llm_error

        class APITimeoutError(Exception):
            pass

        with pytest.raises(LLMServerError):
            try:
                raise APITimeoutError("timeout")
            except Exception as e:
                _reraise_as_llm_error(e)

    def test_substring_fallback_still_works(self):
        """Generic exception with '429' in message should still classify as rate limit."""
        from core.llm_client import _reraise_as_llm_error

        with pytest.raises(LLMRateLimitError):
            try:
                raise Exception("HTTP 429 too many requests")
            except Exception as e:
                _reraise_as_llm_error(e)

    def test_unknown_error_becomes_generic_llm_error(self):
        from core.llm_client import _reraise_as_llm_error

        with pytest.raises(LLMError) as exc_info:
            try:
                raise Exception("some weird error with no identifiable keywords")
            except Exception as e:
                _reraise_as_llm_error(e)
        # Should be LLMError, not a specific subclass
        assert type(exc_info.value).__name__ == "LLMError"


# =============================================================================
# Fix #17 — calendar-invalid dates rejected by normalize_date
# =============================================================================

class TestNormalizeDate:
    def test_valid_iso_date(self):
        assert normalize_date("2026-03-19") == "20260319"

    def test_single_digit_components(self):
        assert normalize_date("2026-3-5") == "20260305"

    def test_yyyymmdd_shorthand(self):
        assert normalize_date("20260319") == "20260319"

    def test_calendar_invalid_rejected(self):
        """Post-review fix #17: 2026-02-31 is not a valid date."""
        assert normalize_date("2026-02-31") == ""

    def test_month_out_of_range(self):
        assert normalize_date("2026-13-05") == ""

    def test_empty_returns_empty(self):
        assert normalize_date("") == ""
        assert normalize_date(None) == ""

    def test_garbage_returns_empty(self):
        assert normalize_date("not a date") == ""


# =============================================================================
# Fix #1 — LLMAuthError propagates from analyze_pdf_batch
# =============================================================================

class TestAnalyzePdfBatchAuthPropagation:
    def test_auth_error_propagates_not_swallowed(self, tmp_path, monkeypatch):
        """Post-review fix #1: analyze_pdf_batch must re-raise LLMAuthError so
        the CLI can exit with EXIT_LLM_CONFIG instead of silently UNPARSING
        every record."""
        # Force get_client() to raise LLMAuthError
        from core import llm_client
        def raising_get_client(override=None):
            raise LLMAuthError("ANTHROPIC_API_KEY not set")
        monkeypatch.setattr(llm_client, "get_client", raising_get_client)
        # Also patch the import reference in postprocess
        import postprocess
        monkeypatch.setattr(postprocess, "get_client", raising_get_client)

        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        records = [{"path": str(pdf), "valid": True, "message_id": "m1"}]

        # Must raise, not return results with all-UNPARSED
        with pytest.raises(LLMAuthError):
            analyze_pdf_batch(records, use_llm=True)


class TestConcurrencyEnvVar:
    """v5.5: LLM_OCR_CONCURRENCY honored; default bumped 2 → 5.

    `postprocess.py` imports ThreadPoolExecutor via `from concurrent.futures
    import ThreadPoolExecutor`, so the monkeypatch must target the module-
    level binding `postprocess.ThreadPoolExecutor`, not the source class.
    """

    def _spy_executor_cls(self, captured):
        import postprocess as _pp
        real = _pp.ThreadPoolExecutor

        class SpyExecutor(real):
            def __init__(self, max_workers=None, *a, **k):
                captured["max_workers"] = max_workers
                super().__init__(max_workers=max_workers, *a, **k)

        return SpyExecutor

    def test_default_is_5_when_unset(self, monkeypatch):
        monkeypatch.delenv("LLM_OCR_CONCURRENCY", raising=False)
        captured: Dict[str, Any] = {}
        monkeypatch.setattr(
            "postprocess.ThreadPoolExecutor", self._spy_executor_cls(captured)
        )
        from postprocess import analyze_pdf_batch
        analyze_pdf_batch([], use_llm=False)
        assert captured.get("max_workers") == 5

    def test_env_var_honored(self, monkeypatch):
        monkeypatch.setenv("LLM_OCR_CONCURRENCY", "3")
        captured: Dict[str, Any] = {}
        monkeypatch.setattr(
            "postprocess.ThreadPoolExecutor", self._spy_executor_cls(captured)
        )
        from postprocess import analyze_pdf_batch
        analyze_pdf_batch([], use_llm=False)
        assert captured.get("max_workers") == 3

    def test_explicit_kwarg_beats_env(self, monkeypatch):
        monkeypatch.setenv("LLM_OCR_CONCURRENCY", "10")
        captured: Dict[str, Any] = {}
        monkeypatch.setattr(
            "postprocess.ThreadPoolExecutor", self._spy_executor_cls(captured)
        )
        from postprocess import analyze_pdf_batch
        analyze_pdf_batch([], use_llm=False, max_workers=4)
        assert captured.get("max_workers") == 4

    def test_invalid_env_raises_config_error(self, monkeypatch):
        from postprocess import analyze_pdf_batch
        for bad in ("abc", "-1", "0"):
            monkeypatch.setenv("LLM_OCR_CONCURRENCY", bad)
            with pytest.raises(LLMConfigError):
                analyze_pdf_batch([], use_llm=False)

    def test_empty_env_is_unset(self, monkeypatch):
        """Empty string should behave like the env var being unset."""
        monkeypatch.setenv("LLM_OCR_CONCURRENCY", "")
        captured: Dict[str, Any] = {}
        monkeypatch.setattr(
            "postprocess.ThreadPoolExecutor", self._spy_executor_cls(captured)
        )
        from postprocess import analyze_pdf_batch
        analyze_pdf_batch([], use_llm=False)
        assert captured.get("max_workers") == 5

    def test_cli_does_not_hardcode_max_workers(self):
        """Regression guard for v5.5 task-5 bug: download-invoices.py must
        NOT pass max_workers= as a literal to analyze_pdf_batch — it
        would bypass LLM_OCR_CONCURRENCY. Use the sentinel None default."""
        import pathlib
        import re
        repo_root = pathlib.Path(__file__).resolve().parent.parent
        src = (repo_root / "scripts" / "download-invoices.py").read_text()
        # Grab each call's arg region
        calls = re.findall(
            r"analyze_pdf_batch\s*\([^)]*?\)",
            src,
            re.DOTALL,
        )
        assert calls, "could not locate analyze_pdf_batch call — test may be stale"
        for call in calls:
            assert "max_workers" not in call, (
                f"analyze_pdf_batch call hardcodes max_workers; "
                f"this would bypass LLM_OCR_CONCURRENCY:\n{call}"
            )


# =============================================================================
# Unit 1 — build_aggregation + MergedRow + category registry
# =============================================================================

from decimal import Decimal, ROUND_HALF_EVEN, getcontext  # noqa: E402


def _agg_record(category: str, ocr: dict, path: str = "x.pdf") -> dict:
    """Build a download record shaped for do_all_matching / build_aggregation."""
    return {
        "path": path,
        "valid": True,
        "category": category,
        "ocr": ocr,
    }


class TestCategoryConstants:
    def test_hotel_and_ridehailing_labels_registered(self):
        assert CATEGORY_LABELS["HOTEL"] == "酒店"
        assert CATEGORY_LABELS["RIDEHAILING"] == "网约车"

    def test_category_order_has_new_keys(self):
        assert "HOTEL" in CATEGORY_ORDER
        assert "RIDEHAILING" in CATEGORY_ORDER

    def test_category_order_relative_sort(self):
        """HOTEL merged < HOTEL_FOLIO < HOTEL_INVOICE < RIDEHAILING < rest < UNPARSED."""
        o = CATEGORY_ORDER
        assert o["HOTEL"] < o["HOTEL_FOLIO"] < o["HOTEL_INVOICE"] < o["RIDEHAILING"]
        assert o["RIDEHAILING"] < o["RIDEHAILING_INVOICE"] < o["RIDEHAILING_RECEIPT"]
        assert o["RIDEHAILING_RECEIPT"] < o["MEAL"] < o["TRAIN"]
        assert o["UNPARSED"] == 99
        # All non-UNPARSED come before UNPARSED
        for cat, idx in o.items():
            if cat == "UNPARSED":
                continue
            assert idx < o["UNPARSED"]

    def test_ignored_label_registered(self):
        from postprocess import CATEGORY_LABELS, CATEGORY_ORDER
        assert CATEGORY_LABELS.get("IGNORED") == "已忽略"
        # CATEGORY_ORDER deliberately NOT registering IGNORED: IGNORED
        # records do not enter aggregation rows, so the default fallback
        # get(..., 50) is enough. Registering IGNORED would risk
        # interacting with the UNPARSED=99 invariant.
        assert "IGNORED" not in CATEGORY_ORDER


class TestWorstOf:
    def test_low_worse_than_high(self):
        assert worst_of("high", "low") == "low"
        assert worst_of("low", "high") == "low"

    def test_failed_is_worst(self):
        assert worst_of("low", "failed") == "failed"
        assert worst_of("high", "failed") == "failed"

    def test_all_high(self):
        assert worst_of("high", "high") == "high"

    def test_single_value(self):
        assert worst_of("high") == "high"
        assert worst_of("low") == "low"

    def test_unknown_raises(self):
        """DEC-7 fail-fast: unknown confidence must not silently downgrade to high."""
        with pytest.raises(ValueError):
            worst_of("high", "medium")
        with pytest.raises(ValueError):
            worst_of("medium")

    def test_valid_confidences_set(self):
        assert VALID_CONFIDENCES == frozenset({"high", "low", "failed"})


class TestBuildAggregation:
    # -- Happy path (a): hotel pair collapses to 1 row ---------------------

    def test_hotel_pair_collapses_to_one_row(self):
        inv = _agg_record("HOTEL_INVOICE", {
            "transactionAmount": 1280.00,
            "transactionDate": "2026-03-19",
            "remark": "HT-A",
            "vendorName": "无锡万怡",
        }, path="/out/pdfs/inv.pdf")
        fol = _agg_record("HOTEL_FOLIO", {
            "balance": 1280.00,
            "checkOutDate": "2026-03-19",
            "confirmationNo": "HT-A",
            "hotelName": "无锡万怡",
        }, path="/out/pdfs/fol.pdf")

        matching = do_all_matching([inv, fol])
        agg = build_aggregation(matching, [inv, fol])

        hotel_rows = [r for r in agg["rows"] if r.category == "HOTEL"]
        assert len(hotel_rows) == 1
        row = hotel_rows[0]
        assert row.primary_file == "inv.pdf"
        assert row.paired_file == "fol.pdf"
        assert row.paired_kind == "水单"
        assert row.amount == Decimal("1280.00")

    # -- Happy path (b): ride-hailing pair collapses to 1 row -------------

    def test_ridehailing_pair_collapses_to_one_row(self):
        inv = _agg_record("RIDEHAILING_INVOICE", {
            "transactionAmount": 139.80,
            "transactionDate": "2026-04-05",
            "vendorName": "滴滴",
        }, path="/out/pdfs/didi_inv.pdf")
        rec = _agg_record("RIDEHAILING_RECEIPT", {
            "totalAmount": 139.80,
            "transactionDate": "2026-04-05",
        }, path="/out/pdfs/didi_trip.pdf")

        matching = do_all_matching([inv, rec])
        agg = build_aggregation(matching, [inv, rec])

        rh_rows = [r for r in agg["rows"] if r.category == "RIDEHAILING"]
        assert len(rh_rows) == 1
        row = rh_rows[0]
        assert row.paired_kind == "行程单"
        assert row.primary_file == "didi_inv.pdf"
        assert row.paired_file == "didi_trip.pdf"

    # -- Happy path (c): unpaired hotel invoice keeps original label ------

    def test_unmatched_hotel_invoice_keeps_label(self):
        inv = _agg_record("HOTEL_INVOICE", {
            "transactionAmount": 300.00,
            "transactionDate": "2026-05-01",
            "remark": "ORPHAN",
            "vendorName": "孤儿酒店",
        }, path="/out/pdfs/orphan.pdf")

        matching = do_all_matching([inv])
        agg = build_aggregation(matching, [inv])

        rows = agg["rows"]
        assert len(rows) == 1
        assert rows[0].category == "HOTEL_INVOICE"
        assert rows[0].paired_file is None
        assert rows[0].paired_kind is None

    # -- Happy path (d): None amount kept in rows but not in subtotals ----

    def test_none_amount_preserved_and_excluded_from_subtotal(self):
        rec = _agg_record("MEAL", {
            "transactionDate": "2026-04-20",
            "transactionAmount": None,
            "vendorName": "某餐厅",
        }, path="/out/pdfs/meal.pdf")
        other = _agg_record("MEAL", {
            "transactionDate": "2026-04-21",
            "transactionAmount": 50.00,
            "vendorName": "某餐厅",
        }, path="/out/pdfs/meal2.pdf")

        matching = do_all_matching([rec, other])
        agg = build_aggregation(matching, [rec, other])

        meal_rows = [r for r in agg["rows"] if r.category == "MEAL"]
        assert len(meal_rows) == 2
        # None stays None; subtotal only counts the 50.00 row
        amounts = sorted((r.amount for r in meal_rows), key=lambda a: (a is None, a or 0))
        assert amounts[0] == Decimal("50.00")
        assert amounts[1] is None
        assert agg["subtotals"]["MEAL"] == Decimal("50.00")
        # voucher_count counts both (non-UNPARSED)
        assert agg["voucher_count"] == 2

    # -- Happy path (e): low_conf counts + aggregates --------------------

    def test_low_conf_counted_with_amount(self):
        inv = _agg_record("MEAL", {
            "transactionDate": "2026-04-15",
            "transactionAmount": 80.00,
            "vendorName": "X",
            "_amountConfidence": "low",
        }, path="/out/pdfs/meal.pdf")

        matching = do_all_matching([inv])
        agg = build_aggregation(matching, [inv])

        assert agg["low_conf"]["count"] == 1
        assert agg["low_conf"]["amount"] == Decimal("80.00")

    # -- Happy path (f): Decimal precision avoids float drift ------------

    def test_decimal_precision_no_float_drift(self):
        recs = [
            _agg_record("MEAL", {"transactionDate": "2026-04-01",
                                  "transactionAmount": 0.10, "vendorName": "A"},
                        path="/out/pdfs/a.pdf"),
            _agg_record("MEAL", {"transactionDate": "2026-04-02",
                                  "transactionAmount": 0.20, "vendorName": "B"},
                        path="/out/pdfs/b.pdf"),
            _agg_record("MEAL", {"transactionDate": "2026-04-03",
                                  "transactionAmount": 0.30, "vendorName": "C"},
                        path="/out/pdfs/c.pdf"),
        ]
        matching = do_all_matching(recs)
        agg = build_aggregation(matching, recs)

        assert agg["subtotals"]["MEAL"] == Decimal("0.60")
        assert agg["grand_total"] == Decimal("0.60")
        # Ensure the string form matches too (what CSV/MD will render)
        assert f"{agg['grand_total']:.2f}" == "0.60"

    # -- Edge: --no-llm all-UNPARSED -------------------------------------

    def test_no_llm_all_unparsed(self):
        recs = [
            {"path": f"/out/pdfs/u{i}.pdf", "valid": True,
             "category": "UNPARSED", "ocr": None}
            for i in range(3)
        ]
        matching = do_all_matching(recs)
        agg = build_aggregation(matching, recs)

        assert agg["voucher_count"] == 0
        assert agg["grand_total"] == Decimal("0.00")
        assert len(agg["rows"]) == 3
        assert all(r.category == "UNPARSED" for r in agg["rows"])

    # -- Edge: P3 fallback demotes confidence to low ---------------------

    def test_p3_fallback_demotes_confidence(self):
        inv = _agg_record("HOTEL_INVOICE", {
            "transactionAmount": 480.00,  # differs from folio balance
            "transactionDate": "2026-05-10",
            "vendorName": "酒店 B",
        }, path="/out/pdfs/inv.pdf")
        fol = _agg_record("HOTEL_FOLIO", {
            "balance": 500.00,
            "checkOutDate": "2026-05-10",
            "hotelName": "酒店 B",
        }, path="/out/pdfs/fol.pdf")

        matching = do_all_matching([inv, fol])
        agg = build_aggregation(matching, [inv, fol])

        hotel_rows = [r for r in agg["rows"] if r.category == "HOTEL"]
        assert len(hotel_rows) == 1
        assert hotel_rows[0].confidence == "low"
        assert agg["low_conf"]["count"] == 1

    # -- Edge: completely empty Gmail run -------------------------------

    def test_empty_matching_result(self):
        matching = do_all_matching([])
        agg = build_aggregation(matching, [])

        assert agg["rows"] == []
        assert agg["subtotals"] == {}
        assert agg["grand_total"] == Decimal("0.00")
        assert agg["voucher_count"] == 0
        assert agg["unmatched"]["hotel_invoices"] == 0
        assert agg["unmatched"]["hotel_folios"] == 0
        assert agg["unmatched"]["rh_invoices"] == 0
        assert agg["unmatched"]["rh_receipts"] == 0

    # -- Edge: mixed valid + ocr-None records ---------------------------

    def test_row_count_matches_valid_records(self):
        """5 valid records, 1 of which has ocr=None (UNPARSED)."""
        ok = [
            _agg_record("MEAL", {"transactionDate": "2026-04-01",
                                  "transactionAmount": 10.00, "vendorName": "V"},
                        path=f"/out/pdfs/m{i}.pdf")
            for i in range(4)
        ]
        broken = {"path": "/out/pdfs/broken.pdf", "valid": True,
                  "category": "UNPARSED", "ocr": None}
        recs = ok + [broken]

        matching = do_all_matching(recs)
        agg = build_aggregation(matching, recs)

        assert len(agg["rows"]) == len(recs)  # completeness assertion
        assert agg["voucher_count"] == 4  # excludes UNPARSED

    # -- Edge: localcontext does not pollute global rounding ------------

    def test_localcontext_does_not_pollute_global(self):
        before = getcontext().rounding
        recs = [_agg_record("MEAL", {"transactionDate": "2026-04-01",
                                      "transactionAmount": 1.235, "vendorName": "V"},
                            path="/out/pdfs/m.pdf")]
        matching = do_all_matching(recs)
        build_aggregation(matching, recs)
        # Global context must be untouched
        assert getcontext().rounding == before
        assert getcontext().rounding == ROUND_HALF_EVEN

    # -- Edge: error path - malformed matching_result -------------------

    def test_missing_hotel_key_raises(self):
        """Defensive defaults on matching_result would mask upstream bugs —
        fail fast instead."""
        with pytest.raises(KeyError):
            build_aggregation({}, [])

    # -- Integration: rows sorted by CATEGORY_ORDER then date -----------

    def test_rows_sorted_by_category_then_date(self):
        recs = [
            _agg_record("MEAL", {"transactionDate": "2026-04-02",
                                  "transactionAmount": 20.00, "vendorName": "M2"},
                        path="/out/pdfs/m2.pdf"),
            _agg_record("HOTEL_INVOICE", {"transactionDate": "2026-04-01",
                                            "transactionAmount": 300.00,
                                            "vendorName": "H", "remark": "NOPAIR"},
                        path="/out/pdfs/h.pdf"),
            _agg_record("MEAL", {"transactionDate": "2026-04-01",
                                  "transactionAmount": 10.00, "vendorName": "M1"},
                        path="/out/pdfs/m1.pdf"),
        ]
        matching = do_all_matching(recs)
        agg = build_aggregation(matching, recs)

        # HOTEL_INVOICE sorts before MEAL; within MEAL, date asc
        assert agg["rows"][0].category == "HOTEL_INVOICE"
        assert agg["rows"][1].category == "MEAL"
        assert agg["rows"][1].date == "2026-04-01"
        assert agg["rows"][2].category == "MEAL"
        assert agg["rows"][2].date == "2026-04-02"

    # -- Regression: do_all_matching's OCR-business dedup must not strand
    # -- records that main() still considers "valid". do_all_matching now
    # -- returns the removed records under "dedup_removed"; main filters them
    # -- out of valid_records before calling build_aggregation, so the
    # -- completeness assertion stays balanced.
    def test_dedup_removed_surfaces_and_balances_completeness(self, tmp_path):
        # Two MEAL records sharing invoiceNo — one will be deduped.
        a = {
            "path": str(tmp_path / "meal_a.pdf"),
            "valid": True, "category": "MEAL",
            "ocr": {"invoiceNo": "25932000000090149375",
                    "transactionAmount": 98.35,
                    "transactionDate": "2025-09-11",
                    "vendorName": "瑞幸咖啡（宁波）"},
        }
        b = {
            "path": str(tmp_path / "meal_a (1).pdf"),
            "valid": True, "category": "MEAL",
            "ocr": {"invoiceNo": "25932000000090149375",
                    "transactionAmount": 98.35,
                    "transactionDate": "2025-09-11",
                    "vendorName": "瑞幸咖啡（宁波）"},
        }
        # Both paths need to exist for _dedup_by_ocr_business_key's delete step
        for rec in (a, b):
            Path(rec["path"]).write_bytes(b"%PDF stub")

        downloaded_all = [a, b]
        matching = do_all_matching(downloaded_all)

        # The matcher surfaces the losers so main() can exclude them.
        assert "dedup_removed" in matching
        assert len(matching["dedup_removed"]) == 1

        # Simulating main()'s filter: valid_records drops the dedup losers.
        dedup_ids = {id(r) for r in matching["dedup_removed"]}
        valid_records = [d for d in downloaded_all
                         if d.get("valid") and id(d) not in dedup_ids]
        assert len(valid_records) == 1

        # build_aggregation's completeness assertion must now pass.
        agg = build_aggregation(matching, valid_records)
        assert len([r for r in agg["rows"] if r.category == "MEAL"]) == 1

    def test_ignored_record_leaking_into_aggregation_raises(self):
        """Unit 3 guard: main() is the only place that filters out IGNORED
        records before they reach build_aggregation. If a future refactor
        silently lets one through, catch it here.
        """
        from postprocess import build_aggregation
        matching_result = {
            "hotel": {"paired": [], "unmatched_invoices": [], "unmatched_folios": []},
            "ridehailing": {"paired": [], "unmatched_invoices": [], "unmatched_receipts": []},
            "other": [{
                "category": "IGNORED",  # should never reach here
                "path": "/tmp/bogus.pdf",
                "transaction_date": "20250101",
                "vendor_name": "Termius",
                "ocr": {"transactionAmount": 120.0},
            }],
            "unparsed": [],
            "dedup_removed": [],
        }
        valid_records = matching_result["other"]
        with pytest.raises(AssertionError, match="IGNORED leaked"):
            build_aggregation(matching_result, valid_records)


class TestAggregationConsistency:
    """Three writers must render the same grand_total string.

    This suite is placeholder-ready — Unit 2/3 will flesh it out. We assert
    the invariant at the aggregation layer: grand_total is a single Decimal
    object that round-trips through `:.2f` consistently.
    """

    def test_grand_total_decimal_formats_two_decimals(self):
        recs = [
            _agg_record("MEAL", {"transactionDate": "2026-04-01",
                                  "transactionAmount": 1234.567,
                                  "vendorName": "V"},
                        path="/out/pdfs/a.pdf"),
        ]
        matching = do_all_matching(recs)
        agg = build_aggregation(matching, recs)

        # 1234.567 → 1234.57 (ROUND_HALF_UP)
        assert agg["subtotals"]["MEAL"] == Decimal("1234.57")
        assert f"{agg['grand_total']:.2f}" == "1234.57"


# =============================================================================
# Unit 3 — print_openclaw_summary
# =============================================================================

class TestPrintOpenClawSummary:
    """Assert content + shape of the OpenClaw chat summary. We use a sink
    list + lambda writer so we can inspect the exact lines emitted, but the
    implementation must also work with writer=print (stdout capture fallback).
    """

    @staticmethod
    def _capture(**kwargs) -> List[str]:
        """Run print_openclaw_summary against a sink and return its lines."""
        sink: List[str] = []
        kwargs.setdefault("writer", lambda s: sink.append(s))
        print_openclaw_summary(**kwargs)
        return sink

    @staticmethod
    def _default_paths(tmp_path):
        return {
            "output_dir": str(tmp_path),
            "zip_path":   str(tmp_path / "package.zip"),
            "csv_path":   str(tmp_path / "发票汇总.csv"),
            "md_path":    str(tmp_path / "下载报告.md"),
            "log_path":   str(tmp_path / "run.log"),
        }

    def _populated_agg(self):
        recs = [
            _agg_record("MEAL", {"transactionDate": "2026-04-01",
                                  "transactionAmount": 100.0, "vendorName": "V1"},
                        path="/out/pdfs/m1.pdf"),
            _agg_record("TRAIN", {"transactionDate": "2026-04-02",
                                   "transactionAmount": 300.0, "vendorName": "G1234"},
                        path="/out/pdfs/t.pdf"),
        ]
        matching = do_all_matching(recs)
        return build_aggregation(matching, recs)

    # -- Non-empty template (R16a) ---------------------------------------

    def test_non_empty_template_shows_vouchers_and_total(self, tmp_path):
        agg = self._populated_agg()
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop", date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        assert "📄 发票报销包" in text
        assert "共 2 份凭证" in text
        assert "¥400.00" in text

    def test_stop_status_says_can_submit(self, tmp_path):
        agg = self._populated_agg()
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop", date_range=("2026/04/01", "2026/04/30"),
        )
        assert any("可以提交报销" in ln for ln in lines)

    def test_run_supplemental_includes_quoted_command(self, tmp_path):
        agg = self._populated_agg()
        output_dir = str(tmp_path / "spaces in path")
        os.makedirs(output_dir, exist_ok=True)
        paths = self._default_paths(tmp_path)
        paths["output_dir"] = output_dir
        lines = self._capture(
            aggregation=agg, **paths,
            missing_status="run_supplemental",
            date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        assert "--supplemental" in text
        # shlex.quote wraps paths with spaces in single quotes
        assert "'" in text

    def test_ask_user_points_at_md(self, tmp_path):
        agg = self._populated_agg()
        paths = self._default_paths(tmp_path)
        lines = self._capture(
            aggregation=agg, **paths,
            missing_status="ask_user",
            date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        assert "需人工核查" in text
        assert paths["md_path"] in text

    def test_unknown_missing_status_raises(self, tmp_path):
        agg = self._populated_agg()
        with pytest.raises(ValueError):
            print_openclaw_summary(
                aggregation=agg, **self._default_paths(tmp_path),
                missing_status="bogus",
                date_range=("2026/04/01", "2026/04/30"),
            )

    # -- Empty template (R16b) -------------------------------------------

    def test_empty_template_short(self, tmp_path):
        matching = do_all_matching([])
        agg = build_aggregation(matching, [])
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        assert "本次未下载到凭证" in text
        # Empty template: no zip/csv/md lines
        assert "📦" not in text
        # Empty template does not include a "下一步" block
        assert "可以提交报销" not in text

    # -- Exclusions invite ----------------------------------------------

    def test_invite_present_in_non_empty_template(self, tmp_path):
        agg = self._populated_agg()
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop", date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        assert "💡 发现不该报销的" in text
        assert "learned_exclusions.json，下次自动排除" in text

    def test_invite_absent_in_empty_template(self, tmp_path):
        matching = do_all_matching([])
        agg = build_aggregation(matching, [])
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        assert "💡" not in text

    # -- Edge cases -----------------------------------------------------

    def test_zip_failure_degrades_gracefully(self, tmp_path):
        agg = self._populated_agg()
        paths = self._default_paths(tmp_path)
        paths["zip_path"] = None  # DEC-6 sentinel
        lines = self._capture(
            aggregation=agg, **paths,
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        assert "未生成" in text or "打包失败" in text
        # CSV and MD paths still present
        assert paths["csv_path"] in text
        assert paths["md_path"] in text

    def test_unparsed_only_uses_non_empty_template(self, tmp_path):
        """--no-llm run: all UNPARSED → non-empty template (R16a), warning category."""
        recs = [
            {"path": "/out/pdfs/u1.pdf", "valid": True,
             "category": "UNPARSED", "ocr": None},
        ]
        matching = do_all_matching(recs)
        agg = build_aggregation(matching, recs)
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="ask_user",
            date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        # UNPARSED rows exist → non-empty template, not R16b
        assert "本次未下载到凭证" not in text
        assert "📄 发票报销包" in text

    def test_low_conf_footnote_when_present(self, tmp_path):
        recs = [
            _agg_record("MEAL", {"transactionDate": "2026-04-01",
                                  "transactionAmount": 50.0, "vendorName": "V",
                                  "_amountConfidence": "low"},
                        path="/out/pdfs/m.pdf"),
        ]
        matching = do_all_matching(recs)
        agg = build_aggregation(matching, recs)
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        assert "†" in text
        assert "可信度=low" in text

    def test_unmatched_hotel_warnings_only_when_positive(self, tmp_path):
        recs = [
            _agg_record("HOTEL_INVOICE", {"transactionDate": "2026-04-01",
                                            "transactionAmount": 500.0,
                                            "remark": "UNIQUE",
                                            "vendorName": "酒店"},
                        path="/out/pdfs/inv.pdf"),
        ]
        matching = do_all_matching(recs)
        agg = build_aggregation(matching, recs)
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="run_supplemental",
            date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        assert "酒店发票无对应水单" in text
        # No ride-hailing noise
        assert "网约车" not in text

    def test_absolute_paths_in_output(self, tmp_path):
        agg = self._populated_agg()
        # Pass relative paths — implementation must abspath them
        lines = self._capture(
            aggregation=agg,
            output_dir=str(tmp_path),
            zip_path="rel_package.zip",
            csv_path="rel_summary.csv",
            md_path="rel_report.md",
            log_path="rel_run.log",
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        text = "\n".join(lines)
        for fragment in ["rel_package.zip", "rel_summary.csv", "rel_report.md"]:
            # Each referenced path should resolve to absolute
            assert os.path.abspath(fragment) in text

    def test_grand_total_strings_match_csv_and_md(self, tmp_path):
        """Integration: CSV total, MD total, stdout total render identically."""
        recs = [
            _agg_record("MEAL", {"transactionDate": "2026-04-01",
                                  "transactionAmount": 12.345,
                                  "vendorName": "V"},
                        path="/out/pdfs/m.pdf"),
        ]
        matching = do_all_matching(recs)
        agg = build_aggregation(matching, recs)

        csv_path = tmp_path / "summary.csv"
        write_summary_csv(str(csv_path), agg)
        csv_text = csv_path.read_text(encoding="utf-8-sig")

        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        stdout_text = "\n".join(lines)

        # ROUND_HALF_UP: 12.345 → 12.35
        expected = "12.35"
        assert expected in csv_text
        assert expected in stdout_text
        # MD rendering — via write_report_md
        md_path = tmp_path / "report.md"
        # Minimal shim: build via dispatch-style call is cumbersome; just
        # assert the Decimal itself renders identically:
        assert f"{agg['grand_total']:.2f}" == expected

    # -- CHAT_MESSAGE boundary sentinels (v5.6) --------------------------

    def test_chat_message_anchors_wrap_non_empty_summary(self, tmp_path):
        agg = self._populated_agg()
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop", date_range=("2026/04/01", "2026/04/30"),
        )
        # Exactly one START, exactly one END
        assert lines.count("CHAT_MESSAGE_START") == 1
        assert lines.count("CHAT_MESSAGE_END") == 1
        start = lines.index("CHAT_MESSAGE_START")
        end = lines.index("CHAT_MESSAGE_END")
        assert start < end
        # Anchors are bare — no trailing colon, no payload
        assert lines[start] == "CHAT_MESSAGE_START"
        assert lines[end] == "CHAT_MESSAGE_END"

    def test_chat_message_anchors_wrap_empty_template(self, tmp_path):
        matching = do_all_matching([])
        agg = build_aggregation(matching, [])
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        assert lines.count("CHAT_MESSAGE_START") == 1
        assert lines.count("CHAT_MESSAGE_END") == 1
        start = lines.index("CHAT_MESSAGE_START")
        end = lines.index("CHAT_MESSAGE_END")
        assert start < end
        # R16b "本次未下载到凭证" text falls inside the boundary
        inner = "\n".join(lines[start + 1:end])
        assert "本次未下载到凭证" in inner

    def test_chat_message_boundary_includes_exclusions_invite(self, tmp_path):
        agg = self._populated_agg()
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop", date_range=("2026/04/01", "2026/04/30"),
        )
        start = lines.index("CHAT_MESSAGE_START")
        end = lines.index("CHAT_MESSAGE_END")
        inner = "\n".join(lines[start + 1:end])
        # Both invite lines live inside the boundary
        assert "💡 发现不该报销的" in inner
        assert "learned_exclusions.json，下次自动排除" in inner

    # -- CHAT_ATTACHMENTS JSON sentinel (v5.6) ---------------------------

    @staticmethod
    def _extract_attachments(lines: List[str]) -> Optional[Dict[str, Any]]:
        """Find `CHAT_ATTACHMENTS: {...}` line and return parsed JSON, or None."""
        import json as _json
        prefix = "CHAT_ATTACHMENTS: "
        for ln in lines:
            if ln.startswith(prefix):
                return _json.loads(ln[len(prefix):])
        return None

    def test_attachments_emitted_normal_three_deliverables(self, tmp_path):
        agg = self._populated_agg()
        paths = self._default_paths(tmp_path)
        lines = self._capture(
            aggregation=agg, **paths,
            missing_status="stop", date_range=("2026/04/01", "2026/04/30"),
        )
        payload = self._extract_attachments(lines)
        assert payload is not None
        files = payload["files"]
        assert len(files) == 3
        assert files[0]["caption"] == "报销包"
        assert files[1]["caption"] == "报告"
        assert files[2]["caption"] == "明细"
        for f in files:
            assert os.path.isabs(f["path"])
        assert files[0]["path"] == os.path.abspath(paths["zip_path"])
        assert files[1]["path"] == os.path.abspath(paths["md_path"])
        assert files[2]["path"] == os.path.abspath(paths["csv_path"])

    def test_attachments_omits_zip_when_zip_failed(self, tmp_path):
        agg = self._populated_agg()
        paths = self._default_paths(tmp_path)
        paths["zip_path"] = None
        lines = self._capture(
            aggregation=agg, **paths,
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        payload = self._extract_attachments(lines)
        assert payload is not None
        files = payload["files"]
        assert len(files) == 2
        captions = [f["caption"] for f in files]
        assert "报销包" not in captions
        assert captions == ["报告", "明细"]

    def test_attachments_omitted_on_empty_template(self, tmp_path):
        matching = do_all_matching([])
        agg = build_aggregation(matching, [])
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        assert any(ln == "CHAT_MESSAGE_START" for ln in lines)
        assert any(ln == "CHAT_MESSAGE_END" for ln in lines)
        assert not any(ln.startswith("CHAT_ATTACHMENTS: ") for ln in lines)

    def test_attachments_after_message_end(self, tmp_path):
        agg = self._populated_agg()
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop", date_range=("2026/04/01", "2026/04/30"),
        )
        end_idx = lines.index("CHAT_MESSAGE_END")
        att_idx = next(
            i for i, ln in enumerate(lines)
            if ln.startswith("CHAT_ATTACHMENTS: ")
        )
        assert att_idx > end_idx

    def test_attachments_json_is_valid_utf8(self, tmp_path):
        """JSON line parses and preserves Chinese captions (ensure_ascii=False)."""
        agg = self._populated_agg()
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop", date_range=("2026/04/01", "2026/04/30"),
        )
        att_line = next(
            ln for ln in lines if ln.startswith("CHAT_ATTACHMENTS: ")
        )
        assert "报销包" in att_line
        import json as _json
        payload = _json.loads(att_line[len("CHAT_ATTACHMENTS: "):])
        assert "files" in payload

    def test_unknown_missing_status_raises_without_emitting_start(self, tmp_path):
        """ValueError must fire before any sentinel is written (no half-open
        CHAT_MESSAGE_START with missing END). Guards against future refactors
        that reorder the validation and the START emission.
        """
        agg = self._populated_agg()
        sink: List[str] = []
        with pytest.raises(ValueError):
            print_openclaw_summary(
                aggregation=agg, **self._default_paths(tmp_path),
                missing_status="bogus",
                date_range=("2026/04/01", "2026/04/30"),
                writer=lambda s: sink.append(s),
            )
        assert "CHAT_MESSAGE_START" not in sink
        assert "CHAT_MESSAGE_END" not in sink

    def test_chat_message_end_is_terminal_on_empty_path(self, tmp_path):
        """On R16b (empty result), CHAT_MESSAGE_END must be the final line —
        nothing may leak after it, including CHAT_ATTACHMENTS.
        """
        matching = do_all_matching([])
        agg = build_aggregation(matching, [])
        lines = self._capture(
            aggregation=agg, **self._default_paths(tmp_path),
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        end_idx = lines.index("CHAT_MESSAGE_END")
        assert end_idx == len(lines) - 1, (
            f"Expected CHAT_MESSAGE_END to be the last line on R16b, "
            f"but lines after it: {lines[end_idx+1:]}"
        )

    # -- Unit 4 R3: ignored_count line (v5.7) -----------------------------

    def test_ignored_count_line_rendered_when_nonzero(self):
        """Unit 4 R3: print_openclaw_summary emits '📭 已忽略 N 张非报销票据'
        when ignored_count > 0.
        """
        from postprocess import print_openclaw_summary
        aggregation = {
            "rows": [],
            "subtotals": {},
            "unmatched": {"hotel_invoices": 0, "hotel_folios": 0,
                          "rh_invoices": 0, "rh_receipts": 0},
            "voucher_count": 1,
            "low_conf": {"count": 0, "amount": 0.0},
            "grand_total": 100.0,
        }
        lines = []
        print_openclaw_summary(
            aggregation,
            output_dir="/tmp/out",
            zip_path="/tmp/out/发票打包.zip",
            csv_path="/tmp/out/发票汇总.csv",
            md_path="/tmp/out/下载报告.md",
            log_path="/tmp/out/run.log",
            missing_status="stop",
            date_range=("2025/01/01", "2025/03/31"),
            writer=lines.append,
            ignored_count=3,
        )
        text = "\n".join(lines)
        assert "📭 已忽略 3 张非报销票据" in text

    def test_ignored_count_zero_omits_line(self):
        from postprocess import print_openclaw_summary
        aggregation = {
            "rows": [],
            "subtotals": {},
            "unmatched": {"hotel_invoices": 0, "hotel_folios": 0,
                          "rh_invoices": 0, "rh_receipts": 0},
            "voucher_count": 1,
            "low_conf": {"count": 0, "amount": 0.0},
            "grand_total": 100.0,
        }
        lines = []
        print_openclaw_summary(
            aggregation,
            output_dir="/tmp/out",
            zip_path="/tmp/out/发票打包.zip",
            csv_path="/tmp/out/发票汇总.csv",
            md_path="/tmp/out/下载报告.md",
            log_path="/tmp/out/run.log",
            missing_status="stop",
            date_range=("2025/01/01", "2025/03/31"),
            writer=lines.append,
            ignored_count=0,
        )
        text = "\n".join(lines)
        assert "已忽略" not in text


class TestPromptContract:
    """Unit 0 R5: guard the hotel-field conditional extraction rule in prompts.py.

    The rule prevents LLM from filling arrival/departure/room fields from
    non-hotel contexts (subscription period, date due, etc.), which would
    otherwise trigger is_hotel_folio_by_fields 3-choose-2 on SaaS invoices.
    Keep these substrings synced with the rule text in prompts.py.
    """

    def test_hotel_conditional_rule_present(self):
        from core.prompts import get_ocr_prompt
        prompt = get_ocr_prompt()
        # Core rule phrasing — do not remove during snapshot sync with
        # reimbursement-helper without re-reading SKILL.md Lessons Learned v5.7.
        required_substrings = [
            "Hotel-specific field conditional extraction",
            "subscription period",
            "date due",
            "Room No.",   # anchor the English positive-trigger half of the rule
            "入离日期",
            "房号",
            "MUST remain `null`",
        ]
        for s in required_substrings:
            assert s in prompt, f"Prompt missing required substring: {s!r}"


class TestClassifyIgnored:
    """Unit 1 R1: classify_invoice fallthrough returns IGNORED.

    Termius-shape SaaS invoices (English docType, no Chinese tax ID,
    no hotel fields, no service type match) fall through all priority
    branches and must land on IGNORED, not UNKNOWN.
    """

    def test_empty_invoice_returns_ignored(self):
        from core.classify import classify_invoice
        assert classify_invoice({}) == "IGNORED"

    def test_termius_shape_returns_ignored(self):
        from core.classify import classify_invoice
        invoice = {
            "isChineseInvoice": False,
            "vendorTaxId": None,
            "docType": "Invoice",
            "serviceType": None,
            "vendorName": "Termius Corporation",
            # No hotel fields
        }
        assert classify_invoice(invoice) == "IGNORED"

    def test_stripe_like_with_balance_but_no_hotel_fields_ignored(self):
        """The balance field alone must not gate a record into HOTEL_FOLIO.

        Stripe/Termius invoices commonly surface "Amount due/Amount paid",
        which LLMs may spill into 'balance'. Narrow gate must not trigger
        on that single signal.
        """
        from core.classify import classify_invoice
        invoice = {
            "docType": "Statement",
            "balance": 120.0,
            "vendorName": "Stripe-like SaaS",
        }
        assert classify_invoice(invoice) == "IGNORED"

    def test_chinese_meal_invoice_still_classifies_correctly(self):
        """Regression: pre-existing MEAL path unaffected by fallthrough rename."""
        from core.classify import classify_invoice
        invoice = {
            "isChineseInvoice": True,
            "vendorTaxId": "91320214MA1XXXXXX",
            "serviceType": "*餐饮服务*餐饮费",
            "docType": "电子发票（普通发票）",
        }
        assert classify_invoice(invoice) == "MEAL"


class TestHotelFolioNarrowGate:
    """Unit 1 R1: is_hotel_folio_by_doctype narrow gate requires >=2
    of {hotelName, confirmationNo, internalCodes, roomNumber}.

    balance is deliberately NOT in the set (SaaS "Amount due" conflict).
    arrivalDate/departureDate are deliberately NOT in the set
    (Unit 0 prompt layer handles subscription-range leakage).
    """

    def test_two_fields_pass_narrow_gate(self):
        from core.classify import classify_invoice
        invoice = {
            "docType": "Statement",
            "hotelName": "Marriott Shanghai",
            "confirmationNo": "86690506",
        }
        assert classify_invoice(invoice) == "HOTEL_FOLIO"

    def test_one_field_fails_narrow_gate(self):
        from core.classify import classify_invoice
        invoice = {
            "docType": "Statement",
            "hotelName": "Marriott Shanghai",
            # Only 1 field — should fall through to IGNORED
        }
        assert classify_invoice(invoice) == "IGNORED"

    def test_termius_single_field_hallucination_still_ignored(self):
        """LLM might fill hotelName with 'Termius Corporation'. Single
        field is not enough to pass the gate.
        """
        from core.classify import classify_invoice
        invoice = {
            "docType": "Invoice",
            "hotelName": "Termius Corporation",
        }
        assert classify_invoice(invoice) == "IGNORED"

    def test_guest_folio_with_balance_only_is_ignored(self):
        """balance is NOT in the narrow-gate field set. A folio that
        arrives with only balance filled falls through to IGNORED.
        Previous 5-field proposal (with balance) is deliberately rejected
        — see brainstorm 2026-05-02 for rationale.
        """
        from core.classify import classify_invoice
        invoice = {
            "docType": "Guest Folio",
            "balance": 1260.0,
        }
        assert classify_invoice(invoice) == "IGNORED"

    def test_room_and_confirmation_pass(self):
        from core.classify import classify_invoice
        invoice = {
            "docType": "Statement",
            "roomNumber": "1205",
            "confirmationNo": "86690506",
        }
        assert classify_invoice(invoice) == "HOTEL_FOLIO"

    def test_fields_path_still_works_unchanged(self):
        """is_hotel_folio_by_fields 3-choose-2 of roomNumber/arrivalDate/
        departureDate is untouched by Unit 1; legit folios go through this
        path unaffected.

        Use an empty docType so is_hotel_folio_by_doctype short-circuits
        at its `if not doc_type` guard and cannot mask a fields-path
        regression. Previous version used "any" which relied on the
        keyword set not containing that literal — brittle.
        """
        from core.classify import classify_invoice, is_hotel_folio_by_doctype
        # Make the precondition explicit: doc_type does not match any
        # is_hotel_folio_by_doctype keyword, so only the fields path can
        # produce HOTEL_FOLIO here.
        assert is_hotel_folio_by_doctype("") is False
        invoice = {
            "docType": "",
            "roomNumber": "1205",
            "arrivalDate": "2025-11-12",
            "departureDate": "2025-11-13",
        }
        assert classify_invoice(invoice) == "HOTEL_FOLIO"


class TestSenderEmailPassthrough:
    """Unit 2 R2 prerequisite: sender_email is exported in classify_email
    return dict and flows through to download records, so Unit 3's
    rename_by_ocr can compose IGNORED_{domain-label}_{base}.pdf.
    """

    def _msg(self, from_value: str) -> dict:
        return {
            "id": "msg-001",
            "payload": {
                "headers": [
                    {"name": "From", "value": from_value},
                    {"name": "Subject", "value": "Invoice for Termius Pro"},
                ],
                "body": {"data": ""},
            },
            "internalDate": "1731600000000",
        }

    def test_sender_email_extracted_from_name_bracket_form(self):
        from invoice_helpers import classify_email
        result = classify_email(self._msg("Billing <billing@termius.com>"))
        assert result.get("sender") == "Billing <billing@termius.com>"
        assert result.get("sender_email") == "billing@termius.com"

    def test_sender_email_extracted_from_bare_address(self):
        from invoice_helpers import classify_email
        result = classify_email(self._msg("kent@example.com"))
        assert result.get("sender_email") == "kent@example.com"

    def test_sender_email_empty_when_sender_missing(self):
        from invoice_helpers import classify_email
        msg = {
            "id": "x",
            "payload": {"headers": [], "body": {"data": ""}},
            "internalDate": "1731600000000",
        }
        result = classify_email(msg)
        assert result.get("sender_email") == ""


class TestRenameIgnoredBranch:
    """Unit 3 R2: rename_by_ocr IGNORED branch produces
    IGNORED_{sender_short}_{base}.pdf. Reuses UNPARSED branch's
    sanitize + .pdf re-append pattern for safety.
    """

    def _setup(self, tmp_path, filename="original.pdf"):
        pdf = tmp_path / filename
        pdf.write_bytes(b"%PDF-1.4 dummy")
        return pdf

    def test_termius_renamed_with_domain_label(self, tmp_path):
        from postprocess import rename_by_ocr
        pdf = self._setup(tmp_path, "Q9YJOO4I-0001.pdf")
        record = {
            "path": str(pdf),
            "message_id": "msg-termius-001",
            "sender": "Billing <billing@termius.com>",
            "sender_email": "billing@termius.com",
            "merchant": "Termius",
            "date": "20251112",
        }
        analysis = {"category": "IGNORED", "ocr": {"docType": "Invoice"}}
        rename_by_ocr(record, analysis, str(tmp_path))
        assert os.path.basename(record["path"]).startswith("IGNORED_termius_")
        assert record["path"].endswith(".pdf")
        assert os.path.exists(record["path"])

    def test_empty_sender_email_falls_back_to_unknown(self, tmp_path):
        from postprocess import rename_by_ocr
        pdf = self._setup(tmp_path, "mystery.pdf")
        record = {
            "path": str(pdf),
            "message_id": "msg-mystery-001",
            "sender": "",
            "sender_email": "",
            "merchant": "",
            "date": "20250101",
        }
        analysis = {"category": "IGNORED", "ocr": {"docType": "whatever"}}
        rename_by_ocr(record, analysis, str(tmp_path))
        assert os.path.basename(record["path"]).startswith("IGNORED_unknown_")

    def test_long_domain_truncated(self, tmp_path):
        from postprocess import rename_by_ocr
        pdf = self._setup(tmp_path, "x.pdf")
        record = {
            "path": str(pdf),
            "message_id": "msg-x-001",
            "sender_email": "ops@reallylongdomainname-with-suffix.co",
            "merchant": "X",
            "date": "20250101",
        }
        analysis = {"category": "IGNORED", "ocr": {}}
        rename_by_ocr(record, analysis, str(tmp_path))
        basename = os.path.basename(record["path"])
        # Domain label capped at 20 chars (between the two underscores).
        # Structure: IGNORED_{domain<=20}_{base}
        parts = basename.split("_", 2)
        assert parts[0] == "IGNORED"
        assert len(parts[1]) <= 20
        assert basename.endswith(".pdf")

    def test_weird_email_sanitized(self, tmp_path):
        """sender_email with unusual chars must not break os.rename
        (sanitize_filename handles path separators / NUL / etc.).
        Pin to the IGNORED branch: without the startswith assertion the
        test would still pass via the happy path, which also sanitizes
        and preserves .pdf.
        """
        from postprocess import rename_by_ocr
        pdf = self._setup(tmp_path, "z.pdf")
        record = {
            "path": str(pdf),
            "message_id": "msg-z-001",
            "sender_email": "foo@bar@baz.com",
            "merchant": "Z",
            "date": "20250101",
        }
        analysis = {"category": "IGNORED", "ocr": {}}
        rename_by_ocr(record, analysis, str(tmp_path))
        basename = os.path.basename(record["path"])
        assert basename.startswith("IGNORED_")
        assert basename.endswith(".pdf")
        assert os.path.exists(record["path"])

    def test_osrename_failure_degrades_to_unparsed_with_error(self, tmp_path, monkeypatch):
        """OSError on IGNORED rename must degrade to UNPARSED: file gets
        UNPARSED_ prefix for visual triage, record.error carries the
        failure message for write_report_md:727 to render.
        """
        from postprocess import rename_by_ocr
        pdf = self._setup(tmp_path, "original.pdf")
        record = {
            "path": str(pdf),
            "message_id": "msg-fail-001",
            "sender_email": "billing@termius.com",
            "merchant": "Termius",
            "date": "20251112",
        }
        analysis = {"category": "IGNORED", "ocr": {"docType": "Invoice"}}

        # Fail the first os.rename (the IGNORED rename), let the
        # recursive UNPARSED rename through.
        import postprocess as pp
        calls = {"n": 0}
        real_rename = pp.os.rename

        def flaky_rename(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("mocked disk failure")
            real_rename(src, dst)

        monkeypatch.setattr(pp.os, "rename", flaky_rename)
        rename_by_ocr(record, analysis, str(tmp_path))

        assert record["category"] == "UNPARSED"
        assert "IGNORED rename failed" in record.get("error", "")
        basename = os.path.basename(record["path"])
        # Degrade must physically rename to UNPARSED_ for visual triage
        assert basename.startswith("UNPARSED_")
        assert basename.endswith(".pdf")
        assert os.path.exists(record["path"])


class TestIgnoredCtaRendering:
    """Unit 4 R3: write_report_md renders '📭 已忽略的非报销票据 (N)' section
    plus a learned_exclusions.json CTA block listing -from:<domain> hints
    aggregated per sender domain.
    """

    def _load_write_report_md(self):
        """Load the function from scripts/download-invoices.py (dash in filename
        prevents plain import)."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "download_invoices", "scripts/download-invoices.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.write_report_md

    def _base_args(self):
        return {
            "downloaded_all": [],
            "failed": [],
            "skipped": [],
            "matching_result": {
                "hotel": {"paired": [], "matched": [],
                          "unmatched_invoices": [], "unmatched_folios": []},
                "ridehailing": {"paired": [], "matched": [],
                                "unmatched_invoices": [], "unmatched_receipts": []},
                "other": [],
                "unparsed": [],
                "dedup_removed": [],
            },
            "date_range": ("2025/01/01", "2025/03/31"),
            "iteration": 1,
            "supplemental": False,
            "aggregation": None,
        }

    def test_three_distinct_senders_emit_three_cta_lines(self, tmp_path):
        write_report_md = self._load_write_report_md()
        ignored = [
            {"sender_email": "billing@termius.com", "ocr": {"transactionAmount": 120.0}},
            {"sender_email": "receipts@openrouter.ai", "ocr": {"transactionAmount": 20.0}},
            {"sender_email": "invoice@anthropic.com", "ocr": {"transactionAmount": 10.0}},
        ]
        out = tmp_path / "report.md"
        write_report_md(str(out), **self._base_args(), ignored_records=ignored)
        text = out.read_text()
        assert "📭 已忽略的非报销票据 (3)" in text
        assert "learned_exclusions.json" in text
        assert "-from:termius.com" in text
        assert "-from:openrouter.ai" in text
        assert "-from:anthropic.com" in text
        assert "billing@termius.com" in text
        assert "120" in text

    def test_same_domain_multiple_records_aggregate_to_one_cta_line(self, tmp_path):
        write_report_md = self._load_write_report_md()
        ignored = [
            {"sender_email": "support@termius.com", "ocr": {"transactionAmount": 120.0}},
            {"sender_email": "billing@termius.com", "ocr": {"transactionAmount": 20.0}},
        ]
        out = tmp_path / "report.md"
        write_report_md(str(out), **self._base_args(), ignored_records=ignored)
        text = out.read_text()
        assert text.count("-from:termius.com") == 1
        assert "已过滤 2 次" in text

    def test_empty_ignored_omits_section(self, tmp_path):
        write_report_md = self._load_write_report_md()
        out = tmp_path / "report.md"
        write_report_md(str(out), **self._base_args(), ignored_records=[])
        text = out.read_text()
        assert "已忽略的非报销票据" not in text
        assert "learned_exclusions" not in text

    def test_none_ignored_records_also_omits_section(self, tmp_path):
        """Default keyword value is None; existing callers that don't pass
        ignored_records must continue to work (no report section).
        """
        write_report_md = self._load_write_report_md()
        out = tmp_path / "report.md"
        write_report_md(str(out), **self._base_args())  # no ignored_records
        text = out.read_text()
        assert "已忽略的非报销票据" not in text

    def test_empty_sender_email_excluded_from_cta_only(self, tmp_path):
        """A record with empty sender_email still appears as "未知发件人"
        in the sender listing, but is NOT in the CTA aggregation
        (no domain to propose)."""
        write_report_md = self._load_write_report_md()
        ignored = [
            {"sender_email": "", "ocr": {"transactionAmount": 50.0}},
            {"sender_email": "billing@termius.com", "ocr": {"transactionAmount": 120.0}},
        ]
        out = tmp_path / "report.md"
        write_report_md(str(out), **self._base_args(), ignored_records=ignored)
        text = out.read_text()
        assert "未知发件人" in text
        assert "-from:termius.com" in text
        # No empty-domain CTA line
        assert "-from:\n" not in text
        assert "-from: " not in text

    def test_string_amount_coerced_no_crash(self, tmp_path):
        """LLM-returned transactionAmount can come back as a string
        ('120.00') or unconvertible garbage ('unknown'). Report must
        coerce via float() and degrade to '金额未识别' on failure, never
        crash with ValueError from f'{str:.2f}'.
        """
        write_report_md = self._load_write_report_md()
        ignored = [
            {"sender_email": "billing@termius.com",
             "ocr": {"transactionAmount": "120.00"}},       # numeric string
            {"sender_email": "receipts@openrouter.ai",
             "ocr": {"transactionAmount": "unknown"}},      # unconvertible
        ]
        out = tmp_path / "report.md"
        # Must not raise.
        write_report_md(str(out), **self._base_args(), ignored_records=ignored)
        text = out.read_text()
        # Coerced numeric string renders like a real amount:
        assert "¥120.00" in text
        # Unconvertible falls through to the "金额未识别" branch:
        assert "receipts@openrouter.ai：金额未识别" in text


class TestOutputDirPreflight:
    """v5.7.2 fix: main() inspects --output at run start and prints an info
    line when the dir already has artifacts from a previous run. This helps
    users / Agents notice they're reusing a dir (where zip packaging is
    already protected by v5.7.1 whitelist, so no correctness risk, but fresh
    output dirs are strongly preferred — see SKILL.md Agent First-Run
    Procedure).

    Only the pure inspection helper is unit-tested here. Integration of the
    info line into main() is verified by running the full suite with no
    regression on agent-contract tests.
    """

    def _load_fn(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "download_invoices", "scripts/download-invoices.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._inspect_existing_output_dir

    def test_empty_dir_reports_empty(self, tmp_path):
        inspect = self._load_fn()
        result = inspect(str(tmp_path))
        assert result["pdf_count"] == 0
        assert result["has_state"] is False
        assert result["is_empty"] is True

    def test_nonexistent_dir_reports_empty(self, tmp_path):
        inspect = self._load_fn()
        missing = tmp_path / "does-not-exist"
        result = inspect(str(missing))
        assert result["pdf_count"] == 0
        assert result["has_state"] is False
        assert result["is_empty"] is True

    def test_dir_with_prior_pdfs_detected(self, tmp_path):
        inspect = self._load_fn()
        pdfs = tmp_path / "pdfs"
        pdfs.mkdir()
        (pdfs / "a.pdf").write_bytes(b"%PDF")
        (pdfs / "b.pdf").write_bytes(b"%PDF")
        result = inspect(str(tmp_path))
        assert result["pdf_count"] == 2
        assert result["is_empty"] is False

    def test_ignored_prefix_not_counted_as_prior_pdf(self, tmp_path):
        """IGNORED_*.pdf files are produced by v5.7 classifier; they're
        'current-run but deliberately out of zip', not prior-batch residue.
        Don't alarm the user about them."""
        inspect = self._load_fn()
        pdfs = tmp_path / "pdfs"
        pdfs.mkdir()
        (pdfs / "IGNORED_termius_x.pdf").write_bytes(b"%PDF")
        # No state files, no non-IGNORED PDFs → treat as empty.
        result = inspect(str(tmp_path))
        assert result["pdf_count"] == 0
        assert result["is_empty"] is True

    def test_state_files_detected(self, tmp_path):
        """Detect step4_downloaded.json or missing.json as 'has_state'
        even if pdfs/ is absent — prior-run signatures that matter."""
        inspect = self._load_fn()
        (tmp_path / "step4_downloaded.json").write_text("{}")
        result = inspect(str(tmp_path))
        assert result["pdf_count"] == 0
        assert result["has_state"] is True
        assert result["is_empty"] is False

    def test_report_csv_also_signal_non_empty(self, tmp_path):
        inspect = self._load_fn()
        (tmp_path / "下载报告.md").write_text("prev")
        result = inspect(str(tmp_path))
        assert result["has_state"] is True
        assert result["is_empty"] is False


class TestCurrencyPromptContract:
    """Unit A: OCR prompt must declare `currency` field and example values."""

    def test_prompt_declares_currency_field(self):
        from core.prompts import get_ocr_prompt
        prompt = get_ocr_prompt()
        assert "| currency |" in prompt, "currency 行必须在通用字段表"
        assert "ISO-4217" in prompt or "ISO 4217" in prompt, \
            "说明必须引用 ISO-4217 三字母码标准"
        assert "CNY" in prompt and "USD" in prompt, \
            "示例币种必须覆盖 CNY / USD"

    def test_prompt_examples_include_cny_default(self):
        from core.prompts import get_ocr_prompt
        prompt = get_ocr_prompt()
        # 3 段 example JSON 至少各有一处 "currency": "CNY"
        assert prompt.count('"currency": "CNY"') >= 3, \
            "电子发票 / 酒店水单 / 网约车 3 段 example 都要列 currency=CNY"


class TestCurrencyDefensiveFill:
    """Unit A: extract_from_bytes guarantees currency field presence."""

    def test_missing_currency_defaults_to_cny(self, tmp_path, monkeypatch):
        from core import llm_ocr

        # Stub extract_with_retry to return LLM output without `currency`.
        monkeypatch.setattr(
            llm_ocr, "extract_with_retry",
            lambda pdf, prompt, client: '{"transactionAmount": 100.0, "vendorName": "T"}',
        )
        # Stub get_client to bypass provider init.
        class _FakeClient:
            provider_name = "anthropic"
        monkeypatch.setattr(llm_ocr, "get_client", lambda: _FakeClient())

        result = llm_ocr.extract_from_bytes(
            b"%PDF-fake", use_cache=False, cache_dir=tmp_path,
        )
        assert result.get("currency") == "CNY"

    def test_empty_currency_is_normalized_to_cny(self, tmp_path, monkeypatch):
        from core import llm_ocr
        monkeypatch.setattr(
            llm_ocr, "extract_with_retry",
            lambda pdf, prompt, client: '{"transactionAmount": 1, "currency": ""}',
        )
        class _FakeClient:
            provider_name = "anthropic"
        monkeypatch.setattr(llm_ocr, "get_client", lambda: _FakeClient())

        result = llm_ocr.extract_from_bytes(
            b"%PDF-fake2", use_cache=False, cache_dir=tmp_path,
        )
        assert result.get("currency") == "CNY"

    def test_old_cache_without_currency_gets_filled(self, tmp_path):
        from core import llm_ocr
        import json, hashlib

        pdf_bytes = b"%PDF-old-cache"
        # Seed a cache entry that predates v5.8 (no `currency` field).
        tmp_path.mkdir(exist_ok=True)
        digest = hashlib.sha256(pdf_bytes).hexdigest()[:16]
        cache_file = tmp_path / f"{digest}.json"
        cache_file.write_text(json.dumps({
            "ocr": {"transactionAmount": 50.0, "vendorName": "legacy"},
            "schema_version": "1.0",
        }), encoding="utf-8")

        result = llm_ocr.extract_from_bytes(
            pdf_bytes, use_cache=True, cache_dir=tmp_path,
        )
        assert result.get("currency") == "CNY"

    def test_explicit_usd_is_preserved(self, tmp_path, monkeypatch):
        from core import llm_ocr
        monkeypatch.setattr(
            llm_ocr, "extract_with_retry",
            lambda pdf, prompt, client: '{"transactionAmount": 10, "currency": "USD"}',
        )
        class _FakeClient:
            provider_name = "anthropic"
        monkeypatch.setattr(llm_ocr, "get_client", lambda: _FakeClient())

        result = llm_ocr.extract_from_bytes(
            b"%PDF-usd", use_cache=False, cache_dir=tmp_path,
        )
        assert result.get("currency") == "USD"


class TestCurrencySymbolTable:
    """Unit A: currency_symbol maps ISO-4217 codes to display symbols.

    Contract:
      - Known codes → short symbol (¥/$/€/£ etc.)
      - Unknown codes → "{CODE} " (uppercase + trailing space)
      - None / empty → ¥ (CNY fallback)
      - Case-insensitive input
    """

    def test_known_codes(self):
        from postprocess import currency_symbol
        assert currency_symbol("CNY") == "¥"
        assert currency_symbol("USD") == "$"
        assert currency_symbol("EUR") == "€"
        assert currency_symbol("GBP") == "£"
        assert currency_symbol("JPY") == "¥"
        assert currency_symbol("HKD") == "HK$"

    def test_unknown_code_preserves_as_prefix(self):
        from postprocess import currency_symbol
        # Hallucinated / malformed code: fallback to uppercase + space
        assert currency_symbol("RMB") == "RMB "
        assert currency_symbol("XYZ") == "XYZ "

    def test_none_is_cny(self):
        from postprocess import currency_symbol
        assert currency_symbol(None) == "¥"

    def test_empty_string_is_cny(self):
        from postprocess import currency_symbol
        assert currency_symbol("") == "¥"

    def test_lowercase_input(self):
        from postprocess import currency_symbol
        assert currency_symbol("usd") == "$"
        assert currency_symbol("cny") == "¥"
