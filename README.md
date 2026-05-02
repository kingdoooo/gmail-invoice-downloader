# gmail-invoice-downloader

批量下载 Gmail 里的报销票据，自动 OCR、配对、出交付物。覆盖中国境内酒店住宿（发票 + 水单）、餐饮、网约车（发票 + 行程单）、话费等常见报销场景，一条命令跑完 10 步 pipeline。

面向用 OpenClaw 等 Agent 做报销自动化的开发者，也可以当独立 CLI 单独用。

> 目前只附带 Gmail OAuth 配置指南。下载 + OCR + 匹配 + 报告这套后半段跟邮件源解耦 —— 只要另一个邮箱（Outlook、Lark Mail、企业邮箱 IMAP 等）能通过 API / connector 拿到附件和正文，改一个 `GmailClient` 实现就能复用。

## 能做什么

一条命令输入日期区间，输出：

- `下载报告.md` — 带金额汇总表、P1/P2/P3 匹配详情、跨季度边界项说明的中文报告
- `发票汇总.csv` — UTF-8 BOM，Excel 直接打开，带小计 + 总计行
- `missing.json` — Agent 可读的状态机（`converged` / `needs_retry` / `max_iterations_reached` / `user_action_required`），v1.0 schema
- `发票打包_<时间>.zip` — 交给财务的最终打包（仅 PDF + MD + CSV）

覆盖的票据类型：

- **酒店发票**（中国增值税电子普票 / 专票）+ **水单 / Guest Folio**（自动按 `确认号` / `日期+金额` / 同日兜底三级匹配）
- **网约车发票** + **行程报销单**（按金额匹配，发票号做 tiebreaker）
- **餐饮** / **话费** / **铁路电子客票** / **通行费** 等其它类目

支持 9 种中国电子发票平台 short link：百望云 / 诺诺网 / fapiao.com / xforceplus / 云票 / 百旺金穗云 / 金财数科 / 克如云 / 12306。Agent 遇到新平台时可调 `scripts/probe-platform.py` 逆向 + 走 `scripts/record-unknown-platform.py` 把未知平台的邮件上报给用户（Agent Loop Playbook 自动处理）。

## 快速开始

```bash
# 1. clone
git clone git@github.com:kingdoooo/gmail-invoice-downloader.git
cd gmail-invoice-downloader

# 2. 装依赖（用到 curl + poppler-utils，需要先 brew install）
brew install poppler
pip install -r requirements.txt

# 3. 配 Gmail OAuth（见 references/setup.md）
#    默认凭据目录：~/.openclaw/credentials/gmail/
#    放入 credentials.json 后运行：
python3 scripts/gmail-auth.py

# 4. 配 LLM provider（默认 Bedrock，Sonnet 4.6）
export AWS_REGION=us-east-1
# BEDROCK_MODEL_ID 默认 global.anthropic.claude-sonnet-4-6
# 想换 Opus: export BEDROCK_MODEL_ID=global.anthropic.claude-opus-4-7
# 其它 provider（Anthropic / OpenAI / 兼容端点 / 离线 none）见 SKILL.md

# 5. preflight 检查（exit 0 = 全绿）
python3 scripts/doctor.py

# 6. 跑一次
python3 scripts/download-invoices.py \
    --start 2026/01/01 --end 2026/04/01 \
    --output ~/invoices/2026-Q1
```

完整参数、多 LLM provider 选型（Bedrock / Anthropic / Anthropic-compatible / OpenAI / OpenAI-compatible / none）、Agent Loop Playbook、Exit Codes 契约、Lessons Learned 都在 [`SKILL.md`](SKILL.md)。

## 交给 Agent 跑

这个 repo 是 **Agent-ready** 的 —— `CLAUDE.md` 写了架构边界和编辑规范，`SKILL.md` 是 Agent 执行契约（含 Loop Playbook、Exit Codes、`missing.json` schema）。把 repo URL 给能跑 shell 的 Agent（Claude Code / OpenClaw Skill / Cursor / Cline 等），它会自己 clone、读上下文、跑起来。

你需要先告诉 Agent 的三件事（直接把这个Repo链接丢给 Agent 也行，它会指导你按流程走 ）：

1. **Gmail OAuth 凭据** —— 参考 [`references/setup.md`](references/setup.md) 在 Google Cloud 建一个 OAuth client，把 `credentials.json` 放到 `~/.openclaw/credentials/gmail/credentials.json`（或另选路径 + 运行时带 `--creds <path>` 覆盖）。首次跑 `python3 scripts/gmail-auth.py` 会交互式地生成 `token.json`，之后自动刷新 —— 这一步需要人打开浏览器授权，Agent 做不了。
2. **LLM provider 凭据** —— 默认 AWS Bedrock，需要 `AWS_REGION` + 任一种 AWS 凭据（IAM role / `AWS_PROFILE` / AK/SK / `AWS_BEARER_TOKEN_BEDROCK`）。要用 Anthropic / OpenAI 直连或兼容端点（DeepSeek / OpenRouter / LiteLLM 等）看 [`SKILL.md` § LLM Provider](SKILL.md)。
3. **日期范围 + 输出目录** —— 比如 "下载 2026 年第一季度的报销票据到 `~/invoices/2026-Q1`"，或者更宽松的 "上个季度的"，Agent 会翻成 `--start YYYY/MM/DD --end YYYY/MM/DD`。

Agent 自动做的事：`pip install -r requirements.txt`、`python3 scripts/doctor.py` 自检、跑主命令、读 `missing.json` 决定要不要 `--supplemental` 补搜、对 failed short link 调 `scripts/probe-platform.py` 做补救、最后把 `发票打包_<时间>.zip` 交付给你。遇到未知平台 Agent 不会盲改代码 —— 会通过 `scripts/record-unknown-platform.py` 把信息上报到 `missing.json.items[]`，让你决定是否扩展支持（[`references/platforms.md`](references/platforms.md) 有 5-step playbook）。

> 目前 `scripts/gmail-auth.py` 里的凭据路径在某些平台上是硬编码的 `/home/ubuntu/...`。macOS / 其它 Linux 用户首次跑可能需要改成自己的路径，或把 `credentials.json` 放到脚本期望的位置。这是已知的小摩擦，路线图里会清理。

## 架构

10 步 pipeline，分两层：

| 层 | 负责 | 代码 |
|---|---|---|
| Layer 1：Gmail 下载（v5.2 稳定） | Step 1–5：搜索 / 分类 / 附件 + ZIP + 9 个平台 short link 解析 / `%PDF` header 校验 | `scripts/invoice_helpers.py` |
| Layer 2：LLM 后处理 | Step 6–10：OCR（ThreadPool 默认并发 5，可用 `LLM_OCR_CONCURRENCY` 覆盖）+ 可信度校验 / 按 OCR 重命名 / P1 remark → P2 日期+金额 → P3 同日兜底 / 跨季度边界项路由到 `out_of_range_items[]` / 写 3 份交付物 + zip | `scripts/postprocess.py` + `scripts/core/` |

当脚本的批量下载跑完后如果有 failed short link，Agent 可以用 `--postprocess-only` 跳过 Gmail、只重跑 Step 6–10，配合 `scripts/probe-platform.py` 做手工 / 自动补救。

完整的目录说明、每步数据流、Agent Loop 契约、文件级改动边界在 [`CLAUDE.md`](CLAUDE.md) 和 [`SKILL.md`](SKILL.md)。

## 开发

```bash
# 229 个测试，全量 ~6 秒，离线 mock 了 Gmail + LLM
python3 -m pytest tests/ -q

# 跑集成测试需要真实 PDF 样本，默认路径：
#   ~/Documents/agent Test/
# 或用环境变量覆盖：
export GMAIL_INVOICE_FIXTURES=/path/to/your/samples
python3 -m pytest tests/ -q
```

季度 smoke runbook（真实 Gmail + 真实 LLM）：[`references/seasonal-smoke.md`](references/seasonal-smoke.md)

发布历史 + 升级备注：[`CHANGELOG.md`](CHANGELOG.md)

## 依赖

- 必须：Python 3.10+、`boto3>=1.35.17`、`curl`、`pdftotext` (poppler-utils)
- 可选：`anthropic>=0.34`（`LLM_PROVIDER=anthropic` / `anthropic-compatible`）、`openai>=1.50`（`LLM_PROVIDER=openai` / `openai-compatible`）
- 测试：`pytest`、`pytest-mock`

Gmail OAuth 走手写 HTTPS + OAuth token 刷新（`GmailClient._api_get`）。`google-api-python-client` 故意不引入 —— 依赖更少，transient 网络错误的重试策略更可控。

## License

[MIT](LICENSE)
