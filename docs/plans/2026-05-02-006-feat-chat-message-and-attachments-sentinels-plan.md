# CHAT_MESSAGE + CHAT_ATTACHMENTS Sentinels — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two stdout sentinels to `print_openclaw_summary` so the wrapping Agent can reliably (a) forward the user-facing summary verbatim and (b) upload the three deliverables (zip + MD + CSV) as attachments using the native message tool of whichever chat channel the Agent is in.

**Architecture:** Two independent sentinels emitted from `print_openclaw_summary`:
1. `CHAT_MESSAGE_START` / `CHAT_MESSAGE_END` — bare anchor lines (no colon, no payload) wrapping the full human-readable summary on every code path that reaches `print_openclaw_summary`, including the R16b empty-result early-return branch.
2. `CHAT_ATTACHMENTS:` — single-line JSON listing deliverable files with captions, emitted only on the R16a non-empty path, after `CHAT_MESSAGE_END`.

The Agent Playbook lives in SKILL.md as a new top-level section `## Presenting Results to the User`, inserted after `## Exit Codes`. The contract is **channel-agnostic** — Skill names no IM SDK.

**Tech Stack:** Python 3.10+ stdlib (`json`, existing `os`); pytest + pytest-mock for tests. No new runtime dependencies.

**Reference:** `docs/brainstorms/2026-05-02-chat-message-and-attachments-sentinels-requirements.md`

---

## File Structure

**Files modified (no new files):**
- `scripts/postprocess.py` — add sentinel constants + writer calls inside `print_openclaw_summary` (lines 1048–1167). About 20 lines total across 3 locations (function start, R16b return, function end).
- `SKILL.md` — add new `## Presenting Results to the User` section (~30 lines) between `## Exit Codes` (currently line 511) and `## Handling Unknown Platforms` (currently line 526).
- `tests/test_postprocess.py::TestPrintOpenClawSummary` — append 6 new test methods at the end of the existing class (class starts at line 2654). Reuses existing `_capture` / `_default_paths` / `_populated_agg` helpers.
- `tests/test_agent_contract.py` — append new `TestChatSentinelContract` class at the end of the file, plus an R8-style section comment at the top of the file listing the sentinels.

**Rationale for not splitting:** The change is a small, cohesive addition to one function. Adding helper modules would over-abstract. The function is already long (~120 lines) but the sentinel logic is trivial enough to inline without making it worse.

---

## Task 1: Add sentinel constants + message anchors to `print_openclaw_summary`

**Intent:** Introduce the two `CHAT_MESSAGE_*` anchor lines so the Agent can identify the user-facing text region on every code path. Attachments sentinel comes in Task 2.

**Files:**
- Modify: `scripts/postprocess.py` near line 1045 (constants) and around lines 1072, 1094, 1167 (writer calls)
- Test: `tests/test_postprocess.py::TestPrintOpenClawSummary` (append to class ending around line 2900)

- [ ] **Step 1: Write failing tests for the message boundary anchors**

Append to `tests/test_postprocess.py::TestPrintOpenClawSummary` (right before the last existing test in that class — find the final `def test_*` in the class and insert these after it, still inside the class):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_postprocess.py::TestPrintOpenClawSummary::test_chat_message_anchors_wrap_non_empty_summary tests/test_postprocess.py::TestPrintOpenClawSummary::test_chat_message_anchors_wrap_empty_template tests/test_postprocess.py::TestPrintOpenClawSummary::test_chat_message_boundary_includes_exclusions_invite -v`

Expected: **All 3 FAIL** with `assert 0 == 1` on `lines.count("CHAT_MESSAGE_START")` — sentinels don't exist yet.

- [ ] **Step 3: Add sentinel constants to `postprocess.py`**

Edit `scripts/postprocess.py`. Find this block (around line 1041–1046):

```python
# =============================================================================
# Step 10 — print_openclaw_summary (stdout-only OpenClaw chat summary)
# =============================================================================

_MISSING_STATUSES = frozenset({"stop", "run_supplemental", "ask_user"})
```

Replace with:

```python
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
```

- [ ] **Step 4: Emit `CHAT_MESSAGE_START` at function entry (after validation)**

Still in `scripts/postprocess.py`, find this block (around lines 1072–1082):

```python
    if missing_status not in _MISSING_STATUSES:
        raise ValueError(
            f"unknown missing_status {missing_status!r}; "
            f"allowed: {sorted(_MISSING_STATUSES)}"
        )

    unmatched = aggregation["unmatched"]
    voucher_count = aggregation["voucher_count"]
    low = aggregation["low_conf"]
    unmatched_any = any(v > 0 for v in unmatched.values())
    has_rows = bool(aggregation["rows"])
```

Replace with:

```python
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
```

- [ ] **Step 5: Emit `CHAT_MESSAGE_END` before the R16b empty-result return**

Still in `scripts/postprocess.py`, find the R16b block (around lines 1084–1094):

```python
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
        return
```

Replace with:

```python
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
```

- [ ] **Step 6: Emit `CHAT_MESSAGE_END` at the end of the R16a non-empty path**

Still in `scripts/postprocess.py`, find the last two `writer(...)` calls of the function (around lines 1166–1167):

```python
    writer("💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？")
    writer("   直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。")
```

Replace with:

```python
    writer("💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？")
    writer("   直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。")
    writer(CHAT_MESSAGE_END_SENTINEL)
```

- [ ] **Step 7: Run the 3 new tests to verify they now pass**

Run: `python3 -m pytest tests/test_postprocess.py::TestPrintOpenClawSummary::test_chat_message_anchors_wrap_non_empty_summary tests/test_postprocess.py::TestPrintOpenClawSummary::test_chat_message_anchors_wrap_empty_template tests/test_postprocess.py::TestPrintOpenClawSummary::test_chat_message_boundary_includes_exclusions_invite -v`

Expected: **3 PASS**.

- [ ] **Step 8: Run the full `TestPrintOpenClawSummary` class to verify no regression**

Run: `python3 -m pytest tests/test_postprocess.py::TestPrintOpenClawSummary -v`

Expected: **all tests PASS** (existing tests like `test_non_empty_template_shows_vouchers_and_total`, `test_empty_template_short`, `test_invite_present_in_non_empty_template` should still pass — the anchors are new lines that don't break any existing `in text` / `not in text` assertions).

- [ ] **Step 9: Commit**

```bash
git add scripts/postprocess.py tests/test_postprocess.py
git commit -m "feat(postprocess): add CHAT_MESSAGE_START/END anchors

Emit bare anchor lines around the user-facing summary on every
code path of print_openclaw_summary (R16a + R16b), enabling the
wrapping Agent to forward the message verbatim instead of
summarizing it selectively. Per docs/brainstorms/2026-05-02-
chat-message-and-attachments-sentinels-requirements.md."
```

---

## Task 2: Add `CHAT_ATTACHMENTS:` JSON sentinel

**Intent:** Emit a single-line JSON declaring which deliverable files should appear as chat attachments. Only on the R16a non-empty path; skipped on R16b. `zip_path=None` → attachments JSON omits the zip entry (DEC-6 degradation).

**Files:**
- Modify: `scripts/postprocess.py` — insert sentinel emission at the very end of `print_openclaw_summary` (after `CHAT_MESSAGE_END` on the R16a path)
- Test: `tests/test_postprocess.py::TestPrintOpenClawSummary` (append more tests after Task 1's tests)

- [ ] **Step 1: Write failing tests for the attachments sentinel**

Append to `tests/test_postprocess.py::TestPrintOpenClawSummary` (after the Task 1 tests):

```python
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
        # Order: zip, MD, CSV
        assert files[0]["caption"] == "报销包"
        assert files[1]["caption"] == "报告"
        assert files[2]["caption"] == "明细"
        # Paths are absolute
        for f in files:
            assert os.path.isabs(f["path"])
        # Paths match the inputs (zip_path is not wrapped in os.path.abspath
        # when passed in absolute; test helpers already pass absolute tmp_path)
        assert files[0]["path"] == os.path.abspath(paths["zip_path"])
        assert files[1]["path"] == os.path.abspath(paths["md_path"])
        assert files[2]["path"] == os.path.abspath(paths["csv_path"])

    def test_attachments_omits_zip_when_zip_failed(self, tmp_path):
        agg = self._populated_agg()
        paths = self._default_paths(tmp_path)
        paths["zip_path"] = None  # DEC-6 sentinel
        lines = self._capture(
            aggregation=agg, **paths,
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
        )
        payload = self._extract_attachments(lines)
        assert payload is not None
        files = payload["files"]
        assert len(files) == 2
        # No "报销包" entry when zip is absent
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
        # CHAT_MESSAGE anchors exist, but no attachments sentinel
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
        # Raw line contains Chinese glyphs, not \uXXXX escapes
        assert "报销包" in att_line
        # Still parses as JSON
        import json as _json
        payload = _json.loads(att_line[len("CHAT_ATTACHMENTS: "):])
        assert "files" in payload
```

Also ensure the imports at the top of `tests/test_postprocess.py` include `Optional` and `Any` from `typing` (they are already imported — check to be safe). If `Optional` or `Any` are not imported in the existing file header, add them.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_postprocess.py::TestPrintOpenClawSummary -k "attachments" -v`

Expected: **5 FAIL** — `_extract_attachments` returns `None` / `CHAT_ATTACHMENTS: ` line does not exist.

- [ ] **Step 3: Emit attachments sentinel on the R16a path**

Edit `scripts/postprocess.py`. The final lines of the function after Task 1 currently look like:

```python
    writer("💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？")
    writer("   直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。")
    writer(CHAT_MESSAGE_END_SENTINEL)
```

Replace with:

```python
    writer("💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？")
    writer("   直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。")
    writer(CHAT_MESSAGE_END_SENTINEL)

    # Agent contract: declare deliverables for the current chat. Order is
    # zip → MD → CSV. Skipped when zip failed? No — zip entry is skipped
    # but MD + CSV are still declared. Skipped entirely when we didn't
    # reach this line (R16b returned early, so no files to attach).
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
```

- [ ] **Step 4: Run the 5 new tests to verify they pass**

Run: `python3 -m pytest tests/test_postprocess.py::TestPrintOpenClawSummary -k "attachments" -v`

Expected: **5 PASS**.

- [ ] **Step 5: Run the full `TestPrintOpenClawSummary` class**

Run: `python3 -m pytest tests/test_postprocess.py::TestPrintOpenClawSummary -v`

Expected: **all tests PASS**.

- [ ] **Step 6: Run the full test suite to catch regressions**

Run: `python3 -m pytest tests/ -q`

Expected: **all 195+ tests PASS** (now ~201 with 6 new tests in Task 1 + Task 2). No failures in `TestPrintOpenClawSummary`, `TestBuildAggregation`, `TestAggregationConsistency`, `TestE2E`, or elsewhere.

- [ ] **Step 7: Commit**

```bash
git add scripts/postprocess.py tests/test_postprocess.py
git commit -m "feat(postprocess): emit CHAT_ATTACHMENTS JSON sentinel

Append a single-line JSON declaring deliverable files (zip, MD, CSV)
for the wrapping Agent to upload via the current channel's native
message tool. Omitted on R16b empty-result path. zip entry skipped
when zip_output failed (DEC-6). Captions: 报销包 / 报告 / 明细. Per
docs/brainstorms/2026-05-02-chat-message-and-attachments-sentinels-requirements.md."
```

---

## Task 3: Add contract-level tests in `test_agent_contract.py`

**Intent:** Lock the sentinel contract at the Agent-facing boundary so future refactors can't silently break the format. Mirrors existing R8 `REMEDIATION:` contract tests.

**Files:**
- Modify: `tests/test_agent_contract.py` — add R18 comment at the top + new `TestChatSentinelContract` class at the end

- [ ] **Step 1: Add the R18 reference comment at the top of `test_agent_contract.py`**

Find this block near line 1–10:

```python
"""Agent-facing contract tests.

R8  Exit codes + stderr `REMEDIATION:` prefix
...
"""
```

(Do a `grep -n '^"""' tests/test_agent_contract.py` to find the exact module docstring location.)

Add a new contract line:

```
R18 CHAT_MESSAGE_START / CHAT_MESSAGE_END / CHAT_ATTACHMENTS: sentinels
    (see SKILL.md § Presenting Results to the User)
```

If the file has no module docstring summarizing contracts, add the reference as a top-of-file comment block near the existing `R8` mention (line 5 per earlier inspection: `- R8  Exit codes + stderr REMEDIATION: prefix`). Preserve surrounding style.

- [ ] **Step 2: Write failing tests in `tests/test_agent_contract.py`**

Append to the end of `tests/test_agent_contract.py`:

```python
# =============================================================================
# R18 — CHAT_MESSAGE_START / END + CHAT_ATTACHMENTS: sentinel contract
# =============================================================================

class TestChatSentinelContract:
    """Agent-facing contract: print_openclaw_summary emits:
      - Exactly one CHAT_MESSAGE_START anchor (bare, no colon)
      - Exactly one CHAT_MESSAGE_END anchor (bare, no colon)
      - At most one CHAT_ATTACHMENTS: JSON line (R16a only)
      - Strict ordering: START < END < ATTACHMENTS (when all present)
      - CHAT_ATTACHMENTS: JSON schema: {"files":[{"path","caption"}, ...]}
    See SKILL.md § Presenting Results to the User.
    """

    @staticmethod
    def _run_and_capture(populate: bool, zip_ok: bool = True):
        """Drive print_openclaw_summary with a sink writer and return lines."""
        import sys as _sys
        import os as _os
        _sys.path.insert(
            0, _os.path.join(_os.path.dirname(__file__), "..", "scripts")
        )
        from postprocess import (  # type: ignore
            print_openclaw_summary,
            build_aggregation,
            do_all_matching,
        )

        if populate:
            recs = [
                {
                    "path": "/out/pdfs/m.pdf",
                    "valid": True,
                    "category": "MEAL",
                    "ocr": {
                        "transactionDate": "2026-04-01",
                        "transactionAmount": 100.0,
                        "vendorName": "V1",
                    },
                },
            ]
        else:
            recs = []
        matching = do_all_matching(recs)
        agg = build_aggregation(matching, recs)

        sink = []
        print_openclaw_summary(
            aggregation=agg,
            output_dir="/tmp/contract_test",
            zip_path="/tmp/contract_test/p.zip" if zip_ok else None,
            csv_path="/tmp/contract_test/发票汇总.csv",
            md_path="/tmp/contract_test/下载报告.md",
            log_path="/tmp/contract_test/run.log",
            missing_status="stop",
            date_range=("2026/04/01", "2026/04/30"),
            writer=lambda s: sink.append(s),
        )
        return sink

    def test_anchors_exactly_once_on_non_empty_path(self):
        lines = self._run_and_capture(populate=True)
        assert lines.count("CHAT_MESSAGE_START") == 1
        assert lines.count("CHAT_MESSAGE_END") == 1

    def test_anchors_exactly_once_on_empty_path(self):
        lines = self._run_and_capture(populate=False)
        assert lines.count("CHAT_MESSAGE_START") == 1
        assert lines.count("CHAT_MESSAGE_END") == 1

    def test_anchors_are_bare_no_colon_no_payload(self):
        """Anchors must be literal strings, not prefix:payload."""
        lines = self._run_and_capture(populate=True)
        start_lines = [ln for ln in lines if "CHAT_MESSAGE_START" in ln]
        end_lines = [ln for ln in lines if "CHAT_MESSAGE_END" in ln]
        assert start_lines == ["CHAT_MESSAGE_START"]
        assert end_lines == ["CHAT_MESSAGE_END"]

    def test_attachments_at_most_once(self):
        lines = self._run_and_capture(populate=True)
        prefix_hits = [
            ln for ln in lines if ln.startswith("CHAT_ATTACHMENTS: ")
        ]
        assert len(prefix_hits) == 1

    def test_attachments_absent_on_empty_path(self):
        lines = self._run_and_capture(populate=False)
        prefix_hits = [
            ln for ln in lines if ln.startswith("CHAT_ATTACHMENTS: ")
        ]
        assert prefix_hits == []

    def test_ordering_start_lt_end_lt_attachments(self):
        lines = self._run_and_capture(populate=True)
        start_idx = lines.index("CHAT_MESSAGE_START")
        end_idx = lines.index("CHAT_MESSAGE_END")
        att_idx = next(
            i for i, ln in enumerate(lines)
            if ln.startswith("CHAT_ATTACHMENTS: ")
        )
        assert start_idx < end_idx < att_idx

    def test_attachments_json_schema(self):
        import json as _json
        lines = self._run_and_capture(populate=True)
        att = next(
            ln for ln in lines if ln.startswith("CHAT_ATTACHMENTS: ")
        )
        payload = _json.loads(att[len("CHAT_ATTACHMENTS: "):])
        assert set(payload.keys()) == {"files"}
        assert isinstance(payload["files"], list)
        assert len(payload["files"]) >= 1
        for entry in payload["files"]:
            assert set(entry.keys()) == {"path", "caption"}
            assert isinstance(entry["path"], str)
            assert entry["path"].startswith("/")  # absolute
            assert entry["caption"] in {"报销包", "报告", "明细"}

    def test_attachments_omits_zip_on_zip_failure(self):
        lines = self._run_and_capture(populate=True, zip_ok=False)
        import json as _json
        att = next(
            ln for ln in lines if ln.startswith("CHAT_ATTACHMENTS: ")
        )
        payload = _json.loads(att[len("CHAT_ATTACHMENTS: "):])
        captions = [f["caption"] for f in payload["files"]]
        assert "报销包" not in captions
        assert captions == ["报告", "明细"]

    def test_sentinel_strings_unique_in_postprocess_module(self):
        """Static guard: the literal sentinel strings should only appear
        in the sentinel constant definitions and writer calls, not sprinkled
        throughout the codebase. Catches accidental duplicates.
        """
        import os as _os
        path = _os.path.join(
            _os.path.dirname(__file__), "..", "scripts", "postprocess.py"
        )
        with open(path, encoding="utf-8") as f:
            source = f.read()
        # CHAT_MESSAGE_START appears in: constant def + writer call + docstring.
        # Allow <= 4 occurrences; flag blow-ups.
        assert source.count('"CHAT_MESSAGE_START"') <= 2, (
            "Unexpected duplication of CHAT_MESSAGE_START literal"
        )
        assert source.count('"CHAT_MESSAGE_END"') <= 2, (
            "Unexpected duplication of CHAT_MESSAGE_END literal"
        )
        assert source.count('"CHAT_ATTACHMENTS: "') <= 2, (
            "Unexpected duplication of CHAT_ATTACHMENTS prefix literal"
        )
```

- [ ] **Step 3: Run the new test class to verify it passes**

Run: `python3 -m pytest tests/test_agent_contract.py::TestChatSentinelContract -v`

Expected: **9 PASS** (all tests green because Task 1 + Task 2 already implemented the behavior they assert).

- [ ] **Step 4: Run the full agent contract suite**

Run: `python3 -m pytest tests/test_agent_contract.py -v`

Expected: **all tests PASS** (existing R8 `REMEDIATION:` tests + new R18 tests).

- [ ] **Step 5: Run the full repo test suite**

Run: `python3 -m pytest tests/ -q`

Expected: **all 200+ tests PASS**.

- [ ] **Step 6: Commit**

```bash
git add tests/test_agent_contract.py
git commit -m "test(agent-contract): R18 CHAT_MESSAGE + CHAT_ATTACHMENTS sentinels

Lock the sentinel contract at the Agent-facing boundary: anchor
format, ordering invariants, JSON schema, caption allowed values,
and zip-failure degradation. Mirrors R8 REMEDIATION: contract."
```

---

## Task 4: Document the Agent Playbook in `SKILL.md`

**Intent:** Tell Agents how to consume the sentinels. Channel-agnostic: name no specific skill; say "current channel's native message tool".

**Files:**
- Modify: `SKILL.md` — insert `## Presenting Results to the User` between `## Exit Codes` (line 511) and `## Handling Unknown Platforms` (line 526)

- [ ] **Step 1: Read the boundary context in SKILL.md**

Run: `python3 -c "
with open('SKILL.md') as f:
    lines = f.readlines()
for i, ln in enumerate(lines[508:530], start=509):
    print(f'{i:4d}: {ln}', end='')
"`

Expected: Lines around 511 show `## Exit Codes` and the block continues until line 526 where `## Handling Unknown Platforms` starts. Confirm the insertion point is the blank line immediately before `## Handling Unknown Platforms`.

- [ ] **Step 2: Insert the new section**

Find the `## Handling Unknown Platforms (extensibility)` heading in `SKILL.md`. Directly before it (with one blank line separator), insert:

```markdown
## Presenting Results to the User

Every successful (or partially successful) `print_openclaw_summary` run emits **two stdout sentinels** so the wrapping Agent can deliver the result to the user faithfully across any chat channel (飞书 / Slack / Discord / iMessage / …). The Skill itself is channel-agnostic and does **not** call any IM API directly.

### Sentinel 1 — `CHAT_MESSAGE_START` / `CHAT_MESSAGE_END`

Two bare anchor lines (no colon, no payload) wrap the full human-readable summary on every code path that reaches `print_openclaw_summary`, including the R16b empty-result branch.

```
CHAT_MESSAGE_START
📄 发票报销包 — 2025/04/01 → 2025/07/01

✅ 共 63 份凭证，合计 ¥48231.00
  ...(summary body)...

💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？
   直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。
CHAT_MESSAGE_END
```

### Sentinel 2 — `CHAT_ATTACHMENTS:`

A single-line JSON declaring deliverable files. Emitted only on the R16a non-empty path, after `CHAT_MESSAGE_END`. Skipped entirely on R16b.

```
CHAT_ATTACHMENTS: {"files":[{"path":"/abs/.../发票打包_xxx.zip","caption":"报销包"},{"path":"/abs/.../下载报告.md","caption":"报告"},{"path":"/abs/.../发票汇总.csv","caption":"明细"}]}
```

Schema:
- `files[]`: ordered list; the array order is the recommended upload order (报销包 → 报告 → 明细).
- `files[].path`: absolute path on the Skill host.
- `files[].caption`: short display label. Current values: `"报销包"` / `"报告"` / `"明细"`.

When `zip_output` fails (DEC-6), the zip entry is omitted but MD + CSV are still declared.

### Agent Playbook

After each Skill run:

1. Scan stdout for `CHAT_MESSAGE_START` and `CHAT_MESSAGE_END` lines.
2. Forward the content **between** those two lines to the user **verbatim** — do not add, remove, translate, summarize, or cherry-pick. Preserve all emoji, Chinese text, blank lines, and the tail invite.
3. If the stdout also contains `CHAT_ATTACHMENTS: {...}`, parse the JSON and, **in order**, upload each `file.path` as an attachment to the current chat using the channel's native message tool. Use `file.caption` as the attachment label/caption.
   - 飞书 channel: use the message tool's file-upload capability.
   - Slack / Discord / WhatsApp / iMessage / other IM: use the equivalent message tool in the Agent's tool set.
   - If the current channel's message tool does **not** support file attachments (e.g., plain SMS): skip the upload and include the absolute path in the forwarded text instead.
4. If a single upload fails, **do not abort**. Append one warning line to the same reply:
   ```
   ⚠️ {filename} 上传失败（{reason}），请从 {abs_path} 取
   ```
   Then continue with the next file.
5. If `CHAT_MESSAGE_START` / `CHAT_MESSAGE_END` are absent (early-error path), follow the `REMEDIATION:` stderr line as documented in § Exit Codes. Do not attempt attachments.

**Delivery order:** summary text first, attachments after.

**Redundancy is intentional:** the human-readable summary already shows absolute paths (e.g., `📦 报销包（提交这个）: /abs/...`). Those lines remain in the forwarded text so that if an upload fails (channel limit, network, unsupported), the user still has the path.

### Invariants

- Each sentinel appears at most **once** per Skill run.
- Strict ordering: `CHAT_MESSAGE_START` → `CHAT_MESSAGE_END` → `CHAT_ATTACHMENTS:` (the last two may be absent).
- `CHAT_ATTACHMENTS:` present ⇒ `CHAT_MESSAGE_START` / `END` both present.
- Regression-tested in `tests/test_agent_contract.py::TestChatSentinelContract` (R18).

```

- [ ] **Step 3: Verify SKILL.md still parses and reads cleanly**

Run: `head -60 SKILL.md | grep -n '^#'` then `grep -n '^## ' SKILL.md` to confirm the new heading appears between `## Exit Codes` and `## Handling Unknown Platforms`.

Expected output should include (numbers will shift after insertion):
```
... ## Exit Codes
... ## Presenting Results to the User
... ## Handling Unknown Platforms (extensibility)
```

- [ ] **Step 4: Commit**

```bash
git add SKILL.md
git commit -m "docs(SKILL): document CHAT_MESSAGE + CHAT_ATTACHMENTS playbook

New top-level section '## Presenting Results to the User' tells
Agents how to consume the two stdout sentinels emitted by
print_openclaw_summary: forward the message verbatim, upload
the declared attachments via the current channel's native message
tool, fall back to paths on upload failure. Channel-agnostic —
names no specific skill. Regression guarded by R18 contract tests."
```

---

## Task 5: Verify end-to-end + bump version label + CHANGELOG

**Intent:** Confirm the full pipeline still produces correct output, bump SKILL.md line 1 to v5.6, add a CHANGELOG entry. This is the final commit of the feature.

**Files:**
- Modify: `SKILL.md` line 1 (version label — only place it lives, per CLAUDE.md)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -q`

Expected: **all 200+ tests PASS**. Specifically verify:
- `tests/test_postprocess.py::TestPrintOpenClawSummary` — ~20 tests
- `tests/test_agent_contract.py::TestChatSentinelContract` — 9 tests
- No regressions elsewhere

- [ ] **Step 2: Smoke-test the sentinel rendering manually**

Run a tiny end-to-end check that exercises both paths:

```bash
python3 -c "
import sys, os, tempfile
sys.path.insert(0, 'scripts')
from postprocess import (
    print_openclaw_summary, build_aggregation, do_all_matching,
)

# R16b empty path
with tempfile.TemporaryDirectory() as td:
    matching = do_all_matching([])
    agg = build_aggregation(matching, [])
    sink = []
    print_openclaw_summary(
        aggregation=agg,
        output_dir=td,
        zip_path=os.path.join(td, 'p.zip'),
        csv_path=os.path.join(td, 'c.csv'),
        md_path=os.path.join(td, 'm.md'),
        log_path=os.path.join(td, 'run.log'),
        missing_status='stop',
        date_range=('2026/04/01', '2026/04/30'),
        writer=sink.append,
    )
    print('=== R16b empty path ===')
    for ln in sink:
        print(repr(ln))
    assert 'CHAT_MESSAGE_START' in sink
    assert 'CHAT_MESSAGE_END' in sink
    assert not any(ln.startswith('CHAT_ATTACHMENTS: ') for ln in sink)
    print('R16b OK — anchors present, no attachments')
"
```

Expected output: R16b sink contains exactly one `'CHAT_MESSAGE_START'` and one `'CHAT_MESSAGE_END'` line, no `CHAT_ATTACHMENTS:` line, plus the 3 "本次未下载到凭证" lines between the anchors.

- [ ] **Step 3: Bump version label in SKILL.md line 1**

Find the line at the very top of `SKILL.md` (likely `# Gmail Invoice Downloader (v5.5)` or similar, confirmed line 10 earlier). Edit only that label:

```
# Gmail Invoice Downloader (v5.6)
```

**Do not** add any other version labels anywhere else per CLAUDE.md ("Version label lives only in SKILL.md line 1").

- [ ] **Step 4: Add CHANGELOG.md entry**

Check whether `CHANGELOG.md` exists in the repo root with:

```bash
ls CHANGELOG.md 2>&1
```

If it exists, find the top of the file and insert a new v5.6 entry **above** the v5.5 entry. If it does not exist, skip to Step 5.

Example entry (prepend below the CHANGELOG header, above any existing v5.5 content):

```markdown
## v5.6 — 2026-05-02 — Agent-delivered chat attachments + verbatim message contract

### Added
- `CHAT_MESSAGE_START` / `CHAT_MESSAGE_END` stdout anchors wrap the full user-facing summary emitted by `print_openclaw_summary`, so the wrapping Agent can forward the message verbatim instead of selectively summarizing.
- `CHAT_ATTACHMENTS:` stdout sentinel (single-line JSON) declares deliverable files (zip + MD + CSV) for the Agent to upload as chat attachments using the current channel's native message tool. Channel-agnostic — works on 飞书, Slack, Discord, iMessage, etc.
- New `## Presenting Results to the User` section in `SKILL.md` documents the contract + Agent Playbook.
- R18 contract tests in `tests/test_agent_contract.py::TestChatSentinelContract` lock anchor format, ordering, JSON schema, and zip-failure degradation.
- 6 new tests in `tests/test_postprocess.py::TestPrintOpenClawSummary` cover behavior on R16a / R16b / zip-failure paths.

### Behavior
- On R16a non-empty path: anchors + attachments JSON both emitted.
- On R16b empty-result path: anchors only; attachments JSON omitted.
- When zip_output fails (DEC-6): attachments JSON omits the zip entry but still declares MD + CSV.
- Sentinels appear in both stdout and `run.log` (via existing `writer=say` double-write) — acceptable, doesn't hurt debugging.

### No behavior change
- `missing.json` schema, `REMEDIATION:` stderr lines, exit codes, `print_openclaw_summary` signature — all unchanged.
```

- [ ] **Step 5: Run full suite one last time**

Run: `python3 -m pytest tests/ -q`

Expected: **all tests PASS** — no version-label based tests should break (version label isn't part of any test assertion, per repo convention).

- [ ] **Step 6: Commit**

Choose the right scope depending on whether CHANGELOG.md exists:

If CHANGELOG.md exists:
```bash
git add SKILL.md CHANGELOG.md
git commit -m "chore(v5.6): bump version + changelog for CHAT_MESSAGE + CHAT_ATTACHMENTS

v5.6 adds two stdout sentinels to print_openclaw_summary so the
wrapping Agent can forward the user-facing summary verbatim and
upload deliverables (zip + MD + CSV) as attachments in any IM
channel. Channel-agnostic contract — no IM SDK dependency in Skill."
```

If CHANGELOG.md does not exist:
```bash
git add SKILL.md
git commit -m "chore(v5.6): bump version label for CHAT_* sentinel release

v5.6 adds two stdout sentinels (CHAT_MESSAGE_START/END +
CHAT_ATTACHMENTS:) so the wrapping Agent can forward the summary
verbatim and upload deliverables as chat attachments. Channel-agnostic."
```

---

## Self-Review Checklist (already run)

1. **Spec coverage** — every spec requirement maps to a task:
   - `CHAT_MESSAGE_START/END` anchors emitted on every code path → Task 1 Steps 4–6
   - R16b attachments skipped → Task 2 Step 1 `test_attachments_omitted_on_empty_template` + Task 2 Step 3 (attachments emitted only on R16a path, after R16b has already `return`ed)
   - zip failure degrades → Task 2 Step 3 conditional `if zip_path is not None`
   - Caption allowed values → Task 1 Step 3 `_ATTACHMENT_CAPTIONS` dict + Task 3 Step 2 `test_attachments_json_schema` assertion
   - Strict ordering → Task 3 Step 2 `test_ordering_start_lt_end_lt_attachments`
   - Agent Playbook text → Task 4 Step 2
   - Regression guard for 195 existing tests → Task 2 Step 6 + Task 5 Step 1

2. **Placeholder scan** — no "TBD", "TODO", "implement later". All code blocks complete. All test assertions concrete.

3. **Type consistency** — `CHAT_MESSAGE_START_SENTINEL` / `CHAT_MESSAGE_END_SENTINEL` / `CHAT_ATTACHMENTS_PREFIX` / `_ATTACHMENT_CAPTIONS` constants defined once in Task 1 Step 3 and referenced consistently in Task 2 Step 3. Caption values `"报销包"` / `"报告"` / `"明细"` match across spec, implementation, Task 2 test `test_attachments_emitted_normal_three_deliverables`, and Task 3 contract test `test_attachments_json_schema`.
