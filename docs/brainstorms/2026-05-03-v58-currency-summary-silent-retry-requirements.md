# v5.8 — 货币感知的 IGNORED 摘要 + run_supplemental 静默分支 — Requirements

**Date**: 2026-05-03
**Status**: draft — awaiting user review before planning
**Origin**: 2025Q1 批次跑完后复盘发现三个独立但互相牵扯的问题：
1. 摘要里 `📭 已忽略 N 张非报销票据（详见下载报告.md）` 那一行信息密度太低，Agent 照搬之后用户在聊天里看不到哪个 sender、总金额、CTA，add-exclusion 的闭环断了。
2. `下载报告.md` §已忽略的非报销票据 里把 4 张 Anthropic USD 账单显示成 `¥10.00` — OCR prompt 里根本没有 `currency` 字段，渲染代码读一个 LLM 从不产出的字段，fallback 永远走 `¥`。
3. `status=needs_retry` 时 Skill 先发一套完整摘要 + 中间 zip 给用户，Agent 再跑补搜后又发一套最终的 —— 用户收到 2 份作废的交付物（CSV / MD / zip 都会变）。

## Problem

三个症状其实覆盖三层：

- **呈现层（E）**：`print_openclaw_summary` 对 IGNORED 只给 N，用户没法在聊天里直接做"要不要加过滤"的决定。
- **数据层（货币）**：OCR prompt 缺 `currency`，导致下游 IGNORED 呈现 hard-code `¥` 前缀 → 非 CNY 账单永远显示错。
- **合约层（F）**：Skill 把中间状态 (`run_supplemental`) 当作终态交付，产物 (csv/md/zip) 之后还会变。

## Goals

- IGNORED 摘要行扩成 "总金额 + 主 sender + 回复 CTA"，币种正确
- OCR schema 新增 `currency` 字段，下游（MD IGNORED、摘要）共用一份符号查表
- `status=needs_retry`（= `recommended_next_action=="run_supplemental"`）在**非补搜**的初运行里**不向用户发任何东西**，只给 Agent stderr 一个 `AGENT_HINT:`；用户在整个流程里只看到一次最终交付物
- 改动拆成三个独立 Unit，同一个 v5.8 bundle 发布

## Non-Goals

- **不** 扩 aggregation 支持多币种总计（IGNORED 不进 aggregation，reimbursable 100% CNY）
- **不** 改 CSV 币种显示（CSV 没 IGNORED 行；reimbursable 就是 `¥`）
- **不** 改 `missing.json` schema — 仍是 1.0，不加 `ignored_count` / `run_supplemental_pending` 顶层字段
- **不** 加 exit code 6。`run_supplemental` 延迟路径复用 exit 5 + stderr `AGENT_HINT:` 区分子语义
- **不** 主动清 `~/.cache/gmail-invoice-downloader/ocr/`（开发阶段、单用户，升级路径非对外合约）
- **不** 让 Skill 自动写 `learned_exclusions.json` — "加 <domain>" 指令由 Agent 处理，Skill 只发 CTA
- **不** 在静默分支产出"预览 MD / 草稿 CSV"。硬盘上的产物就是原 CSV/MD/zip，供补搜 merge；不给用户发就是不给发
- **不** 在 Skill 里加 Agent "占位消息" 逻辑。主运行静默期间的 UX 兜底属于 Agent 职责，SKILL.md 里**建议**但不强制

## Design

### Unit 切分

| Unit | 名称 | 层 | 依赖 |
|---|---|---|---|
| A | currency in OCR schema | 数据层 + MD 呈现层 | — |
| B | IGNORED summary line | 摘要呈现层 | A（消费 `currency` + `currency_symbol`） |
| C | `run_supplemental` 静默分支 | 合约层 | — |

推荐 commit 顺序：A → B → C → v5.8 chore (SKILL.md line 1 + CHANGELOG)。A/B/C 在同一 `feature/v5.8-currency-summary-silent-retry` 分支，一个 PR，4 个 commits。

### Unit A — OCR schema 加 `currency` + IGNORED MD 渲染切符号

**prompt 改动** (`scripts/core/prompts.py`)
通用字段表新增一条：

| currency | string | ISO-4217 三字母币种代码。大陆增值税发票/普票/电子发票 = "CNY"；美元账单 = "USD"；欧元 = "EUR"；未写明或识别不出 → 仍返回 "CNY"（保守默认，避免误改人民币发票）|

同时在所有 example JSON（电子发票、酒店水单、网约车行程单）里显式列出 `"currency": "CNY"`，锚定 LLM 输出。

**defensive fill** (`scripts/core/llm_ocr.py`)
返回前：若 `ocr.get("currency")` 为 None/空串，回填 `"CNY"`。兼容老 cache（`llm_ocr.py:148` cache key = `sha256(pdf_bytes)[:16]`，不含 prompt，所以 prompt 变动不失效 cache；老 cache 没 `currency` 字段 → 回填）。

**符号查表** (`scripts/postprocess.py` 顶层工具区)

```python
_CURRENCY_SYMBOLS = {"CNY": "¥", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "HKD": "HK$"}

def currency_symbol(code: str | None) -> str:
    return _CURRENCY_SYMBOLS.get((code or "CNY").upper(), (code or "CNY").upper() + " ")
```

放 `postprocess.py` 而非 `core/` — `core/` 是 reimbursement-helper snapshot，本地增量会增加 sync 成本；`currency_symbol` 只有本 Skill 的呈现层消费。

**MD 渲染切换** (`scripts/download-invoices.py:754-757`)

现状：
```python
prefix = "¥" if not currency or currency == "CNY" else ""
suffix = f" {currency}" if currency and currency != "CNY" else ""
lines.append(f"- {label}：{prefix}{amount:.2f}{suffix}")
```

改为：
```python
sym = currency_symbol(ocr.get("currency"))
lines.append(f"- {label}：{sym}{amount:.2f}")
```

从 `postprocess` 导入 `currency_symbol`。

**错误处理**
- LLM 返回 null / 空 / 非法 currency → `llm_ocr.py` 回填 `"CNY"`
- 未知三字母码（"RMB" / "USS"）→ 查表 miss → fallback `"RMB "` / `"USS "` + 金额（不让奇怪输入吃掉符号）
- 老 cache 命中（无 `currency`）→ 回填 `"CNY"`，IGNORED 的 Anthropic 老 cache 命中时仍显示错误的 `¥`，但开发阶段单用户，不主动清

### Unit B — 摘要带 IGNORED 明细（E 项，选项 A）

**当前** (`scripts/postprocess.py:1229-1234`)
```python
if ignored_count:
    writer(
        f"📭 已忽略 {ignored_count} 张非报销票据"
        f"（详见下载报告.md §已忽略的非报销票据）"
    )
```

**改后** — 2 行 "总金额 + 主 sender + 回复 CTA"

```python
if ignored_records:
    totals, top_domain, _ = _ignored_summary(ignored_records)
    total_str = " / ".join(
        f"{currency_symbol(cur)}{amt:.2f}"
        for cur, amt in sorted(totals.items())
    )
    if top_domain != "未知发件人":
        writer(
            f"📭 已忽略 {len(ignored_records)} 张非报销票据"
            f"（{total_str}，主要来自 {top_domain}）"
        )
        writer(
            f'   加过滤规则？回复 "加 {top_domain}" '
            f"我帮你写进 learned_exclusions.json"
        )
    else:
        writer(f"📭 已忽略 {len(ignored_records)} 张非报销票据（{total_str}）")
```

**新辅助函数** (`scripts/postprocess.py`)
```python
def _ignored_summary(records: list[dict]) -> tuple[dict[str, float], str, int]:
    """Aggregate IGNORED → (totals-by-currency, top-sender-domain, top-count).

    Amount parsing mirrors download-invoices.py:749-753: unconvertible
    amounts contribute 0.00 to their currency bucket. Records with empty
    sender_email bucket as '未知发件人'.
    """
    totals: dict[str, float] = {}
    domain_counts: dict[str, int] = {}
    for rec in records:
        ocr = rec.get("ocr") or {}
        raw_amt = ocr.get("transactionAmount")
        try:
            amt = float(raw_amt) if raw_amt not in (None, "") else 0.0
        except (TypeError, ValueError):
            amt = 0.0
        cur = (ocr.get("currency") or "CNY").upper()
        totals[cur] = totals.get(cur, 0.0) + amt

        sender = rec.get("sender_email") or ""
        domain = sender.split("@", 1)[-1] if "@" in sender else "未知发件人"
        domain_counts[domain] = domain_counts.get(domain, 0) + 1

    top_domain, top_count = max(domain_counts.items(), key=lambda kv: kv[1])
    return totals, top_domain, top_count
```

**接口变动**
`print_openclaw_summary`: `ignored_count: int = 0` → `ignored_records: list[dict] | None = None`。两个调用点（`download-invoices.py:1041` 和 `:1462`）都从 `ignored_count=len(ignored_records)` 改成 `ignored_records=ignored_records`。

**样例输出**（2025Q1 4 张 Anthropic USD 账单）：
```
📭 已忽略 4 张非报销票据（$60.00，主要来自 mail.anthropic.com）
   加过滤规则？回复 "加 mail.anthropic.com" 我帮你写进 learned_exclusions.json
```

**边界**
- 空列表 / None → 不打印
- 所有 sender 缺失 → top_domain == `"未知发件人"` → 省 CTA 行，只保留第一行（不含 "主要来自..."）
- 多 sender 同 count 并列 → Python dict 插入序首位，不稳定但无害（YAGNI，不单独排序）
- 多币种混合（理论上 USD + CNY）→ `total_str = "¥10.00 / $60.00"`，字母序

### Unit C — `run_supplemental` 静默分支（F-1）

**当前** (`scripts/download-invoices.py:1031` 和 `:1452`)
无论 `missing_status` 如何，都调 `print_openclaw_summary(...)`，打两对哨兵 + `CHAT_ATTACHMENTS:`。

**改后**：两个调用点前加分支

```python
if (not args.supplemental
        and missing_data.get("recommended_next_action") == "run_supplemental"):
    sys.stderr.write(
        f"AGENT_HINT: run_supplemental "
        f"--start {args.start} --end {args.end} "
        f"--output {shlex.quote(os.path.abspath(args.output))}\n"
    )
    log.close()
    sys.exit(EXIT_PARTIAL)  # 5

print_openclaw_summary(...)
```

**关键不变式**
- 只在**非补搜的初运行** + `recommended_next_action == "run_supplemental"` 触发
- `args.supplemental=True` 的补搜**永远发哨兵 + attachments**，即使状态仍是 `run_supplemental`（防用户沉默）
- 其他两种 missing_status（`stop` / `ask_user`）照常发哨兵

**Exit code 复用**
- 不加 exit 6。exit 5 复用，stderr 哨兵区分子语义：
  - `REMEDIATION: ...` → 手工处理
  - `AGENT_HINT: run_supplemental ...` → 自动恢复

**SKILL.md 改动（5 节 + 1 节新增）**

1. **§Exit Codes** 表 exit=5 行加注解："若 stderr 含 `AGENT_HINT: run_supplemental ...`，**不要**转发 stdout（stdout 为空或仅含 run.log 输出），直接按该命令跑补搜；否则按现有规则处理 UNPARSED / failed。"

2. **§Presenting Results / §Agent Playbook** 第 1 步前加守则 0：
   > **0. 如果 stdout 中不存在 `CHAT_MESSAGE_START`**（例如 exit=5 + stderr 有 `AGENT_HINT: run_supplemental`）：跳过本节所有步骤，按 stderr 指令执行下一步命令。不向用户发送任何消息、不上传任何附件。

3. **§Invariants** 松绑：
   > Each sentinel appears **at most once** per Skill run. **May also appear zero times** — on early-error paths (exit 2/3/4), and on the new `run_supplemental` deferral path (exit 5 + `AGENT_HINT:`). Agents MUST check for sentinel presence before forwarding; absence is a valid state.

4. **§Loop Playbook** 追加："主运行不会向用户发中间结果；用户在看到任何东西之前，你应已完成补搜。建议 Agent 在触发补搜前向用户发一句 '正在补搜...' 占位消息，避免长时间静默。"

5. **新增 §User Reply Conventions**：
   > 当 Skill 摘要里出现 `回复 "加 <domain>" ...` 且用户回复 `加 <domain>`，Agent 在 `<output>/../learned_exclusions.json` 的 `senders: []` 追加 `{"domain":"<domain>","reason":"user approved","added_at":"<ISO-8601>"}`，然后确认："已加 <domain>，下次跑该时间窗会自动过滤"。

6. **§Lessons Learned** 新增 `v5.8 — currency-aware IGNORED summary + silent run_supplemental` 节。

### 数据流

**非补搜触发路径**（主运行 → `stop` 或 `ask_user`）
```
Gmail search → download → classify
  ├─ reimbursable  → build_aggregation → CSV + MD + zip
  └─ IGNORED       → rec["ocr"]["currency"]  ← Unit A
                     rec["sender_email"]     (v5.7 Unit 2 既有)
     ↓
  write_report_md §IGNORED: currency_symbol(cur) + amount   ← Unit A
  print_openclaw_summary(..., ignored_records=[...])         ← Unit B
     └─ 2 行：📭 … + CTA "回复 加 <domain>"
     ↓
  CHAT_MESSAGE_START/END + CHAT_ATTACHMENTS: (zip/md/csv)
  exit 0 / 5
```

**补搜触发路径**（主运行 → `run_supplemental`） ← Unit C
```
Gmail search → download → classify → build_aggregation
  写 CSV/MD/zip 到 output_dir（硬盘上，不发给用户）
  missing_data.recommended_next_action == "run_supplemental"
     ↓
  sys.stderr.write("AGENT_HINT: run_supplemental --start ... --end ... --output ...")
  sys.exit(5)   # 无 CHAT_MESSAGE_START/END，无 CHAT_ATTACHMENTS
     ↓
  Agent 读 stderr → 跑 `--supplemental ...`
     ↓
  merge_supplemental_downloads → 重新 matching → 重新 CSV/MD/zip
  print_openclaw_summary(...)  # 补搜这次照常发
  CHAT_MESSAGE_START/END + CHAT_ATTACHMENTS: (zip/md/csv)
  exit 0 / 5 / ask_user
```

### 跨 Unit 接口锁点

| 接口 | 生产者 | 消费者 | 形态 |
|---|---|---|---|
| `rec["ocr"]["currency"]` | A (llm_ocr.py 出口) | A (MD 渲染) + B (`_ignored_summary`) | str，缺省 `"CNY"` |
| `rec["sender_email"]` | v5.7 Unit 2 既有 | B (`_ignored_summary`) | str，可能 `""` |
| `currency_symbol(code)` | A (`postprocess.py` 顶层) | A (MD 渲染) + B (摘要) | `str → str` |
| `ignored_records: list[dict]` | `main()` 既有 | B (`print_openclaw_summary` 新参数) | list |
| stderr `AGENT_HINT: run_supplemental ...` | C (`main()` 新分支) | OpenClaw Agent runtime | stderr 行 |

### 测试矩阵

| 测试类 / 文件 | 覆盖 Unit | 关键断言 |
|---|---|---|
| `TestCurrencySymbolTable`（新）`test_postprocess.py` | A | CNY/USD/EUR/未知/None/小写 |
| `TestIgnoredMdRendering`（新或扩）`test_postprocess.py` | A | USD 记录 → `$10.00`；missing → `¥` |
| `TestIgnoredSummaryLine`（新）`test_postprocess.py` | B | 单币种 / 混合币种 / 空 sender 省 CTA / 空列表无输出 / 非数值金额 |
| `TestPrintOpenClawSummary`（改）`test_postprocess.py` | B | `ignored_count: int` → `ignored_records: list` 签名迁移 |
| `TestRunSupplementalSilence`（新）`test_agent_contract.py` | C | initial+run_supplemental→静默+AGENT_HINT+exit5；supplemental+run_supplemental→照常；initial+stop/ask_user→照常 |
| `TestChatSentinelContract`（松）`test_agent_contract.py` | C | "每次运行都出哨兵" → "非静默路径下出哨兵" |
| 集成 smoke（手动）`references/seasonal-smoke.md` | A+B+C | 真实 4 张 Anthropic 账单 + 一个 needs_retry 场景端到端 |

**回归保护**（已有必须绿）：`TestHotelMatchingTiers`、`TestBuildAggregation` / `TestAggregationConsistency`、`TestDoctorLLMMatrix`、`TestProviderMatrix`。

### 文档改动清单

- `SKILL.md` line 1 → `v5.8`
- `SKILL.md` §Exit Codes / §Presenting Results / §Agent Playbook / §Invariants / §Loop Playbook — Unit C 改写
- `SKILL.md` 新增 §User Reply Conventions —— Unit B CTA 的落地规则
- `SKILL.md` §Lessons Learned 新增 `v5.8 — currency-aware IGNORED summary + silent run_supplemental`
- `CHANGELOG.md` 新版本条目
- `docs/brainstorms/2026-05-03-v58-currency-summary-silent-retry-requirements.md` — 本文档
- `docs/plans/NNN-v58-...-plan.md` — 待 writing-plans 产出

### 风险

- **用户首次跑主运行 → 沉默几十秒到几分钟 → 补搜跑完才看到结果**。缓解：Agent 在触发补搜前发一句自己的占位消息（非 Skill 输出）。SKILL.md §Loop Playbook 建议但不强制。
- **Agent 实现 bug（吞 stderr 或忘跑补搜）→ 用户永远沉默**。Skill 不负责 —— 属 Agent / OpenClaw runtime QA。Loop Playbook 的 `max_iterations_reached` 机制兜底。
- **Prompt 加字段 → LLM 行为漂移**。缓解：增量是"加一个可选字段"，未动现有字段提取规则；example JSON 显式列出 `"currency": "CNY"` 锚定；cache key 不含 prompt，老 cache 仍命中。

### 发布

feature 分支 `feat/v5.8-currency-summary-silent-retry`，单 PR，4 个 commits：
1. `feat(prompts): currency field in OCR schema (v5.8 Unit A)`
2. `feat(report): IGNORED summary line with totals + top sender CTA (v5.8 Unit B)`
3. `feat(contract): silent run_supplemental deferral path (v5.8 Unit C)`
4. `chore(v5.8): bump version + CHANGELOG + SKILL.md v5.8 Lessons Learned`
