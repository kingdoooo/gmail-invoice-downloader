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
from v53_pipeline import (
    CATEGORY_LABELS,
    _compute_convergence_hash,
    _to_matching_input,
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
        # Also patch the import reference in v53_pipeline
        import v53_pipeline
        monkeypatch.setattr(v53_pipeline, "get_client", raising_get_client)

        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        records = [{"path": str(pdf), "valid": True, "message_id": "m1"}]

        # Must raise, not return results with all-UNPARSED
        with pytest.raises(LLMAuthError):
            analyze_pdf_batch(records, use_llm=True)
