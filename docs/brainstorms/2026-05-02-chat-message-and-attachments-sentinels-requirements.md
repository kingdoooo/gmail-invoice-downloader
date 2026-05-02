# Chat Message + Attachments Sentinels — Requirements

**Date**: 2026-05-02
**Status**: draft — awaiting user review before planning
**Origin**: Skill 运行结束后的交付物（zip + MD + CSV）当前仅以绝对路径形式出现在 stdout 摘要里，Agent 没有可靠方式把它们作为附件发到用户所在 chat；同时 stdout 末尾的 "💡 发现不该报销的…" 提示也常被 Agent 当作装饰文字丢弃，用户看不到。

## Problem

Skill → Agent → 用户这条链上缺两块契约：

1. **给用户看的文字原文没有边界**。Agent 混着机器信号（`REMEDIATION:`、`missing.json` 路径）和人读中文摘要一起读，只能靠启发式挑"重要"的转述，结尾的 "💡..." 提示常被砍掉。
2. **交付物是文件、不是路径**。用户在飞书 / Slack / Discord 等 IM 里期望看到真正的附件，现在只看到一串本地绝对路径。

这两个是同一个底层问题（Skill 没办法告诉 Agent "这一段/这些文件必须原封不动送达用户"）的两个症状，一起解决。

## Goals

- Skill stdout 明确标出"给用户的文字原文"边界，让 Agent **原文转发**，不做选择性摘抄
- Skill stdout 明确声明"这些文件应作为附件出现在当前 chat 中"，让 Agent 调用当前 channel 的原生消息/文件工具上传
- 契约 **channel-agnostic** — 不依赖任何特定 skill 或 IM SDK
- 契约面尽量小（两条 sentinel）、可测（严格前缀匹配 + JSON schema）、跟现有 `REMEDIATION:` / `missing.json` 的 Agent-facing 风格一致

## Non-Goals

- Skill **不** 直接调任何 IM API（不引 SDK、不读 channel 凭据）
- Skill **不** 做上传重试、失败恢复、大小预检 — 那是 Agent Playbook 的职责
- **不** 新增 sidecar 文件（如 `attachments.json`）— 沿用现有 stdout-is-contract 风格
- **不** 改 sentinel payload 以携带 MIME / size / checksum — Agent 工具自己能拿到
- **不** 改变 `missing.json` / `REMEDIATION:` / exit code 现有语义
- **不** 改 `print_openclaw_summary` 签名或参数

## Design

### Contract — two independent sentinels

Skill stdout 在成功路径末尾结构如下：

```
... 各 step 日志 ...

CHAT_MESSAGE_START
📄 发票报销包 — 2025/04/01 → 2025/07/01

✅ 共 63 份凭证，合计 ¥48231.00

  • 🏨 酒店 12 份    ¥28450.00
  ... (per-category) ...

⚠️ 2 张酒店发票无对应水单

👉 下一步：可以提交报销 — 打开上面 zip

📦 报销包（提交这个）: /abs/.../发票打包_xxx.zip
  明细: /abs/.../发票汇总.csv   |   报告: /abs/.../下载报告.md

💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？
   直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。
CHAT_MESSAGE_END
CHAT_ATTACHMENTS: {"files":[{"path":"/abs/.../发票打包_xxx.zip","caption":"报销包"},{"path":"/abs/.../下载报告.md","caption":"报告"},{"path":"/abs/.../发票汇总.csv","caption":"明细"}]}
```

#### `CHAT_MESSAGE_START` / `CHAT_MESSAGE_END`

- **格式**：两行无 payload 的纯锚点（字面量，无冒号、无尾随空格）
- **包围**：`print_openclaw_summary` 产出的全部人读文字（含 emoji、中文、换行、末尾提示）
- **触发**：`print_openclaw_summary` 被调到就输出（包括 R16b 空结果分支）
- **每次运行最多出现一对**
- **不变量**：`CHAT_MESSAGE_START` 必须在 `CHAT_MESSAGE_END` 之前；两者之间不穿插任何其它 sentinel

#### `CHAT_ATTACHMENTS:`

- **格式**：单行 `CHAT_ATTACHMENTS: ` 前缀 + 严格 JSON（UTF-8，无多行，无尾随空格）
- **Schema**：
  ```json
  {
    "files": [
      {"path": "<absolute path>", "caption": "<display label>"}
    ]
  }
  ```
  - `files`: array；顺序即推荐上传顺序
  - `files[].path`: 绝对路径（`os.path.abspath` 过）
  - `files[].caption`: 短中文标签，当前三种取值：`"报销包"` / `"报告"` / `"明细"`
- **触发**：仅当**至少一个**交付物文件实际生成；zip 失败时只含 MD + CSV；R16b 空结果**不输出**
- **位置**：必须在 `CHAT_MESSAGE_END` 之后、`print_openclaw_summary` 退出之前
- **每次运行最多出现一次**

### Triggering matrix

| 情况 | `CHAT_MESSAGE_*` | `CHAT_ATTACHMENTS:` |
|---|---|---|
| 三件物全生成（正常成功） | ✅ | ✅（3 项） |
| zip 打包失败（DEC-6 降级） | ✅ | ✅（MD + CSV 共 2 项） |
| R16b 空结果（无凭证下载） | ✅（包住 "ℹ️ 本次未下载到凭证…"段） | ❌ |
| 非零 exit 且 `print_openclaw_summary` 未被调到（auth / LLM config 早期错误） | ❌ | ❌ |
| exit=5 partial 但三件物部分存在 | ✅ | ✅（实际生成的部分） |

### Agent Playbook — new SKILL.md § Presenting Results to the User

新增一节（独立于 § Loop Playbook），内容：

> 每次 Skill 运行结束后：
>
> 1. 扫描 stdout，定位 `CHAT_MESSAGE_START` 和 `CHAT_MESSAGE_END` 两行
> 2. 把两者之间的内容 **原文** 发给用户 — 不增、不删、不翻译、不精炼、不挑重点。包括任何 emoji / 中文 / 换行 / 末尾提示
> 3. 如果 stdout 里还有 `CHAT_ATTACHMENTS: {...}`，解析 JSON，**用当前 channel 的消息工具** 依序把每个 file 作为附件发到当前会话，caption 使用 `file.caption`
>    - 飞书 channel：用消息工具发文件
>    - Slack / Discord / 其它 IM：用对应的消息工具发文件
>    - 当前 channel 的消息工具不支持文件附件（纯 SMS 之类）：跳过上传，把本地路径附在消息里
> 4. 单个文件上传失败不要中断：同一条回复里追加 `⚠️ {filename} 上传失败（{reason}），请从 {abs_path} 取`，继续下一个
> 5. 没有 `CHAT_MESSAGE_START/END` 标记时（早期错误路径），按 `REMEDIATION:` 行处理，不尝试附件
>
> **顺序**：先文字摘要，再附件。

### Message-vs-attachment ordering

用户聊天里最终看到的序列：

1. Agent 转发 `CHAT_MESSAGE_START` / `END` 之间的人读摘要（包含 "📦 报销包: /abs/..." 路径行和 "💡..." 提示）
2. Agent 依 `files` 顺序上传附件（`报销包` → `报告` → `明细`）
3. 任何上传失败的补一行 ⚠️ 提示

路径在消息里出现、文件也作为附件出现是**有意保留的冗余**：附件上传失败时路径即 fallback；成功时路径告知用户文件所在机器位置。

## Implementation surface

### `scripts/postprocess.py::print_openclaw_summary`

- 函数开头（参数校验之后，任何 `writer(...)` 之前）：`writer("CHAT_MESSAGE_START")`
- 所有 `return` 点之前（R16b 空结果分支 + 正常结束）：`writer("CHAT_MESSAGE_END")`
- `CHAT_MESSAGE_END` 之后，**在正常路径上**（voucher_count > 0 或 has_rows）：构造 `files` 列表并 `writer(f"CHAT_ATTACHMENTS: {json.dumps(payload, ensure_ascii=False)}")`
  - zip_path=None 时，跳过 zip 项
  - R16b 分支已提前 return，不会走到 attachments 输出
- `files` 顺序：zip（若存在）→ MD → CSV
- caption 常量：`"报销包"` / `"报告"` / `"明细"`
- 约 15 行新增代码

**关键实现约束**：`CHAT_MESSAGE_END` 必须出现在**每个** return 点前 — R16b 分支和正常结束各有一次；漏一个就会让 Agent 看不到闭合锚点。

### `SKILL.md`

- 新增 § Presenting Results to the User（Playbook 内容见上）
- 放置位置：§ Exit Codes 之后、§ Loop Playbook 之前（或另行讨论）
- 约 30 行

### `tests/test_postprocess.py::TestPrintOpenClawSummary`

现有测试类里新增断言：

- `test_sentinels_present_normal_path` — 正常三件物齐全时，输出含 `CHAT_MESSAGE_START`、`CHAT_MESSAGE_END`、`CHAT_ATTACHMENTS:` 三行
- `test_attachments_skipped_on_empty_result` — R16b（voucher_count=0 且无 rows）时，含 `CHAT_MESSAGE_*` 但不含 `CHAT_ATTACHMENTS:`
- `test_attachments_omits_zip_when_zip_failed` — `zip_path=None` 时 attachments JSON 只有 MD + CSV 两项
- `test_attachment_captions_are_canonical` — caption 严格为 `"报销包"` / `"报告"` / `"明细"`
- `test_message_boundary_wraps_all_user_text` — "💡 发现不该报销的…" 两行落在 `CHAT_MESSAGE_START/END` 之间
- `test_attachments_json_is_valid` — `CHAT_ATTACHMENTS:` 之后严格是合法 JSON，`files[].path` 为绝对路径

### `tests/test_agent_contract.py`

新增合约级别测试：

- sentinel 前缀/锚点行严格匹配（正则或字面量）
- sentinel 成对出现且顺序正确（START 在 END 之前，END 在 ATTACHMENTS 之前）
- 没有 stray 出现（sentinel 字面量在非预期位置不出现）
- JSON schema 合规

## Invariants (must remain true)

- 两个 sentinel 在单次 Skill 运行 stdout 中**各自至多出现一次**
- `CHAT_MESSAGE_START` → `CHAT_MESSAGE_END` → `CHAT_ATTACHMENTS:` 严格顺序（后两者若缺席不影响前者顺序要求）
- `CHAT_ATTACHMENTS:` 存在 ⇒ `CHAT_MESSAGE_*` 一定存在（前者永远晚于后者）
- `CHAT_MESSAGE_*` 在 run.log 中也会出现（`writer=say` 双写）— 可接受，不破坏调试
- 现有 "📦 报销包（提交这个）: ..." 行保留在人读摘要里，即使 attachments sentinel 携带同一路径

## Open questions

无 — 关键决策已在 brainstorm 中拍板：
- 附件范围：zip + MD + CSV 全发
- 契约形状：stdout sentinel（不是路径正则、不是 sidecar 文件）
- 失败语义：best-effort + 透明警告
- payload 字段：`{path, caption}`
- caption 取值：`报销包` / `报告` / `明细`（精简版）
- Sentinel 数量：2（message + attachments 独立）
- Playbook 放置：SKILL.md 新节 § Presenting Results to the User
- 顺序：先文字后附件
- Channel 不支持附件：路径附在消息里降级

## Success criteria

- 用户在飞书 chat 里看到：(a) 完整的人读摘要（含 "💡 发现不该报销的…" 那两行）；(b) 三个文件作为附件；顺序为摘要在前，附件在后
- 切换到非飞书 channel（Slack / Discord / iMessage）时，无需改 Skill，同样链路可工作
- `tests/test_postprocess.py` + `tests/test_agent_contract.py` 新增断言全绿
- 现有 195 个测试不回退
