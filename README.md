# gmail-invoice-downloader

批量从 Gmail 下载发票 / 水单 / 行程单，自动 OCR、配对、生成财务报销包。针对中国增值税电子发票 + 境外酒店 folio 场景，一条命令跑完 10 步 pipeline。

面向的是用 OpenClaw 等 Agent 做报销自动化的开发者，也可以当独立 CLI 单独用。

## 能做什么

一条命令输入日期区间，输出：

- `下载报告.md` — 带金额汇总表 + P1/P2/P3 匹配详情的中文报告
- `发票汇总.csv` — UTF-8 BOM，Excel 直接打开，带小计 + 总计行
- `missing.json` — Agent 可读的状态机（`converged` / `needs_retry` / `max_iterations_reached` / `user_action_required`）
- `发票打包_<时间>.zip` — 交给财务的最终打包（仅 PDF + MD + CSV）

支持 9 种中国电子发票平台 short link：百望云 / 诺诺网 / fapiao.com / xforceplus / 云票 / 百旺金穗云 / 金财数科 / 克如云 / 12306。

## 快速开始

```bash
# 1. 装依赖（用到 curl + poppler-utils，需要先 brew install）
brew install poppler
pip install -r requirements.txt

# 2. 配 Gmail OAuth（见 references/setup.md）
python3 scripts/gmail-auth.py

# 3. 配 LLM provider（四选一，Bedrock 为默认）
export AWS_REGION=us-east-1
export BEDROCK_MODEL_ID=global.anthropic.claude-opus-4-7

# 4. preflight 检查（exit 0 = 全绿）
python3 scripts/doctor.py

# 5. 跑一次
python3 scripts/download-invoices.py \
    --start 2026/01/01 --end 2026/04/01 \
    --output ~/invoices/2026-Q1
```

完整参数 + LLM provider 选型 + Agent Loop Playbook 见 [`SKILL.md`](SKILL.md)。

## 架构

10 步 pipeline，分两层：

| 层 | 负责 | 代码 |
|---|---|---|
| Layer 1：Gmail 下载（v5.2 稳定） | Step 1–5：搜索 / 分类 / 附件 + ZIP + 9 个平台 short link 解析 / `%PDF` header 校验 | `scripts/invoice_helpers.py` |
| Layer 2：LLM 后处理（v5.4） | Step 6–10：OCR + 可信度校验 / 按 OCR 重命名 / P1 remark → P2 日期+金额 → P3 同日兜底 / 写 3 份交付物 + zip | `scripts/postprocess.py` + `scripts/core/` |

完整的目录说明、每步数据流、Agent Loop 契约在 [`CLAUDE.md`](CLAUDE.md) 和 [`SKILL.md`](SKILL.md)。

## 开发

```bash
# 177 个测试，全量 2.5 秒跑完，离线 mock 了 Gmail + LLM
python3 -m pytest tests/ -q

# 想跑集成测试需要的真实 PDF 样本，放到：
#   ~/Documents/agent Test/    (默认路径)
# 或用环境变量指定：
export GMAIL_INVOICE_FIXTURES=/path/to/your/samples
python3 -m pytest tests/ -q
```

季度 smoke runbook：[`references/seasonal-smoke.md`](references/seasonal-smoke.md)

## 依赖

- 必须：Python 3.10+、`boto3>=1.35.17`、`curl`、`pdftotext` (poppler-utils)
- 可选：`anthropic>=0.34`（LLM_PROVIDER=anthropic）、`openai>=1.50`（LLM_PROVIDER=openai）
- 测试：`pytest`、`pytest-mock`

## License

[MIT](LICENSE)
