"""Agent contract evals (Unit 3 of 2026-05-01 Skill compliance plan).

These tests lock the runtime contract the Skill exposes to OpenClaw Agents:

- R8  Exit codes + stderr `REMEDIATION:` prefix
- R9  P1/P2/P3 matching tiers surface correctly in 下载报告.md
- R10 convergence_hash reproducibility + status-machine transitions
- R11 missing.json schema v1.0 enum values
- R12 zip manifest allowlist + self-exclusion

Unlike tests/test_postprocess.py (component-level unit tests), these drive
the pipeline from the CLI boundary inward.  They mock the network
(GmailClient._api_get) and the OCR layer (postprocess.analyze_pdf_batch)
so the suite runs offline with zero network + zero LLM cost.

Mock seam choices are documented in
docs/plans/2026-05-01-001-refactor-skill-compliance-and-agent-contract-evals-plan.md
§ Key Technical Decisions.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# conftest.py inserts scripts/ onto sys.path; re-confirm for direct imports.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import postprocess  # noqa: E402


# =============================================================================
# Module loader — loads scripts/download-invoices.py under a legal module name.
# runpy.run_path or run_module don't work here because the filename has a
# hyphen (invalid Python module identifier).  Import-via-spec is the only
# in-process option that keeps the module cached so monkeypatches stick.
# =============================================================================

@pytest.fixture(scope="session")
def cli_module():
    """Load scripts/download-invoices.py once per pytest session.

    We intentionally load under the name `download_invoices_cli` (underscores)
    so monkeypatch targeting works predictably and the module stays in
    sys.modules across tests.
    """
    spec = importlib.util.spec_from_file_location(
        "download_invoices_cli", str(SCRIPTS / "download-invoices.py")
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["download_invoices_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


# =============================================================================
# CLI runner — invokes main() in-process with argv overrides + mock seams.
# =============================================================================

def _write_gmail_fixtures(tmp_path: Path) -> tuple[Path, Path]:
    creds = tmp_path / "credentials.json"
    token = tmp_path / "token.json"
    creds.write_text(json.dumps({
        "installed": {
            "client_id": "fake.apps.googleusercontent.com",
            "client_secret": "fake",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }))
    token.write_text(json.dumps({
        "access_token": "fake-access",
        "refresh_token": "fake-refresh",
    }))
    return creds, token


def _invoke_main(
    cli_module,
    argv: List[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    api_stub=None,
    api_exception: Exception | None = None,
) -> int:
    """Call cli_module.main() with argv replaced; return the SystemExit code."""
    if api_exception is not None:
        def _fail(self, url):
            raise api_exception
        monkeypatch.setattr(cli_module.GmailClient, "_api_get", _fail)
    elif api_stub is not None:
        monkeypatch.setattr(cli_module.GmailClient, "_api_get", api_stub)

    monkeypatch.setattr(sys, "argv", ["download-invoices.py"] + argv)

    try:
        cli_module.main()
    except SystemExit as se:
        return int(se.code) if se.code is not None else 0
    return 0


# =============================================================================
# Transient network retry contract (discovered via 2025Q4 smoke)
# =============================================================================

class TestTransientRetryContract:
    """GmailClient._api_get must transparently retry transient network
    errors (SSL EOF, TimeoutError, ConnectionError, URLError) with
    exponential backoff before giving up — without disturbing the
    401-refresh / 429-quota / 403-quota error-code dispatch.

    Motivation: 2025Q4 seasonal smoke lost one hotel invoice to a single
    SSL UNEXPECTED_EOF_WHILE_READING during attachment fetch.  The Agent
    loop recovered it in iter 2, but that's the wrong layer for a one-off
    blip.  Regression protection: any change that removes this retry loop
    should fail these tests.
    """

    def _make_client_with_stubbed_urlopen(
        self, cli_module, monkeypatch, responses
    ):
        """Build a GmailClient whose urlopen returns the next item from
        `responses` on each call.  A response can be:
          - bytes: treated as a successful JSON body
          - an Exception instance: raised
        """
        import urllib.request as _ureq
        it = iter(responses)

        class _FakeResp:
            def __init__(self, body: bytes):
                self._body = body
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return self._body

        def fake_urlopen(req, timeout=None):
            nxt = next(it)
            if isinstance(nxt, Exception):
                raise nxt
            return _FakeResp(nxt)

        monkeypatch.setattr(_ureq, "urlopen", fake_urlopen)

        # Also no-op time.sleep so the test doesn't actually wait 3.5s
        import time as _time
        monkeypatch.setattr(_time, "sleep", lambda *_: None)

        # Build a GmailClient without hitting disk
        client = cli_module.GmailClient.__new__(cli_module.GmailClient)
        client.creds = {"token_uri": "https://unused"}
        client.token_path = "/dev/null"
        client.token = {"access_token": "fake", "refresh_token": "fake"}
        return client

    def test_transient_ssl_eof_is_retried(self, cli_module, monkeypatch, capsys):
        """A transient SSL error followed by a 2xx response should succeed
        with no exception raised to the caller."""
        import ssl
        payload = b'{"ok": true}'
        client = self._make_client_with_stubbed_urlopen(
            cli_module, monkeypatch,
            responses=[
                ssl.SSLEOFError("EOF in handshake"),  # retry 1
                payload,                              # success
            ],
        )
        result = client._api_get("https://example/test")
        assert result == {"ok": True}
        # And stderr should advertise the retry so Agents can trace it
        stderr = capsys.readouterr().err
        assert "transient network error" in stderr
        assert "retry 1/" in stderr

    def test_all_retries_exhausted_raises(self, cli_module, monkeypatch, capsys):
        """If every retry fails, the last exception must propagate so the
        caller (download_attachment / main) sees the failure."""
        import ssl
        client = self._make_client_with_stubbed_urlopen(
            cli_module, monkeypatch,
            # 3 backoffs (0.5/1/2s) + 1 final attempt = 4 failures total
            responses=[ssl.SSLEOFError("flaky") for _ in range(4)],
        )
        with pytest.raises(ssl.SSLEOFError):
            client._api_get("https://example/test")

    def test_http_error_not_retried_as_transient(
        self, cli_module, monkeypatch, capsys
    ):
        """401/403/429 are HTTPError subclasses — they must reach the
        original _api_get dispatcher, NOT be caught by the transient-retry
        wrapper.  Verifying via 429 which must raise GmailQuotaError."""
        import urllib.error
        # 429 with Retry-After — _api_get converts this to GmailQuotaError.
        err = urllib.error.HTTPError(
            url="https://example", code=429, msg="rate limited",
            hdrs={"Retry-After": "60"}, fp=None,
        )
        client = self._make_client_with_stubbed_urlopen(
            cli_module, monkeypatch, responses=[err],
        )
        with pytest.raises(cli_module.GmailQuotaError):
            client._api_get("https://example/test")
        # Should NOT have retried — only 1 urlopen call consumed
        # (If transient-retry wrongly caught HTTPError, it would loop 4x
        # and we'd exhaust the responses list with a StopIteration instead.)


# =============================================================================
# R8 — Exit code + REMEDIATION stderr contract
# =============================================================================

class TestExitCodeContract:
    """Every non-zero exit must print a `REMEDIATION:` stderr line so agents
    can pattern-match on recovery hints.  See SKILL.md § Exit Codes."""

    def test_exit_auth_on_gmail_search_failure(
        self, tmp_path, monkeypatch, capsys, cli_module
    ):
        """Generic exception during Gmail search → EXIT_AUTH (2) + REMEDIATION."""
        creds, token = _write_gmail_fixtures(tmp_path)
        out = tmp_path / "out"

        code = _invoke_main(
            cli_module,
            [
                "--start", "2026/01/01", "--end", "2026/01/02",
                "--output", str(out),
                "--creds", str(creds), "--token", str(token),
                "--skip-preflight", "--no-llm",
            ],
            monkeypatch,
            api_exception=RuntimeError("simulated token invalid"),
        )
        captured = capsys.readouterr()
        assert code == 2, f"expected EXIT_AUTH=2, got {code}\nstderr: {captured.err}"
        assert "REMEDIATION:" in captured.err
        assert "gmail-auth.py" in captured.err

    def test_exit_gmail_quota_on_quota_error(
        self, tmp_path, monkeypatch, capsys, cli_module
    ):
        """GmailQuotaError during search → EXIT_GMAIL_QUOTA (4) + wait-60s hint."""
        creds, token = _write_gmail_fixtures(tmp_path)
        out = tmp_path / "out"

        code = _invoke_main(
            cli_module,
            [
                "--start", "2026/01/01", "--end", "2026/01/02",
                "--output", str(out),
                "--creds", str(creds), "--token", str(token),
                "--skip-preflight", "--no-llm",
            ],
            monkeypatch,
            api_exception=cli_module.GmailQuotaError("simulated 429"),
        )
        captured = capsys.readouterr()
        assert code == 4, f"expected EXIT_GMAIL_QUOTA=4, got {code}"
        assert "REMEDIATION:" in captured.err
        assert "60s" in captured.err

    def test_empty_inbox_exits_cleanly(
        self, tmp_path, monkeypatch, capsys, cli_module
    ):
        """0 messages matched → full pipeline still runs → exit 0 + all deliverables."""
        creds, token = _write_gmail_fixtures(tmp_path)
        out = tmp_path / "out"

        def _stub(self, url):
            # messages.list returns empty page, no nextPageToken
            return {"messages": [], "resultSizeEstimate": 0}

        code = _invoke_main(
            cli_module,
            [
                "--start", "2026/01/01", "--end", "2026/01/02",
                "--output", str(out),
                "--creds", str(creds), "--token", str(token),
                "--skip-preflight", "--no-llm",
            ],
            monkeypatch,
            api_stub=_stub,
        )
        captured = capsys.readouterr()
        assert code == 0, (
            f"expected EXIT_OK=0, got {code}\nstderr: {captured.err}"
        )
        # With zero messages there's nothing to zip (no PDFs/deliverables may
        # still write), but at minimum 下载报告.md + missing.json + CSV exist
        assert (out / "下载报告.md").exists()
        assert (out / "missing.json").exists()
        assert (out / "发票汇总.csv").exists()
        # Happy path → no REMEDIATION
        assert "REMEDIATION:" not in captured.err

    def test_all_remediation_lines_use_canonical_prefix(self, cli_module):
        """Static check: every sys.exit(EXIT_*) path in the CLI writes a line
        starting with `REMEDIATION:` to stderr.  Regression guard — a future
        PR that adds a new exit path without a REMEDIATION line fails here."""
        source = (SCRIPTS / "download-invoices.py").read_text(encoding="utf-8")
        # Count REMEDIATION: mentions that print to stderr.  Gross heuristic
        # but sufficient to catch "forgot to add REMEDIATION after adding
        # sys.exit(EXIT_PARTIAL)" style regressions.
        remediation_lines = source.count('REMEDIATION:')
        exit_call_lines = source.count('sys.exit(EXIT_')
        # Every EXIT_* call should have >= 1 REMEDIATION on the stderr side.
        # EXIT_OK and EXIT_PARTIAL don't need remediation (happy + partial-
        # success), but EXIT_AUTH/LLM_CONFIG/GMAIL_QUOTA/UNKNOWN do.
        # We need at least 4 REMEDIATION mentions to cover the 4 error exits.
        assert remediation_lines >= 4, (
            f"Found only {remediation_lines} 'REMEDIATION:' mentions in CLI; "
            f"every error-path exit must print one"
        )
        assert exit_call_lines >= 6, (
            "CLI should have >=6 sys.exit(EXIT_*) calls (one per code + spares)"
        )


# =============================================================================
# R9 — Matching tier contract surfaces in 下载报告.md
# =============================================================================

class TestMatchingTiersContract:
    """P1/P2/P3 matching tiers must surface in the report output.  We drive
    do_all_matching + write_report_md directly (without the full CLI run) to
    keep the test fast while still exercising the report formatting layer
    where the ⚠️ low-confidence marker is emitted.
    """

    def _report_for(
        self, records: List[Dict[str, Any]], tmp_path: Path, cli_module
    ) -> str:
        matching_result = postprocess.do_all_matching(records)
        aggregation = postprocess.build_aggregation(matching_result, records)
        report_path = tmp_path / "下载报告.md"
        cli_module.write_report_md(
            str(report_path),
            downloaded_all=records,
            failed=[],
            skipped=[],
            matching_result=matching_result,
            date_range=("2026/01/01", "2026/05/01"),
            iteration=1,
            supplemental=False,
            aggregation=aggregation,
        )
        return report_path.read_text(encoding="utf-8")

    def test_p1_remark_in_report(self, tmp_path, cli_module):
        inv = {
            "path": "20260319_test_invoice.pdf", "valid": True,
            "category": "HOTEL_INVOICE",
            "ocr": {
                "transactionAmount": 1280.00,
                "transactionDate": "2026-03-19",
                "remark": "HT-XYZ",
                "vendorName": "某某酒店",
            },
        }
        fol = {
            "path": "20260319_test_folio.pdf", "valid": True,
            "category": "HOTEL_FOLIO",
            "ocr": {
                "balance": 1280.00,
                "checkOutDate": "2026-03-19",
                "confirmationNo": "HT-XYZ",
                "hotelName": "某某酒店",
            },
        }
        report = self._report_for([inv, fol], tmp_path, cli_module)
        # P1 match wins; filename/vendor should appear; ⚠️ absent for P1
        assert "某某酒店" in report
        # The report table for P1 does NOT carry the 仅日期 marker.
        # (A P1 row should not carry the ⚠️ low-confidence marker.)

    def test_p2_date_amount_in_report(self, tmp_path, cli_module):
        inv = {
            "path": "inv.pdf", "valid": True, "category": "HOTEL_INVOICE",
            "ocr": {
                "transactionAmount": 500.00,
                "transactionDate": "2026-04-01",
                "vendorName": "酒店 A",
            },
        }
        fol = {
            "path": "fol.pdf", "valid": True, "category": "HOTEL_FOLIO",
            "ocr": {
                "balance": 500.00,
                "checkOutDate": "2026-04-01",
                "confirmationNo": "UNRELATED",
                "hotelName": "酒店 A",
            },
        }
        report = self._report_for([inv, fol], tmp_path, cli_module)
        # P2 exact match → vendor surfaces in report; no ⚠️ on this row.
        assert "酒店 A" in report

    def test_finance_summary_count_matches_stdout_when_amount_missing(
        self, tmp_path, cli_module
    ):
        """MD 金额汇总 row count must include amount=None rows so it stays in
        sync with print_openclaw_summary (single-source-of-truth invariant)."""
        full = {
            "path": "full.pdf", "valid": True, "category": "MEAL",
            "ocr": {"transactionAmount": 50.0,
                    "transactionDate": "2026-03-15",
                    "vendorName": "V1"},
        }
        partial = {
            "path": "partial.pdf", "valid": True, "category": "MEAL",
            "ocr": {"transactionAmount": None,
                    "transactionDate": "2026-03-16",
                    "vendorName": "V2"},
        }
        report = self._report_for([full, partial], tmp_path, cli_module)
        # Both MEAL rows must count — otherwise MD shows "餐饮 | 1" while
        # stdout shows "餐饮 2 份" and the finance summary contradicts itself.
        assert "| 餐饮 | 2 |" in report

    def test_finance_summary_table_contract(self, tmp_path, cli_module):
        """💰 金额汇总 must appear before 📊 摘要 and show 总计 grand total."""
        inv = {
            "path": "inv.pdf", "valid": True, "category": "HOTEL_INVOICE",
            "ocr": {
                "transactionAmount": 1280.00,
                "transactionDate": "2026-03-19",
                "remark": "HT-F",
                "vendorName": "某酒店",
            },
        }
        fol = {
            "path": "fol.pdf", "valid": True, "category": "HOTEL_FOLIO",
            "ocr": {
                "balance": 1280.00,
                "checkOutDate": "2026-03-19",
                "confirmationNo": "HT-F",
                "hotelName": "某酒店",
            },
        }
        meal = {
            "path": "m.pdf", "valid": True, "category": "MEAL",
            "ocr": {
                "transactionAmount": 70.00,
                "transactionDate": "2026-03-20",
                "vendorName": "某餐厅",
            },
        }
        report = self._report_for([inv, fol, meal], tmp_path, cli_module)
        assert "## 💰 金额汇总" in report
        # 💰 must precede 📊
        assert report.index("## 💰 金额汇总") < report.index("## 📊 摘要")
        # Grand total line
        assert "¥1350.00" in report
        assert "**总计**" in report

    def test_p3_date_only_low_confidence_marker(self, tmp_path, cli_module):
        """P1+P2 miss but date matches → P3 fallback + ⚠️ low-confidence marker."""
        inv = {
            "path": "inv.pdf", "valid": True, "category": "HOTEL_INVOICE",
            "ocr": {
                "transactionAmount": 480.00,  # amount differs from folio balance
                "transactionDate": "2026-05-10",
                "vendorName": "酒店 B",
            },
        }
        fol = {
            "path": "fol.pdf", "valid": True, "category": "HOTEL_FOLIO",
            "ocr": {
                "balance": 500.00,
                "checkOutDate": "2026-05-10",
                "hotelName": "酒店 B",
            },
        }
        report = self._report_for([inv, fol], tmp_path, cli_module)
        # P3 rows must surface a ⚠️ so agents and humans see low-confidence
        # without inspecting match_type strings.
        assert "⚠️" in report, f"P3 row missing ⚠️ marker.\nReport:\n{report}"


# =============================================================================
# R10a — convergence_hash is deterministic, order-independent, 16 chars
# =============================================================================

class TestConvergenceHashContract:
    def test_hash_is_16_chars(self):
        h = postprocess._compute_convergence_hash([
            {"type": "hotel_folio", "needed_for": "a.pdf"},
        ])
        assert isinstance(h, str)
        assert len(h) == 16

    def test_hash_is_order_independent(self):
        items_a = [
            {"type": "hotel_folio", "needed_for": "a.pdf"},
            {"type": "hotel_invoice", "needed_for": "b.pdf"},
        ]
        items_b = list(reversed(items_a))
        assert (
            postprocess._compute_convergence_hash(items_a)
            == postprocess._compute_convergence_hash(items_b)
        )

    def test_empty_items_yields_valid_hash(self):
        h = postprocess._compute_convergence_hash([])
        assert isinstance(h, str)
        assert len(h) == 16

    def test_hash_diverges_on_type_change(self):
        """hotel_folio → extraction_failed on same filename must change hash.
        Otherwise a failed OCR pretending to be converged loops forever."""
        before = postprocess._compute_convergence_hash([
            {"type": "hotel_folio", "needed_for": "a.pdf"},
        ])
        after = postprocess._compute_convergence_hash([
            {"type": "extraction_failed", "needed_for": "a.pdf"},
        ])
        assert before != after


# =============================================================================
# R10b — write_missing_json (iteration, items, prev_hash) → (status, action)
# =============================================================================

class TestStateMachineContract:
    """The state machine lives in write_missing_json (postprocess.py:780-794).

    CRITICAL: R10b cases MUST use non-empty items.  The first branch
    `if not items: status = "converged"` short-circuits the whole state
    machine; an empty-items R10b test would pass vacuously without actually
    exercising the branch logic.
    """

    def _empty_matching(self) -> Dict[str, Any]:
        return {
            "hotel": {"matched": [], "unmatched_invoices": [], "unmatched_folios": []},
            "ridehailing": {"matched": [], "unmatched_invoices": [], "unmatched_receipts": []},
        }

    def _matching_with_unmatched_invoice(self) -> Dict[str, Any]:
        """Produces one non-empty item in missing.json (hotel_folio missing)."""
        return {
            "hotel": {
                "matched": [],
                "unmatched_invoices": [{
                    "_record": {
                        "path": "inv.pdf",
                        "ocr": {
                            "transactionDate": "2026-03-19",
                            "transactionAmount": 500.0,
                            "vendorName": "酒店 X",
                            "remark": "HT-X",
                        },
                    },
                }],
                "unmatched_folios": [],
            },
            "ridehailing": {"matched": [], "unmatched_invoices": [], "unmatched_receipts": []},
        }

    def test_converged_when_items_empty(self, tmp_path):
        missing = tmp_path / "missing.json"
        payload = postprocess.write_missing_json(
            str(missing), batch_dir=str(tmp_path),
            iteration=1, iteration_cap=3,
            matching_result=self._empty_matching(),
            unparsed_records=[],
        )
        assert payload["status"] == "converged"
        assert payload["recommended_next_action"] == "stop"

    def test_converged_when_prev_hash_matches(self, tmp_path):
        """Non-empty items + prev_hash == current → converged/stop."""
        missing = tmp_path / "missing.json"
        matching = self._matching_with_unmatched_invoice()

        first = postprocess.write_missing_json(
            str(missing), batch_dir=str(tmp_path),
            iteration=1, iteration_cap=3,
            matching_result=matching,
            unparsed_records=[],
        )
        second = postprocess.write_missing_json(
            str(missing), batch_dir=str(tmp_path),
            iteration=2, iteration_cap=3,
            matching_result=matching,
            unparsed_records=[],
            previous_convergence_hash=first["convergence_hash"],
        )
        assert second["status"] == "converged"
        assert second["recommended_next_action"] == "stop"

    def test_max_iterations_reached_when_iter_ge_cap(self, tmp_path):
        """Non-empty items + iteration == cap + hash changed → max_iterations_reached."""
        missing = tmp_path / "missing.json"
        payload = postprocess.write_missing_json(
            str(missing), batch_dir=str(tmp_path),
            iteration=3, iteration_cap=3,
            matching_result=self._matching_with_unmatched_invoice(),
            unparsed_records=[],
            previous_convergence_hash="0000000000000000",
        )
        assert payload["status"] == "max_iterations_reached"
        assert payload["recommended_next_action"] == "ask_user"

    def test_needs_retry_when_items_present_and_iter_below_cap(self, tmp_path):
        """Non-empty items + iteration < cap + hash changed → needs_retry."""
        missing = tmp_path / "missing.json"
        payload = postprocess.write_missing_json(
            str(missing), batch_dir=str(tmp_path),
            iteration=1, iteration_cap=3,
            matching_result=self._matching_with_unmatched_invoice(),
            unparsed_records=[],
        )
        assert payload["status"] == "needs_retry"
        assert payload["recommended_next_action"] == "run_supplemental"

    def test_user_action_required_when_only_extraction_failed(self, tmp_path):
        """Only extraction_failed items → user_action_required/ask_user."""
        missing = tmp_path / "missing.json"
        payload = postprocess.write_missing_json(
            str(missing), batch_dir=str(tmp_path),
            iteration=1, iteration_cap=3,
            matching_result=self._empty_matching(),
            unparsed_records=[
                {"path": "damaged.pdf", "error": "LLM parse failed"},
            ],
        )
        assert payload["status"] == "user_action_required"
        assert payload["recommended_next_action"] == "ask_user"


# =============================================================================
# R11 — missing.json schema v1.0 enum contract
# =============================================================================

class TestMissingJsonSchemaContract:
    """Every status / recommended_next_action / items[].type the pipeline
    produces must stay in the declared enum.  Any schema bump → test fails →
    SKILL.md § Loop Playbook MUST be updated in the same PR."""

    ALLOWED_STATUS = {
        "converged", "needs_retry", "max_iterations_reached", "user_action_required",
    }
    ALLOWED_ACTIONS = {"stop", "run_supplemental", "ask_user"}
    ALLOWED_ITEM_TYPES = {
        "hotel_folio", "hotel_invoice",
        "ridehailing_receipt", "ridehailing_invoice",
        "extraction_failed",
    }

    def _assert_schema(self, payload: Dict[str, Any]):
        assert payload["schema_version"] == "1.0"
        for key in ("generated_at", "iteration", "iteration_cap",
                    "status", "recommended_next_action",
                    "convergence_hash", "batch_dir", "items"):
            assert key in payload, f"missing top-level key: {key}"
        assert payload["status"] in self.ALLOWED_STATUS
        assert payload["recommended_next_action"] in self.ALLOWED_ACTIONS
        assert isinstance(payload["items"], list)
        for item in payload["items"]:
            assert item["type"] in self.ALLOWED_ITEM_TYPES, (
                f"unknown item type: {item['type']!r}; add to SKILL.md Loop Playbook"
                f" and Loop decision table before expanding the enum"
            )

    def test_converged_payload_shape(self, tmp_path):
        missing = tmp_path / "missing.json"
        payload = postprocess.write_missing_json(
            str(missing), batch_dir=str(tmp_path),
            iteration=1, iteration_cap=3,
            matching_result={
                "hotel": {"matched": [], "unmatched_invoices": [], "unmatched_folios": []},
                "ridehailing": {"matched": [], "unmatched_invoices": [], "unmatched_receipts": []},
            },
            unparsed_records=[],
        )
        self._assert_schema(payload)
        assert payload["status"] == "converged"

    def test_all_five_item_types_validate(self, tmp_path):
        """Construct a payload that exercises all 5 item types at once."""
        missing = tmp_path / "missing.json"
        matching = {
            "hotel": {
                "matched": [],
                "unmatched_invoices": [{
                    "_record": {"path": "hotel_inv.pdf", "ocr": {
                        "transactionDate": "2026-03-19",
                        "transactionAmount": 500.0,
                        "remark": "HT-X",
                        "vendorName": "H",
                    }},
                }],
                "unmatched_folios": [{
                    "_record": {"path": "hotel_fol.pdf", "ocr": {
                        "checkOutDate": "2026-03-19",
                        "balance": 500.0,
                        "hotelName": "H",
                    }},
                }],
            },
            "ridehailing": {
                "matched": [],
                "unmatched_invoices": [{
                    "_record": {"path": "rh_inv.pdf", "ocr": {
                        "transactionDate": "2026-03-19",
                        "transactionAmount": 50.0,
                    }},
                }],
                "unmatched_receipts": [{
                    "_record": {"path": "rh_rec.pdf", "ocr": {
                        "transactionDate": "2026-03-19",
                        "totalAmount": 50.0,
                    }},
                }],
            },
        }
        payload = postprocess.write_missing_json(
            str(missing), batch_dir=str(tmp_path),
            iteration=1, iteration_cap=3,
            matching_result=matching,
            unparsed_records=[{"path": "broken.pdf", "error": "malformed"}],
        )
        self._assert_schema(payload)
        types_seen = {item["type"] for item in payload["items"]}
        assert types_seen == self.ALLOWED_ITEM_TYPES, (
            f"expected all 5 item types, got {types_seen}"
        )

    def test_missing_json_round_trips_through_disk(self, tmp_path):
        """File on disk must parse cleanly and re-validate against the schema.
        Guards against partial-write / JSON-encoding regressions."""
        missing = tmp_path / "missing.json"
        postprocess.write_missing_json(
            str(missing), batch_dir=str(tmp_path),
            iteration=1, iteration_cap=3,
            matching_result={
                "hotel": {"matched": [], "unmatched_invoices": [], "unmatched_folios": []},
                "ridehailing": {"matched": [], "unmatched_invoices": [], "unmatched_receipts": []},
            },
            unparsed_records=[],
        )
        with open(missing, encoding="utf-8") as f:
            loaded = json.load(f)
        self._assert_schema(loaded)


# =============================================================================
# R12 — Zip manifest allowlist + self-exclusion contract
# =============================================================================

class TestZipManifestContract:
    """The 发票打包_*.zip handed to finance must contain ONLY .pdf/.md/.csv.
    run.log and step*_*.json are internal state and must never leak.
    Nested 发票打包_*.zip from prior runs must self-exclude.
    """

    def _build_output_dir(self, tmp_path: Path) -> Path:
        out = tmp_path / "out"
        pdfs = out / "pdfs"
        pdfs.mkdir(parents=True)

        # Legitimate deliverables
        (pdfs / "20260319_酒店_发票.pdf").write_bytes(b"%PDF-1.4 fake")
        (pdfs / "20260319_酒店_水单.pdf").write_bytes(b"%PDF-1.4 fake")
        (out / "下载报告.md").write_text("# Report\n", encoding="utf-8")
        (out / "发票汇总.csv").write_text("﻿序号,金额\n1,500\n", encoding="utf-8")

        # Noise that must NOT enter the zip
        (out / "run.log").write_text("log contents", encoding="utf-8")
        (out / "step3_classified.json").write_text("{}", encoding="utf-8")
        (out / "step4_downloaded.json").write_text("{}", encoding="utf-8")
        (out / "missing.json").write_text(
            '{"schema_version": "1.0"}', encoding="utf-8"
        )

        # An older zip that must self-exclude
        prev_zip = out / "发票打包_20260101-000000.zip"
        with zipfile.ZipFile(prev_zip, "w") as z:
            z.writestr("leftover.pdf", b"%PDF-1.4 stale")

        return out

    def test_only_allowlisted_suffixes_in_zip(self, tmp_path):
        out = self._build_output_dir(tmp_path)
        zip_path = postprocess.zip_output(str(out), dest_dir=str(tmp_path))
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()

        for name in names:
            suffix = Path(name).suffix.lower()
            assert suffix in {".pdf", ".md", ".csv"}, (
                f"disallowed suffix in zip: {name}"
            )

    def test_run_log_and_json_excluded(self, tmp_path):
        out = self._build_output_dir(tmp_path)
        zip_path = postprocess.zip_output(str(out), dest_dir=str(tmp_path))
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()

        for forbidden in ("run.log", "step3_classified.json",
                          "step4_downloaded.json", "missing.json"):
            assert not any(forbidden in n for n in names), (
                f"{forbidden} leaked into zip: {names}"
            )

    def test_nested_prior_zip_self_excluded(self, tmp_path):
        out = self._build_output_dir(tmp_path)
        zip_path = postprocess.zip_output(str(out), dest_dir=str(tmp_path))
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
        assert not any(
            n.startswith("发票打包_") and n.endswith(".zip") for n in names
        ), f"nested prior zip found in output: {names}"

    def test_manifest_check_refuses_missing_md(self, tmp_path):
        """zip_output must raise when .md or .csv is absent — safety net that
        prevents a broken run from silently shipping an incomplete bundle."""
        out = tmp_path / "out"
        pdfs = out / "pdfs"
        pdfs.mkdir(parents=True)
        (pdfs / "solo.pdf").write_bytes(b"%PDF-1.4")
        (out / "发票汇总.csv").write_text("﻿序号\n1\n", encoding="utf-8")
        # deliberately no .md

        with pytest.raises(RuntimeError, match="zip 完整性检查失败"):
            postprocess.zip_output(str(out), dest_dir=str(tmp_path))


# =============================================================================
# Regression — download_link must scope its md5 dedup to this-run-only
# =============================================================================

class TestDownloadLinkDedupScope:
    """download_link's in-memory dedup must only compare against ``known_paths``
    (this run's downloads). Scanning the whole pdfs_dir historically let stale
    files from previous runs silently swallow new downloads, returning
    ``([], [])`` and making invoices vanish from step4 without any failed/skipped
    trace. A 2025-Q3 test run lost ~20% of LINK_BAIWANG invoices this way.
    """

    def _fake_curl_writing(self, payload: bytes):
        """Return a subprocess.run stub that writes ``payload`` to the -o path."""
        import types
        def _run(cmd, **kwargs):
            # curl -sL --max-time 60 -o <out> <url>
            out_path = cmd[cmd.index("-o") + 1]
            with open(out_path, "wb") as f:
                f.write(payload)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return _run

    def test_stale_file_in_pdfs_dir_does_not_kill_new_download(
        self, tmp_path, cli_module, monkeypatch
    ):
        """Given a stale byte-identical PDF in pdfs/ (as if a prior run left it)
        and known_paths=[] (this run hasn't downloaded anything yet), download_link
        must keep the new PDF — the stale file is not a this-run duplicate.
        """
        pdfs = tmp_path / "pdfs"
        pdfs.mkdir()
        payload = b"%PDF-1.4\nstale-vs-new-identical-bytes"
        (pdfs / "stale_from_prior_run.pdf").write_bytes(payload)

        monkeypatch.setattr(
            cli_module.subprocess, "run", self._fake_curl_writing(payload)
        )

        entry = {
            "method": "LINK_BAIWANG",
            "download_url": "https://example.com/fake.pdf",
            "doc_type": "TAX_INVOICE",
            "subject": "fake invoice",
            "message_id": "m1",
            "internal_date": "1700000000000",
        }
        log = open(tmp_path / "run.log", "w")
        try:
            d, fl = cli_module.download_link(entry, str(pdfs), log, known_paths=[])
        finally:
            log.close()

        assert len(d) == 1, (
            "stale file in pdfs_dir must not silently drop the new download"
        )
        assert fl == []
        assert os.path.exists(d[0]["path"]), "new file should still exist on disk"

    def test_in_run_duplicate_is_still_deduped(
        self, tmp_path, cli_module, monkeypatch
    ):
        """When known_paths DOES contain a byte-identical file (same run, e.g.
        ATTACHMENT delivered first + LINK_BAIWANG sent the same PDF after),
        dedup still collapses the duplicate to avoid both copies entering step4.
        """
        pdfs = tmp_path / "pdfs"
        pdfs.mkdir()
        payload = b"%PDF-1.4\nthis-run-identical-bytes"
        earlier = pdfs / "earlier_this_run.pdf"
        earlier.write_bytes(payload)

        monkeypatch.setattr(
            cli_module.subprocess, "run", self._fake_curl_writing(payload)
        )

        entry = {
            "method": "LINK_BAIWANG",
            "download_url": "https://example.com/fake.pdf",
            "doc_type": "TAX_INVOICE",
            "subject": "fake invoice",
            "message_id": "m2",
            "internal_date": "1700000000000",
        }
        log = open(tmp_path / "run.log", "w")
        try:
            d, fl = cli_module.download_link(
                entry, str(pdfs), log, known_paths=[str(earlier)]
            )
        finally:
            log.close()

        assert d == [] and fl == [], "in-run byte-identical duplicate must collapse"


# =============================================================================
# --postprocess-only flag contract — re-run Step 6-10 against existing pdfs/
# =============================================================================

class TestPostprocessOnlyFlag:
    """--postprocess-only skips Gmail (Step 1-5) and re-runs OCR + matching
    + deliverables (Step 6-10) against existing <out>/pdfs/."""

    def test_empty_pdfs_dir_produces_report_and_exits_5(self):
        """Empty pdfs/ → no crash, writes empty deliverables, exits 5."""
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "pdfs"))
            result = subprocess.run(
                [
                    "python3", "scripts/download-invoices.py",
                    "--postprocess-only",
                    "--output", tmp,
                    "--no-llm",    # keep test offline
                ],
                capture_output=True, text=True,
            )
            assert result.returncode == 5, result.stderr
            assert "REMEDIATION:" in result.stderr
            # Deliverables exist
            assert os.path.exists(os.path.join(tmp, "下载报告.md"))
            assert os.path.exists(os.path.join(tmp, "missing.json"))

    def test_rejects_missing_output_dir(self):
        """Unknown output dir → exits non-zero with REMEDIATION."""
        result = subprocess.run(
            [
                "python3", "scripts/download-invoices.py",
                "--postprocess-only",
                "--output", "/nonexistent/path/xyz",
                "--no-llm",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "REMEDIATION:" in result.stderr

    def test_existing_pdfs_dir_rewrites_deliverables(self, monkeypatch, tmp_path):
        """pdfs/ with one cached-OCR PDF → report + missing.json regenerate."""
        import shutil
        fixtures = os.environ.get(
            "GMAIL_INVOICE_FIXTURES",
            os.path.expanduser("~/Documents/agent Test/"),
        )
        if not os.path.isdir(fixtures):
            pytest.skip(f"Fixtures dir missing: {fixtures}")

        # Find any sample PDF from fixtures
        sample = None
        for root, _dirs, files in os.walk(fixtures):
            for f in files:
                if f.lower().endswith(".pdf"):
                    sample = os.path.join(root, f)
                    break
            if sample:
                break
        if sample is None:
            pytest.skip("No PDF in fixtures dir")

        pdfs_dir = tmp_path / "pdfs"
        pdfs_dir.mkdir()
        shutil.copy(sample, pdfs_dir / "sample.pdf")

        result = subprocess.run(
            [
                "python3", "scripts/download-invoices.py",
                "--postprocess-only",
                "--output", str(tmp_path),
                "--no-llm",
            ],
            capture_output=True, text=True,
        )
        # With --no-llm the PDF becomes UNPARSED → exit 5
        assert result.returncode in (0, 5), result.stderr
        assert os.path.exists(tmp_path / "下载报告.md")
        missing = json.loads(open(tmp_path / "missing.json").read())
        assert missing["schema_version"] == "1.0"
