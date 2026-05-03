# v5.9 — Agent Playbook: native-attachment contract

**Date**: 2026-05-03
**Status**: requirements / design approved in brainstorming; next step is plan
**Scope**: documentation-only change to `SKILL.md`

## Motivation

2026 Q1 usage surfaced a concrete regression on the 飞书 channel: when
delivering the three invoice deliverables (`发票汇总.csv`, `下载报告.md`,
`发票打包_*.zip`) to the user, the OpenClaw Agent posted a line like

```
📎 /home/ubuntu/.openclaw/media/outbound/发票汇总---<uuid>.csv
```

inside the reply text instead of attaching the file as a native message
(clickable file bubble with filename, size, and a download button). The user
received a path string they could not click, containing the OpenClaw runtime's
internal `media/outbound/` staging UUID — indistinguishable from a bug in the
eyes of the user.

Earlier in the same session, after the user explicitly said *"直接 message
发附件即可"*, the Agent correctly sent all three deliverables as native
attachments, proving the channel supports it and the Agent has the capability.
The failure was purely a wrong choice of delivery mechanism on the first try.

## Root cause

`SKILL.md` §Presenting Results → §Agent Playbook step 3 enumerated channels
individually:

> 飞书 channel: use the message tool's file-upload capability.
> Slack / Discord / WhatsApp / iMessage / other IM: use the equivalent message
> tool in the Agent's tool set.

That prose gave the Agent room to reason about which tool to pick per channel.
When one of the candidate paths was "dump the staging path as text", nothing in
the contract ruled it out. `~/.openclaw/media/outbound/` is not documented
anywhere in OpenClaw's public skill docs (confirmed by a full grep of the
`openclaw` skill references), so the Agent treats it as an ambiguous,
referenceable path.

Research finding: OpenClaw already provides a **unified, cross-channel native
attachment primitive**: the `message` tool with a `media` field (CLI mirror:
`openclaw message send --media <path>`). The Gateway handles per-channel
routing (飞书 / Lark / Slack / Telegram / Discord / WhatsApp / iMessage via
bridge / 企业微信 / WebUI). The Agent never needs to reason about channel APIs.

## Design decision

Rewrite §Agent Playbook step 3 (and align the wording in step 4) to point at
the unified primitive and forbid textual path leakage, with an explicit
counter-example matching the observed failure.

**Decision matrix recap** (discussed during brainstorming):

| Option | Chosen? | Reason |
|---|---|---|
| A. SKILL.md prose rewrite | ✅ yes | The constraint is already expressible in prose, and OpenClaw's contract is genuinely a single primitive — the Skill only needs to stop hedging. |
| B. Structured `CHAT_ATTACHMENTS` schema field (e.g., `"delivery":"native_message_media"`) | ❌ no | Adds machinery to express a rule a sentence can express. Can be revisited if option A proves insufficient across other channels. |
| C. Memory / CLAUDE.md only | ❌ no | Scoped to one user, one agent — does not protect other users of this Skill. |

## Scope

### In scope (files changed)

`SKILL.md` only. Three edits:

1. **Version bump**, line 10: `# Gmail Invoice Downloader (v5.8)` →
   `(v5.9)`.
2. **Step 3 + step 4 replacement** in §Presenting Results → §Agent Playbook.
3. **New Lessons Learned entry** 🟢 v5.9, appended after the v5.8 entry.

### Out of scope

- No Python changes (`scripts/postprocess.py`, `scripts/download-invoices.py`,
  `scripts/core/`, etc.).
- No test changes. The `CHAT_ATTACHMENTS:` stdout contract and
  `test_agent_contract.py::TestChatSentinelContract` (R18) stay as-is; this is
  a prose change to the Agent-facing guidance, not to the machine contract.
- No `CHAT_ATTACHMENTS` schema changes. Path + caption remain sufficient.
- No `CHANGELOG.md` changes here — handled by the ship flow when v5.9 is
  released.
- No `references/` changes.
- No change to OpenClaw-side Agent configuration (not our repo).

## Detailed changes

### Change 1 — version bump (SKILL.md line 10)

```
-# Gmail Invoice Downloader (v5.8)
+# Gmail Invoice Downloader (v5.9)
```

Per the CLAUDE.md convention, the version label lives only on this line.
Historical version labels inside §Lessons Learned entries and things like
`"date_only (v5.2 fallback)"` are introduced-at lineage and stay untouched.

### Change 2 — rewrite §Agent Playbook step 3 (and align step 4)

Current text (around SKILL.md line 604):

```markdown
3. If the stdout also contains `CHAT_ATTACHMENTS: {...}`, parse the JSON and, **in order**, upload each `file.path` as an attachment to the current chat using the channel's native message tool. Use `file.caption` as the attachment label/caption.
   - 飞书 channel: use the message tool's file-upload capability.
   - Slack / Discord / WhatsApp / iMessage / other IM: use the equivalent message tool in the Agent's tool set.
   - If the current channel's message tool does **not** support file attachments (e.g., plain SMS): skip the upload and include the absolute path in the forwarded text instead.
4. If a single upload fails, **do not abort**. Append one warning line to the same reply:
   ```
   ⚠️ {filename} 上传失败（{reason}），请从 {abs_path} 取
   ```
   Then continue with the next file.
```

New text:

```markdown
3. If the stdout also contains `CHAT_ATTACHMENTS: {...}`, parse the JSON and, **in order**, send each file as a **native message attachment** via OpenClaw's unified message primitive (`openclaw message send --media <path>` or the equivalent `message` tool call with a `media` field). Pass `file.path` as the media and `file.caption` as the caption. OpenClaw's Gateway routes to the destination channel (飞书 / Lark / Slack / Telegram / Discord / WhatsApp / iMessage-bridge / 企业微信 / WebUI) — the Agent MUST NOT choose channel-specific APIs or reason about per-channel attachment semantics.

   **Do NOT** paste the file path into the reply text. In particular, do NOT emit lines like:
   ```
   📎 /home/ubuntu/.openclaw/media/outbound/发票汇总---<uuid>.csv
   ```
   Paths in `CHAT_ATTACHMENTS` — and any `~/.openclaw/media/outbound/...` staging paths surfaced by the runtime — are inputs to the media primitive, not content for the user. The user must receive a clickable file bubble, not a path string.

   The only exception is channels that genuinely cannot carry attachments (plain SMS). In that single case, skip the media call and append one line per file to the forwarded summary:
   ```
   ⚠️ 本 channel 不支持附件，请从 {abs_path} 取 {caption}
   ```
4. If a single `message send --media` call fails, **do not abort**. Append one warning line to the same reply:
   ```
   ⚠️ {filename} 发送失败（{reason}），请从 {abs_path} 取
   ```
   Then continue with the next file.
```

Rationale per sub-change:

- **Single primitive, not per-channel enumeration.** Enumerating channels
  invites the Agent to reason about which tool to pick; the unified primitive
  closes that door.
- **Embed the exact failure form as a counter-example.** The string `📎 /home/ubuntu/.openclaw/media/outbound/发票汇总---<uuid>.csv` matches the actual observed failure so future Agents can pattern-match it as "this is the thing I must not do".
- **Explain what `media/outbound/` is.** Because OpenClaw does not document
  this path, the Skill states its role — a staging input to the media
  primitive, not user-facing content.
- **Preserve SMS degradation.** The only channel that truly cannot carry
  attachments still has a deterministic fallback — a single-line warning in
  the summary. The warning format is intentionally aligned with step 4 so
  both failure modes read the same way visually.
- **Drop the word "upload" from the Agent-facing contract.** "Upload" led the
  Agent toward generic file-upload tools instead of the named primitive. Now
  step 3 says "send as a native message attachment via the primitive" and
  step 4 says "if `message send --media` fails" — one noun, one verb.

### Change 3 — new Lessons Learned entry (🟢 v5.9)

Appended at the end of §Lessons Learned, after the existing v5.8 entry:

```markdown
### 🟢 v5.9 — Agent 把 staging 路径当文本发给用户的回归

**问题**：2026 Q1 使用中发现 OpenClaw Agent 在飞书 channel 给用户回复时，把交付物发成了一行形如 `📎 /home/ubuntu/.openclaw/media/outbound/发票汇总---<uuid>.csv` 的文本，而不是原生 message 附件（可点击的文件 bubble）。用户收到的是一段无法直接下载的路径字符串，且路径里还带 OpenClaw 运行时的 staging UUID —— 观感像 bug（实际上是 Agent 侧选错了姿势，Skill 这边只在文档层加防护栏）。

**根因**：§Agent Playbook 步骤 3 的原文按 channel 枚举（"飞书用 message file-upload / Slack/Discord/... 用对应工具"），给 Agent 留了自行推理 channel 细节的空间。当 Agent 在"挑一个合适的 channel API"时偏向了内部 staging 目录引用，而不是调 OpenClaw 的统一原语 `message send --media`。`~/.openclaw/media/outbound/` 在 OpenClaw 官方文档里没有公开定义，Agent 会把它当成"可以文本引用"的 URL 形态。

**v5.9 解决**：§Agent Playbook 步骤 3 整段重写 —— 明确"永远调用 OpenClaw 的统一 `message send --media` 原语，Gateway 负责路由到 channel；Agent 不要对 channel 细节做推理"。加反面示例（`📎 /home/ubuntu/.openclaw/media/outbound/...` 原型）。显式说明 `CHAT_ATTACHMENTS` 里的 path 是原语的输入、不是用户看到的内容。唯一例外是 SMS（无 MMS）走纯文本降级。步骤 4 的"{filename} 上传失败"措辞同步改成"{filename} 发送失败"，与步骤 3 的新术语一致。

**不改的**：`CHAT_ATTACHMENTS:` JSON schema 不变（path + caption 已经够用 —— 把约束表达在 Agent Playbook 的 prose 里更轻，比再加 schema 字段更合适）；无 Python 改动；`test_agent_contract.py::TestChatSentinelContract` 的契约文本不变。

**教训**：跨 channel 的交付契约，**枚举 channel = 给 Agent 留推理空间**。如果运行时已经提供统一原语（OpenClaw 的 `message send --media`），文档就应该坚定指向它、禁止 Agent 自行选择路径。具体反面示例（整段 `📎 /path/...` 原型）比抽象规则更能防复现。
```

## Acceptance criteria

1. `git diff SKILL.md` shows exactly three logical changes: version bump on
   line 10, step-3-and-step-4 rewrite in §Presenting Results → §Agent Playbook,
   and a new 🟢 v5.9 entry appended to §Lessons Learned. No unrelated edits.
2. `python3 -m pytest tests/ -q` continues to pass. (SKILL.md is prose;
   `test_agent_contract.py` does not assert on `SKILL.md` content, only on
   stdout contract — so a prose change should not regress tests.)
3. Document self-consistency:
   - Step 3's new wording and step 4's "{filename} 发送失败" share the same
     vocabulary — no lingering "upload" in the Agent-facing sentences.
   - The counter-example string in step 3 and in the v5.9 Lessons Learned entry are identical: `📎 /home/ubuntu/.openclaw/media/outbound/发票汇总---<uuid>.csv`.
   - The v5.9 entry's references to "§Agent Playbook 步骤 3 整段重写" point at
     the actual rewritten section.
4. CLAUDE.md guardrails respected:
   - Version label appears only on line 10 — no `(v5.9)` suffixes added to
     other headings, module docstrings, or runtime banners.
   - No hardcoded proxy URL or API key added anywhere.
   - `scripts/core/` untouched.
5. No Python or test changes.

## Risks and explicit non-decisions

- **Risk: Agent on a non-飞书 channel still leaks staging paths.** Likelihood
  low because OpenClaw's routing is a single primitive across channels —
  once the Agent calls `message send --media`, the Gateway handles the rest.
  If this risk materializes, option B (structured `CHAT_ATTACHMENTS` field)
  becomes the next escalation.
- **Risk: OpenClaw someday changes the primitive name.** Then the SKILL.md
  reference to `message send --media` would drift. Accepted — this is a
  standard doc-coupling risk, and the CLI form is a public command today.
- **Non-decision**: we are not amending `tests/test_agent_contract.py` to
  lint SKILL.md wording. The Skill's machine contract is the stdout sentinel
  format; the prose guidance is for the Agent's reasoning layer and is
  validated by user-visible failure modes, not test assertions.

## Follow-up (not in this scope)

- If another channel (Telegram / Discord / WhatsApp / 企业微信 bot) exhibits
  the same staging-path-leak behavior, re-open this design and pursue
  option B (add a structured `delivery` field to `CHAT_ATTACHMENTS` with a
  regression test).
- If OpenClaw publishes an official definition for `~/.openclaw/media/outbound/`, update the SKILL.md reference accordingly.
