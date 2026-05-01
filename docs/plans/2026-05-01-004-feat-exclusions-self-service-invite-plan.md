---
title: "feat: Add self-service exclusions invite to OpenClaw summary"
type: feat
status: active
date: 2026-05-01
origin: docs/brainstorms/2026-05-01-exclusions-self-service-invite-requirements.md
---

# feat: Add self-service exclusions invite to OpenClaw summary

## Overview

Append a 3-line, conversational invite to the end of `print_openclaw_summary`'s non-empty (R16a) template, prompting users to reply with senders/subjects that aren't reimbursable so a host Agent can append them to `learned_exclusions.json`. Strictly additive change: one-pass edit inside `scripts/postprocess.py::print_openclaw_summary`, a docstring cap bump (`≤20` → `≤24`), and three new test cases in `TestPrintOpenClawSummary`.

## Problem Frame

`learned_exclusions.json` is the single source of truth for Gmail `-from:` / `-subject:` noise filters (40+ rules today) and is entirely user-maintained. The current OpenClaw summary never surfaces the operational entry point for adding rules — the only indirect mention is R16b's "过滤过严" hint, which points the **opposite** direction (remove rules). Users who notice non-reimbursable PDFs in the deliverable (Apple subs, SaaS, personal statements) have no obvious reply path, so the noise either gets manually edited in the JSON file or, more commonly, appears again next run. The invite converts this dead-end into a chat-native handoff. See origin for full product framing.

## Requirements Trace

- **R1** — Append 3 `writer()` calls immediately after the existing 交付物 lines (after `scripts/postprocess.py:944`): `writer("")`, then the emoji line, then the indented follow-up line. Exact strings fixed.
- **R2** — The invite block must not appear in the R16b early-return path (voucher_count == 0 AND no unmatched/UNPARSED/failed). Satisfied structurally by placing the appended code after R16b's `return`.
- **R3** — Unconditional within R16a. No reads of `skipped[]`, `learned_exclusions.json`, or any frequency signal.
- **R4** — `print_openclaw_summary` signature unchanged. Exactly 3 new `writer()` calls + a docstring edit. No new imports, no new helpers.
- **R5** — Update docstring cap from `≤20-line` to `≤24-line` on `scripts/postprocess.py:840`.
- **R6** — 3 new tests under `tests/test_postprocess.py::TestPrintOpenClawSummary`:
  (a) R16a substring + ordering integrity; (b) R16b absence; (c) placement after 交付物.

## Scope Boundaries

**Data & aggregation**
- No `skipped[]` aggregation; no cross-run counters (see origin DEC-2).

**File & contract surfaces**
- No reads/writes of `learned_exclusions.json` from this feature's code path (the user Agent owns the write after the reply).
- No changes to `下载报告.md`, `发票汇总.csv`, or `missing.json`.

**Template structure**
- R16a's existing 11-step structure/ordering is untouched — purely additive at the tail.
- R16b untouched.

**Interaction / L10n**
- No interactive prompts (would break exit-code + REMEDIATION contract).
- Chinese-only text.

## Context & Research

### Relevant code and patterns

- `scripts/postprocess.py:828-944` — `print_openclaw_summary`, R16a/R16b branches. Append point is the line immediately after line 944 (`writer(f"  明细: {abs_csv}   |   报告: {abs_md}")`), which is the function's current terminal line.
- `scripts/postprocess.py:864-874` — R16b early-return. Because it `return`s before reaching line 944, any code appended after line 944 automatically satisfies R2 without branch logic.
- `scripts/postprocess.py:840` — docstring `"Render a ≤20-line summary to stdout..."`. Target of R5.
- `tests/test_postprocess.py:1668` — `TestPrintOpenClawSummary` (CamelCase `OpenClaw`). Uses `_capture()` helper (`:1674-1680`) that injects `writer=lambda s: sink.append(s)` and returns the full line list.
- Existing assertion style: `text = "\n".join(lines); assert "…" in text` (e.g. `:1712-1715`). The new tests should mirror this style.
- `learned_exclusions.json` — format is `{exclusions: [{rule, reason, confirmed}]}`, verified via direct read. Agent append contract (not exercised by this plan) uses `confirmed: "YYYY-MM-DD"`.

### Institutional learnings

- CLAUDE.md "Editing norms": silent fallback is a recurring anti-pattern; this feature avoids it (invite is explicit, not inferred behavior).
- CLAUDE.md "Agent-facing runtime contract": the three stable contracts are exit codes, REMEDIATION, and `missing.json` schema. This plan does not touch any of them — stdout additions below the deliverables block are noise-safe.
- Review finding (deferred from brainstorm): "Agent writes `learned_exclusions.json` via chat reply" is a **new implicit contract** — CLAUDE.md does not currently enumerate config-mutation-via-chat as a supported Agent capability. The plan captures this gap in Risks rather than silently shipping; remediation can happen in SKILL.md as a follow-up.

### External references

None used. Feasibility was verified in-repo (append point, function signature stability, test infrastructure existence, absence of line-count assertions in `tests/test_agent_contract.py`).

## Key Technical Decisions

- **Append after line 944 rather than refactoring the function**: exploits the fact that R16b `return`s early, so "append at function tail" == "append inside R16a only". Avoids introducing a conditional, and preserves R4's "signature unchanged" claim.
- **Hard-coded 3-space indent on line 2 of the invite** (see origin R1 and DEC note there): visually aligns with 💡 (2 columns wide + 1 space = 3). Not a parameter, not a constant — just a string literal, matching every other line of this template (no existing line indents from a const).
- **Docstring cap bumped to `≤24` (numeric) rather than made qualitative**: keeps the line count a reviewable regression tripwire. See origin R5.
- **Test ordering assertion uses `"\n".join(lines).index(...)` comparisons**: matches the existing `_capture()` + `"\n".join(lines)` + `in text` pattern at `tests/test_postprocess.py:1712`. Resolves the only deferred planning question from the origin doc.
- **No guardrail text in the invite about "don't blocklist 百望云"** (feasibility reviewer #5): deliberately kept out. The invite is meant to be short, and the real mitigation is the Agent's judgment during the follow-up chat turn. Captured as a Risk instead.

## Open Questions

### Resolved During Planning

- **Test assertion style for placement ordering** → Use joined `text = "\n".join(lines)` and `text.index(...)` comparisons, matching `tests/test_postprocess.py:1712-1715` pattern.
- **Docstring cap shape** → Numeric `≤24`, keep as a tripwire (origin R5 committed this during document-review).

### Deferred to Implementation

- None. The change is mechanical; there are no runtime unknowns to discover.

### Deferred to Follow-up Work (out of scope here)

- Document the "Agent mutates `learned_exclusions.json` in response to chat reply" contract in `SKILL.md`'s Agent Loop Playbook section (surfaced by both feasibility and product-lens reviewers during brainstorm; origin's Risks section acknowledges it). A small SKILL.md edit in a separate PR — not bundled here because this plan must stay strictly additive to `print_openclaw_summary`.

## Implementation Units

- [ ] **Unit 1: Append invite block to R16a and bump docstring cap**

**Goal:** Add the 3-line invite to the end of `print_openclaw_summary` and update its docstring's line-count cap.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** None.

**Files:**
- Modify: `scripts/postprocess.py` (function `print_openclaw_summary` — append at current function tail, line 945; docstring at line 840)

**Approach:**
- Append 3 new `writer()` calls immediately after the existing `writer(f"  明细: {abs_csv}   |   报告: {abs_md}")` (currently the function's terminal line):
  1. `writer("")` — blank separator.
  2. `writer("💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？")`
  3. `writer("   直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。")` (3 half-width spaces to visually align with the 💡 emoji on line 2).
- The R16b branch at `scripts/postprocess.py:864-874` already calls `return`, so the appended block is only reached on the R16a path. No conditional needed.
- Change the docstring substring `"Render a ≤20-line summary"` to `"Render a ≤24-line summary"` at line 840.

**Execution note:** Test-first. Write the 3 new test cases in Unit 2 before touching `scripts/postprocess.py`, and confirm they fail with the expected "substring missing" errors, then make them pass by appending the writer calls. This is the testable invariant in the plan, and the existing `TestPrintOpenClawSummary` infrastructure (`_capture()` helper at `tests/test_postprocess.py:1674-1680`) already supports this loop at zero cost.

**Patterns to follow:**
- Existing writer-call style in `print_openclaw_summary` (plain literals, no f-strings when no interpolation needed, no common indent constant).
- Do NOT introduce an intermediate list and a loop — existing code uses straight-line `writer(...)` calls per line, and the invite should match.

**Test scenarios:** *(see Unit 2 for authoritative wording — Unit 1's verification is that Unit 2's tests pass, plus the full existing suite.)*

**Verification:**
- The 3 new tests from Unit 2 pass.
- All existing `TestPrintOpenClawSummary` tests still pass (e.g. `test_non_empty_template_shows_vouchers_and_total`, `test_stop_status_says_can_submit`, `test_run_supplemental_includes_quoted_command`, `test_ask_user_points_at_md`, `test_empty_template_short`, `test_unknown_missing_status_raises`).
- Full `pytest tests/` run stays green — no regression in `tests/test_agent_contract.py` (no current assertion pins terminal stdout line or exact line count).

---

- [ ] **Unit 2: Add test coverage for invite presence, absence, and placement**

**Goal:** Pin the new behavior with 3 focused test cases in `TestPrintOpenClawSummary`.

**Requirements:** R6

**Dependencies:** None. Under Unit 1's test-first execution note, Unit 2's tests are **authored first** (they should fail against the pre-Unit-1 code), then Unit 1's `writer()` calls are added until the tests turn green. Both units land in a single commit — the separate-unit structure is for plan clarity, not for staged landing.

**Files:**
- Modify: `tests/test_postprocess.py` (append 3 new `test_*` methods inside the existing `TestPrintOpenClawSummary` class, below the existing tests)

**Approach:**
- Reuse the existing `_capture()` helper and `_populated_agg()` / `_default_paths()` fixtures. No new imports, no new fixtures.
- Invite-presence test uses `missing_status="stop"` (cheapest path) with `_populated_agg()` to guarantee R16a.
- Invite-absence test reuses the empty-aggregation construction pattern from the existing `test_empty_template_short` (at `tests/test_postprocess.py:1764`): `do_all_matching([])` + `build_aggregation(matching, [])`. Do NOT add a module-level fixture.
- Ordering test joins `sink` via `"\n".join(lines)` and uses `text.index("💡")`, `text.index("明细:")`, and `text.index("下次自动排除")` directly — matching the style at `tests/test_postprocess.py:1712-1715`.

**Patterns to follow:**
- `TestPrintOpenClawSummary._capture()` — `sink: List[str]` + `lambda s: sink.append(s)` writer, per `tests/test_postprocess.py:1674-1680`.
- Assertion style: `text = "\n".join(lines); assert "…" in text` (no regex, no pytest parametrize for these 3 — they're too divergent to share a body).

**Test scenarios:** exactly **3 new test methods** inside `TestPrintOpenClawSummary`:

1. **`test_invite_present_in_non_empty_template`** — combines happy-path presence with line-ordering integrity.
   - *Happy path* — populated aggregation + `missing_status="stop"` → joined text contains both `"💡 发现不该报销的"` and `"learned_exclusions.json，下次自动排除"`.
   - *Ordering integrity* — same test asserts `text.index("💡 发现不该报销的") < text.index("下次自动排除")`, guarding against a future edit that prints only line 1 or transposes the two.
2. **`test_invite_absent_in_empty_template`** — *edge case (R16b absence)* — aggregation with `voucher_count == 0`, no rows, no unmatched → joined text does NOT contain `"💡 发现不该报销的"`.
3. **`test_invite_appears_after_deliverables`** — *placement integrity* — populated aggregation with `missing_status="stop"` → `text.index("明细:") < text.index("💡 发现不该报销的")`. Covers R6(c): guards against a future refactor that moves the invite above the 📦/明细 block.

**Verification:**
- All 3 new tests pass against Unit 1's implementation.
- All 3 new tests **fail** against the pre-Unit-1 code (confirmed manually before committing by running them first under test-first posture).
- No change to the file count in `tests/` — edits stay inside `test_postprocess.py`.

## System-Wide Impact

- **Interaction graph:** None beyond stdout. `print_openclaw_summary` is called once from `scripts/download-invoices.py` at run tail; no middleware, no callbacks, no observers.
- **Error propagation:** No new error paths. `writer()` is a pure callable (`print` or lambda-into-list) and both existing call sites already tolerate arbitrary string arguments.
- **State lifecycle risks:** None — no files written, no caches touched, no state mutated.
- **API surface parity:** None. The CLI flag surface, exit codes, and `missing.json` schema are unchanged. Stdout content is not a stable API (CLAUDE.md "Agent-facing runtime contract" does not enumerate stdout tail text as a contract).
- **Integration coverage:** Covered by the 3 new unit tests against the real function; no additional integration test needed.
- **Unchanged invariants:**
  - `missing.json` schema v1.0.
  - Exit-code and REMEDIATION contracts.
  - R16a's 11-step ordering (the invite is step 12-13, appended).
  - `print_openclaw_summary` function signature (positional and kwargs unchanged).
  - R16b's 3-line empty template.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| **User replies "屏蔽 百望云"** and the Agent appends `-from:baiwang` to `learned_exclusions.json`, silently blocking a legitimate invoice platform going forward. | Out of scope for code in this plan. The invite's own text avoids naming specific blocklist patterns, keeping the decision firmly in the user's chat reply and the Agent's judgment layer. Recommend a follow-up SKILL.md addition in the Agent Loop Playbook that reminds agents not to blocklist any `from:` pattern that matches a known platform (百望云/诺诺/fapiao.com/xforceplus/云票/百旺金穗云/金财数科/克如云/12306). Tracked in the "Deferred to Follow-up Work" section. |
| **OpenClaw (or other hosts) truncates stdout tail** and users never see the invite. | Accepted. Mitigation: the change is cheap and reversible. If empirical checks show truncation, move the invite higher up the template in a follow-up. Do not pre-optimize. |
| **Agent paraphrases stdout into its own chat wrap-up** and drops the invite. | Accepted. This is a host-behavior question that can only be validated by observation, not planning. If it happens, the mitigation is to include a short "Agent loop tip: preserve lines starting with 💡" note in SKILL.md — deferred. |
| **Docstring cap `≤24` gets busted by a future change** (e.g., a new per-category row or extra footnote pushes the total past 24). | The numeric cap is the mitigation — it exists precisely so a reviewer notices. If legitimately needed, the cap gets revised in the same PR. Not a risk to this plan itself. |
| **New implicit Agent contract** — "Agent writes `learned_exclusions.json` in response to stdout-visible chat invite" — is not documented anywhere in CLAUDE.md's three stable contracts. | Accepted for this plan. The invite is *passive* (does not require Agent action for the summary to be valid) and does not break any existing contract. Remediation in SKILL.md is tracked under "Deferred to Follow-up Work". |

## Documentation / Operational Notes

- No user-facing doc changes in this PR.
- Follow-up PR should add a sentence to `SKILL.md` Agent Loop Playbook covering the "user-reply → Agent appends to `learned_exclusions.json`" pattern and the "don't blocklist known platforms" guardrail. Not bundled here to keep the PR strictly additive within `print_openclaw_summary`.
- No rollout, migration, or monitoring concerns. The change is observable at the next run.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-05-01-exclusions-self-service-invite-requirements.md](../brainstorms/2026-05-01-exclusions-self-service-invite-requirements.md)
- Related code: `scripts/postprocess.py::print_openclaw_summary`, `tests/test_postprocess.py::TestPrintOpenClawSummary`, `learned_exclusions.json`
- Related contracts: `CLAUDE.md` "Agent-facing runtime contract" section (the three stable contracts this plan does not touch)
- No related PRs/issues — greenfield feature in the current development cycle.
