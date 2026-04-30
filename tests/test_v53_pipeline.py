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
    LLMClient,
    LLMRateLimitError,
    extract_with_retry,
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
from v53_pipeline import (
    CATEGORY_LABELS,
    _compute_convergence_hash,
    analyze_pdf_batch,
    do_all_matching,
    merge_supplemental_downloads,
    normalize_date,
    rename_by_ocr,
    sanitize_filename,
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
        pdf_path = "/Users/kentpeng/Documents/agent Test/滴滴电子发票 (1).pdf"
        if not os.path.exists(pdf_path):
            pytest.skip("fixture missing")
        ocr = {"transactionAmount": 999999.00, "transactionDate": "2025-12-09"}
        result = validate_ocr_plausibility(ocr, pdf_path=pdf_path)
        assert result.get("_amountConfidence") == "low"

    def test_plausible_amount_not_flagged(self):
        # We don't know the real amount in the fixture without running LLM,
        # but we can test with a synthetic pdf + known text.
        import subprocess
        pdf_path = "/Users/kentpeng/Documents/agent Test/滴滴电子发票 (1).pdf"
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


# =============================================================================
# #19 HIGH — CSV UTF-8 BOM + None-safe
# =============================================================================

class TestSummaryCSV:
    def test_utf8_bom_prefix(self, tmp_path):
        csv_path = tmp_path / "summary.csv"
        records = [{
            "path": "a.pdf", "valid": True, "category": "HOTEL_INVOICE",
            "ocr": {
                "transactionDate": "2026-03-19",
                "transactionAmount": 1280.00,
                "vendorName": "无锡万怡",
            },
        }]
        write_summary_csv(str(csv_path), records)
        assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf"), "missing UTF-8 BOM"

    def test_none_amount_is_empty_not_zero(self, tmp_path):
        csv_path = tmp_path / "summary.csv"
        records = [{
            "path": "a.pdf", "valid": True, "category": "MEAL",
            "ocr": {"transactionDate": "2026-03-15", "transactionAmount": None, "vendorName": "V"},
        }]
        write_summary_csv(str(csv_path), records)
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
             "ocr": {"transactionDate": "2026-03-19", "transactionAmount": 100, "vendorName": "H"}},
        ]
        write_summary_csv(str(csv_path), records)
        text = csv_path.read_text(encoding="utf-8-sig")
        lines = text.strip().split("\n")
        # Row 1 is header, row 2 is first record, row 3 is last record
        assert "HOTEL" in lines[1] or "酒店" in lines[1]
        assert "需人工核查" in lines[2]


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
        n_csv = write_summary_csv(str(csv_path), records)
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
