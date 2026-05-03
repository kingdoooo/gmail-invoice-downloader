#!/usr/bin/env python3
"""
Gmail Invoice Downloader — end-to-end CLI.

v5.3 upgrade: LLM OCR replaces pdftotext heuristics for invoice field
extraction. Downstream matching (hotel P1/P2/P3, ride-hailing by amount),
the summary CSV, the missing-file feedback loop, and the output .zip are
all new. The Gmail download path (OAuth, 9 platform extractors, ZIP
handling) is unchanged.

Full 11-step workflow:
  1. Load learned_exclusions.json + build Gmail search query
  2. Search Gmail (paginated) within the given date range
  3. Fetch full messages + classify (via invoice_helpers.classify_email)
  4. Download attachments (PDF / ZIP) and resolve link-based downloads
  5. Validate every file with `%PDF` magic bytes
  6. LLM OCR + plausibility validation + classify [v5.3]
  7. Rename PDFs to {date}_{vendor}_{category}.pdf [v5.3]
  8. Match: hotel P1 remark / P2 date+amount / P3 date-only ; ride-hail by amount
  9. Write 下载报告.md + 发票汇总.csv + missing.json
 10. Zip output dir as 发票打包_YYYYMMDD-HHMMSS.zip

Usage:
    python3 download-invoices.py --start 2026/01/01 --end 2026/05/01 --output ./out

Supplemental mode (loop to fill gaps):
    python3 download-invoices.py --supplemental --start ... --end ... \\
        --output ./out --query "水单 OR folio 万豪"

Defaults to credentials at ~/.openclaw/credentials/gmail/{credentials,token}.json.
LLM: defaults to Bedrock (IAM role / instance profile). Override with
LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY, or --no-llm to skip OCR entirely.

Exit codes: 0=ok, 2=auth, 3=llm_config, 4=gmail_quota, 5=partial, 1=unknown.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

# Import invoice_helpers from the same scripts/ directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from invoice_helpers import (  # noqa: E402
    classify_email,
    validate_pdf_header,
    make_unique_path,
    generate_filename,
    extract_date_from_email,
    extract_pdfs_from_zip,
    resolve_baiwang_short_url,
    resolve_baiwang_bwfp_short_url,
    resolve_nuonuo_short_url,
    resolve_bwjf_short_url,
    resolve_keruyun_short_url,
)

# v5.3 additions
from postprocess import (  # noqa: E402
    analyze_pdf_batch,
    build_aggregation,
    currency_symbol,
    rename_by_ocr,
    do_all_matching,
    print_openclaw_summary,
    write_summary_csv,
    write_missing_json,
    zip_output,
    merge_supplemental_downloads,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
)
from core.llm_client import LLMAuthError, LLMConfigError  # noqa: E402


# v5.3 exit codes
EXIT_OK = 0
EXIT_UNKNOWN = 1
EXIT_AUTH = 2
EXIT_LLM_CONFIG = 3
EXIT_GMAIL_QUOTA = 4
EXIT_PARTIAL = 5


class GmailQuotaError(Exception):
    """Raised by GmailClient when Gmail API returns a rate/quota error (429 / 403
    userRateLimitExceeded). Distinguishes quota exhaustion from auth failure so
    the CLI can exit with EXIT_GMAIL_QUOTA and the Agent can wait-and-retry."""

CST = datetime.timezone(datetime.timedelta(hours=8))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
DEFAULT_CREDS = os.path.expanduser("~/.openclaw/credentials/gmail/credentials.json")
DEFAULT_TOKEN = os.path.expanduser("~/.openclaw/credentials/gmail/token.json")

INVOICE_KEYWORDS = (
    "发票 OR invoice OR 水单 OR receipt OR folio OR \"e-folio\" "
    "OR 账单 OR 话费 OR 滴滴 OR 行程报销单 OR 电子发票 "
    "OR (from:12306@rails.com.cn has:attachment)"
)


# ─── Gmail client (with auto token refresh) ────────────────────────────────

class GmailClient:
    def __init__(self, creds_path, token_path):
        with open(creds_path) as f:
            self.creds = json.load(f)["installed"]
        self.token_path = token_path
        with open(token_path) as f:
            self.token = json.load(f)

    def _refresh(self):
        data = urllib.parse.urlencode({
            "client_id": self.creds["client_id"],
            "client_secret": self.creds["client_secret"],
            "refresh_token": self.token["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(self.creds["token_uri"], data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req) as resp:
            new = json.loads(resp.read())
        self.token["access_token"] = new["access_token"]
        with open(self.token_path, "w") as f:
            json.dump(self.token, f, indent=2)

    # Transient network errors (SSL EOF, socket timeouts, connection resets)
    # are retried with exponential backoff. Discovered during 2025Q4 seasonal
    # smoke: a single SSL UNEXPECTED_EOF_WHILE_READING during attachment fetch
    # dropped one invoice from iter 1 entirely. The Agent loop recovered it in
    # iter 2, but that's the wrong layer for a one-off network blip.
    _TRANSIENT_BACKOFF_SEC = (0.5, 1.0, 2.0)

    def _api_get(self, url):
        for _ in range(2):  # 401 → refresh → single retry
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {self.token['access_token']}")
            try:
                return self._fetch_with_transient_retry(req)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self._refresh()
                    continue
                # 429 = rate limit; 403 with Gmail's 'userRateLimitExceeded' /
                # 'quotaExceeded' reason body is a quota error, not an auth
                # failure. Surface as GmailQuotaError so main() can map to
                # EXIT_GMAIL_QUOTA and let the Agent wait-and-retry.
                if e.code == 429:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    raise GmailQuotaError(
                        f"Gmail rate limit (429); Retry-After={retry_after}"
                    ) from e
                if e.code == 403:
                    body = b""
                    try:
                        body = e.read()
                    except Exception:
                        pass
                    body_text = body.decode("utf-8", errors="ignore").lower()
                    if "quota" in body_text or "ratelimit" in body_text:
                        raise GmailQuotaError(
                            f"Gmail 403 quota/rate-limit: {body_text[:200]}"
                        ) from e
                raise
        raise RuntimeError("failed after token refresh")

    def _fetch_with_transient_retry(self, req):
        """Retry transient network errors (SSL EOF, timeouts, connection
        resets) with exponential backoff. HTTPError is re-raised immediately
        so the 401/429/403 handlers in _api_get keep their semantics."""
        backoffs = self._TRANSIENT_BACKOFF_SEC
        for i, backoff in enumerate(backoffs):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError:
                raise  # auth / quota / 5xx — handled by caller
            except (urllib.error.URLError, ssl.SSLError, TimeoutError,
                    ConnectionError) as e:
                print(
                    f"  ⏳ transient network error ({type(e).__name__}), "
                    f"retry {i + 1}/{len(backoffs)} after {backoff}s: {e}",
                    file=sys.stderr,
                )
                time.sleep(backoff)
        # Final attempt — let the exception propagate on failure
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def search(self, query, max_results=1000):
        q = urllib.parse.quote(query)
        page_size = 100
        messages = []
        page_token = None
        while len(messages) < max_results:
            url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={q}&maxResults={page_size}"
            if page_token:
                url += f"&pageToken={page_token}"
            data = self._api_get(url)
            batch = data.get("messages", [])
            messages.extend(batch)
            page_token = data.get("nextPageToken")
            if not page_token or not batch:
                break
        return messages[:max_results]

    def get_full_message(self, msg_id):
        return self._api_get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}?format=full"
        )

    def get_attachment_bytes(self, msg_id, att_id):
        d = self._api_get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/attachments/{att_id}"
        )
        return base64.urlsafe_b64decode(d["data"])


# ─── Query construction ────────────────────────────────────────────────────

def build_query(start, end, exclusions):
    rules = " ".join(e["rule"] for e in exclusions)
    return f"after:{start} before:{end} ({INVOICE_KEYWORDS}) {rules}"


def load_exclusions(skill_dir):
    path = os.path.join(skill_dir, "learned_exclusions.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f).get("exclusions", [])


# ─── Merchant / date resolution ────────────────────────────────────────────

def pick_merchant(classified_entry):
    """Priority: hotel_name -> merchant (from body) -> subject patterns -> sender fallback."""
    if classified_entry.get("hotel_name"):
        return classified_entry["hotel_name"]
    if classified_entry.get("merchant"):
        return classified_entry["merchant"]
    subject = classified_entry.get("subject", "")
    sender = classified_entry.get("sender", "").lower()
    if "滴滴" in subject:
        return "滴滴出行"
    m = re.search(r'来自(.+?)的电子发票', subject)
    if m:
        return m.group(1)
    if "timschina" in sender:
        return "Tim Hortons"
    if "mcd.cn" in sender:
        return "麦当劳"
    if "12306" in sender:
        return "12306"
    if "marriott" in sender:
        m = re.search(r'入住(.+?)的电子', subject)
        if m:
            return m.group(1)
        return "万豪酒店"
    return "未知商户"


def pick_date(classified_entry, body=""):
    """Priority: body "开具日期" -> subject/body/filename regex -> internalDate (CST)."""
    if classified_entry.get("invoice_date"):
        return classified_entry["invoice_date"]
    subj = classified_entry.get("subject", "")
    fns = " ".join(a["filename"] for a in classified_entry.get("pdf_attachments", []))
    d = extract_date_from_email(subj, body, fns)
    if d:
        return d
    if classified_entry.get("internal_date"):
        ts = int(classified_entry["internal_date"]) / 1000
        return datetime.datetime.fromtimestamp(ts, tz=CST).strftime("%Y%m%d")
    return "00000000"


# ─── Helpers: ZIP attachment discovery ────────────────────────────────────

def find_zip_atts(payload):
    out = []
    fn = payload.get("filename", "")
    if fn.lower().endswith(".zip") and payload.get("body", {}).get("attachmentId"):
        out.append((fn, payload["body"]["attachmentId"]))
    for part in payload.get("parts", []):
        out.extend(find_zip_atts(part))
    return out


# ─── Download pipeline ─────────────────────────────────────────────────────

def _infer_doc_type_per_attachment(filename, fallback):
    """Per-file doc_type override when a single email carries both folio + invoice.

    The email-level `doc_type` is one label; when multiple attachments are
    present (e.g. `OperaPrint.pdf` + `dzfp_{no}_..._timestamp.pdf`), we must
    assign each its correct label by filename alone.
    """
    fn = filename.lower()
    if fn.startswith('dzfp_') or '发票' in filename or 'invoice' in fn:
        return "TAX_INVOICE"
    if 'folio' in fn or 'operaprint' in fn or '水单' in filename or '账单' in filename:
        return "HOTEL_FOLIO"
    return fallback


def download_attachment(client, entry, pdfs_dir, log):
    msg_id = entry["message_id"]
    atts = entry.get("pdf_attachments", [])
    if not atts:
        return [], [{"subject": entry.get("subject"), "reason": "no PDF attachment"}]
    downloaded, failed = [], []
    merchant = pick_merchant(entry)
    date_str = pick_date(entry)
    # When there are multiple attachments, each may have a different doc_type
    # (folio + invoice combo). Classify per-file.
    per_file_types = [_infer_doc_type_per_attachment(a["filename"], entry["doc_type"])
                      for a in atts]
    has_mixed = len(set(per_file_types)) > 1

    # Group by doc_type for numbering-within-group
    type_count = {}
    for j, att in enumerate(atts):
        actual_type = per_file_types[j] if has_mixed else entry["doc_type"]
        data = client.get_attachment_bytes(msg_id, att["attachmentId"])
        fname = generate_filename(date_str, merchant, actual_type)
        # When same type repeats within the email, append -1 -2
        type_count[actual_type] = type_count.get(actual_type, 0) + 1
        same_type_total = per_file_types.count(actual_type)
        if not has_mixed and len(atts) > 1:
            # all same type — old behavior
            base, ext = os.path.splitext(fname)
            fname = f"{base}-{j+1}{ext}"
        elif has_mixed and same_type_total > 1:
            # mixed email, multiple of this type
            base, ext = os.path.splitext(fname)
            fname = f"{base}-{type_count[actual_type]}{ext}"
        out = make_unique_path(pdfs_dir, fname)
        with open(out, "wb") as f:
            f.write(data)
        ok, info = validate_pdf_header(out)
        # v5.3: rename_by_ocr (Step 7) overwrites the filename with the
        # LLM-derived vendor, so the v5.2 pdftotext pre-extraction of merchant
        # here was dead work. Removed. In --no-llm mode, files keep the
        # email-derived merchant name as a best-effort fallback.
        print(f"  {'✅' if ok else '⚠️'} {os.path.basename(out)} ({len(data)//1024}KB)", file=log)
        rec = {
            "path": out, "valid": ok, "info": info,
            "subject": entry.get("subject"), "method": "ATTACHMENT",
            "merchant": merchant, "date": date_str, "doc_type": actual_type,
            "message_id": msg_id,
            "attachment_part_id": att.get("attachmentId"),
            "internal_date": entry.get("internal_date"),
            # v5.7 Unit 2: sender fields consumed by rename_by_ocr IGNORED
            # branch and the learned_exclusions CTA in write_report_md.
            "sender": entry.get("sender", ""),
            "sender_email": entry.get("sender_email", ""),
        }
        (downloaded if ok else failed).append(rec)
    return downloaded, failed


def download_zip(client, entry, pdfs_dir, log):
    msg_id = entry["message_id"]
    msg = client.get_full_message(msg_id)
    zips = find_zip_atts(msg["payload"])
    if not zips:
        return [], [{"subject": entry.get("subject"), "reason": "no ZIP attachment"}]
    downloaded, failed = [], []
    merchant = pick_merchant(entry)
    date_str = pick_date(entry)
    for zfn, zid in zips:
        zdata = client.get_attachment_bytes(msg_id, zid)
        with tempfile.TemporaryDirectory() as td:
            zpath = os.path.join(td, zfn)
            with open(zpath, "wb") as f:
                f.write(zdata)
            edir = os.path.join(td, "extracted")
            os.makedirs(edir, exist_ok=True)
            pdfs = extract_pdfs_from_zip(zpath, edir)
            if not pdfs:
                failed.append({"subject": entry.get("subject"), "reason": "ZIP contains no PDF"})
                continue
            for pdf in pdfs:
                fname = generate_filename(date_str, merchant, entry["doc_type"])
                out = make_unique_path(pdfs_dir, fname)
                shutil.copy(pdf, out)
                ok, info = validate_pdf_header(out)
                print(f"  {'✅' if ok else '⚠️'} {os.path.basename(out)} (from ZIP)", file=log)
                rec = {
                    "path": out, "valid": ok, "info": info,
                    "subject": entry.get("subject"), "method": "ATTACHMENT_ZIP",
                    "merchant": merchant, "date": date_str, "doc_type": entry["doc_type"],
                    "message_id": msg_id,
                    "attachment_part_id": f"{zid}:{os.path.basename(pdf)}",
                    "internal_date": entry.get("internal_date"),
                    # v5.7 Unit 2: sender fields for IGNORED rename + CTA
                    "sender": entry.get("sender", ""),
                    "sender_email": entry.get("sender_email", ""),
                }
                (downloaded if ok else failed).append(rec)
    return downloaded, failed


def download_link(entry, pdfs_dir, log, known_paths=None):
    url = entry.get("download_url")
    if not url:
        return [], [{"subject": entry.get("subject"), "reason": f"{entry['method']} no URL"}]
    # Resolve two-step URL markers
    if isinstance(url, str):
        if url.startswith("BAIWANG_SHORT:"):
            resolved = resolve_baiwang_short_url(url.replace("BAIWANG_SHORT:", ""))
            if not resolved:
                return [], [{"subject": entry.get("subject"), "reason": "failed to resolve baiwang short link", "url": url}]
            url = resolved
        elif url.startswith("BAIWANG_BWFP:"):
            resolved = resolve_baiwang_bwfp_short_url(url.replace("BAIWANG_BWFP:", ""))
            if not resolved:
                return [], [{"subject": entry.get("subject"), "reason": "failed to resolve bwfp short link", "url": url}]
            url = resolved
        elif url.startswith("NUONUO_SHORT:"):
            resolved = resolve_nuonuo_short_url(url.replace("NUONUO_SHORT:", ""))
            if not resolved:
                return [], [{"subject": entry.get("subject"), "reason": "failed to resolve nuonuo short link", "url": url}]
            url = resolved
        elif url.startswith("BWJF_SHORT:"):
            resolved = resolve_bwjf_short_url(url.replace("BWJF_SHORT:", ""))
            if not resolved:
                return [], [{"subject": entry.get("subject"), "reason": "failed to resolve bwjf short link", "url": url}]
            url = resolved
        elif url.startswith("KERUYUN_SHORT:"):
            resolved = resolve_keruyun_short_url(url.replace("KERUYUN_SHORT:", ""))
            if not resolved:
                return [], [{"subject": entry.get("subject"), "reason": "failed to resolve keruyun short link", "url": url}]
            url = resolved
    merchant = pick_merchant(entry)
    date_str = pick_date(entry)
    fname = generate_filename(date_str, merchant, entry["doc_type"])
    out = make_unique_path(pdfs_dir, fname)
    r = subprocess.run(
        ["curl", "-sL", "--max-time", "60", "-o", out, url],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return [], [{"subject": entry.get("subject"), "reason": f"curl failed: {r.stderr[:150]}", "url": url}]
    ok, info = validate_pdf_header(out)
    # In-run dedup: if byte-identical to a file we already downloaded this run,
    # drop the new copy. Scope must be this-run-only — scanning the whole pdfs_dir
    # would let stale files from previous runs silently swallow new downloads,
    # which historically hid ~20% of a quarter's LINK_BAIWANG invoices.
    if ok and known_paths:
        with open(out, "rb") as f:
            new_hash = hashlib.md5(f.read()).hexdigest()
        for ex_path in known_paths:
            if ex_path == out or not os.path.exists(ex_path):
                continue
            if os.path.getsize(ex_path) != os.path.getsize(out):
                continue
            with open(ex_path, "rb") as f:
                if hashlib.md5(f.read()).hexdigest() == new_hash:
                    os.remove(out)
                    print(f"  ♻️  duplicate of {os.path.basename(ex_path)}, removed {os.path.basename(out)}", file=log)
                    return [], []
    # v5.3: rename_by_ocr (Step 7) owns final naming. v5.2's pdftotext pre-extraction
    # removed — see download_attachment comment.
    print(f"  {'✅' if ok else '⚠️'} {os.path.basename(out)} ({entry['method']})", file=log)
    url_key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12] if isinstance(url, str) else "link"
    rec = {
        "path": out, "valid": ok, "info": info,
        "subject": entry.get("subject"), "method": entry["method"],
        "merchant": merchant, "date": date_str, "doc_type": entry["doc_type"],
        "url": url,
        "message_id": entry.get("message_id", ""),
        "attachment_part_id": f"url:{url_key}",
        "internal_date": entry.get("internal_date"),
        # v5.7 Unit 2: sender fields for IGNORED rename + CTA
        "sender": entry.get("sender", ""),
        "sender_email": entry.get("sender_email", ""),
    }
    return ([rec], []) if ok else ([], [{**rec, "reason": info}])


# ─── v5.3 report ───────────────────────────────────────────────────────────
# (v5.2 pair_folios_with_invoices + write_report removed in v5.3 — replaced by
#  do_all_matching in postprocess and write_report_md below.)

def write_report_md(
    path, *,
    downloaded_all, failed, skipped,
    matching_result,
    date_range,
    iteration: int,
    supplemental: bool,
    aggregation=None,
    out_of_range_items=None,   # v5.5 — skipped cross-quarter items
    ignored_records=None,      # v5.7 Unit 4 — IGNORED records for §已忽略 + CTA
):
    """Emit 下载报告.md reflecting v5.3 matching (P1/P2/P3 + ride-hailing + unparsed)
    and supplemental loop context.

    If ``aggregation`` is provided, a ``## 💰 金额汇总`` block is inserted
    before ``## 📊 摘要``. Legacy callers that pass None skip that block.
    """
    now = datetime.datetime.now(CST)
    valid = [d for d in downloaded_all if d.get("valid")]

    def _cat_count(cat):
        return sum(1 for d in valid if d.get("category") == cat)

    lines = []
    lines.append("# Gmail 发票下载报告\n")
    lines.append(f"**日期范围**：{date_range[0]} → {date_range[1]}  ")
    lines.append(f"**生成时间**：{now.strftime('%Y-%m-%d %H:%M:%S')} CST  ")
    lines.append(f"**轮次**：iteration={iteration}  "
                 f"{'(supplemental 补搜)' if supplemental else '(initial)'}  ")
    lines.append(f"**有效 PDF**：{len(valid)} 份  ")
    lines.append("**匹配规则**：")
    lines.append("- 酒店：P1 remark==confirmationNo → P2 日期+金额 → P3 同日兜底（v5.2 回退）")
    lines.append("- 网约车：金额 0.01 容差 + 文件名序号消歧")
    lines.append("- 餐饮发票不自动匹配（开票日 ≠ 就餐日）")
    lines.append("")

    # ── 💰 金额汇总 (aggregated totals) ──
    if aggregation is not None:
        lines.append("## 💰 金额汇总\n")
        lines.append("| 类别 | 数量 | 小计 |")
        lines.append("|------|------|------|")
        subtotals = aggregation["subtotals"]
        for cat in sorted(subtotals.keys(),
                          key=lambda c: CATEGORY_ORDER.get(c, 50)):
            # Count every row in this category — matches stdout (per_cat_counts
            # in print_openclaw_summary) and the CSV detail section. Using the
            # subtotal-only filter here would desync MD with the other two
            # writers for categories that contain an amount=None row.
            count = sum(1 for r in aggregation["rows"] if r.category == cat)
            lines.append(
                f"| {CATEGORY_LABELS.get(cat, cat)} | {count} | "
                f"¥{subtotals[cat]:.2f} |"
            )
        lines.append(
            f"| **总计** | {aggregation['voucher_count']} | "
            f"**¥{aggregation['grand_total']:.2f}** |"
        )
        # R13 footnote — condition must match the OpenClaw message footnote
        # (low_conf.count > 0) so CSV/MD/stdout stay in sync.
        low = aggregation["low_conf"]
        if low["count"] > 0:
            lines.append("")
            lines.append(
                f"† 其中 {low['count']} 项金额存疑（可信度=low，合计 "
                f"¥{low['amount']:.2f}），见末尾「⚠️ 需人工核查」区"
            )
        lines.append("")

    # ── Summary table ──
    lines.append("## 📊 摘要\n")
    lines.append("| 类别 | 数量 |")
    lines.append("|------|------|")
    for cat in ["HOTEL_FOLIO", "HOTEL_INVOICE", "MEAL", "RIDEHAILING_INVOICE",
                "RIDEHAILING_RECEIPT", "TRAIN", "TAXI", "MOBILE", "TOLLS",
                "UNKNOWN", "UNPARSED"]:
        n = _cat_count(cat)
        if n == 0:
            continue
        lines.append(f"| {CATEGORY_LABELS.get(cat, cat)} | {n} |")
    lines.append(f"| ❌ 下载失败 | {len(failed)} |")
    lines.append(f"| ⏭️  跳过 (MANUAL/IGNORE) | {len(skipped)} |")
    lines.append("")

    # ── Hotel matching ──
    hotel = matching_result.get("hotel", {})
    matched = hotel.get("matched", [])
    if matched or hotel.get("unmatched_invoices") or hotel.get("unmatched_folios"):
        lines.append("## 🏨 酒店入住配对\n")
        # v5.5: split P1/P2 (trusted) from P3 (date-only fallback) so P3 can
        # surface the folio OCR arrival/departure dates reviewers care about.
        # Filename-derived dates on P3 rows may be email-internalDate-based
        # and drift weeks from the actual checkout.
        primary = [m for m in matched
                   if m.get("match_type") != "date_only (v5.2 fallback)"]
        fallback = [m for m in matched
                    if m.get("match_type") == "date_only (v5.2 fallback)"]
        if primary:
            lines.append("| 退房日 | 销售方 | 匹配方式 | 水单 | 发票 |")
            lines.append("|--------|--------|----------|:----:|:----:|")
            for m in primary:
                inv_rec = (m["invoice"].get("_record") or {})
                fol_rec = (m["folio"].get("_record") or {})
                inv_date = m["invoice"].get("transactionDate") or inv_rec.get("date", "") or "?"
                vendor = (inv_rec.get("ocr") or {}).get("vendorName") or inv_rec.get("merchant", "?")
                match_type = m["match_type"]
                inv_name = os.path.basename(inv_rec.get("path", ""))
                fol_name = os.path.basename(fol_rec.get("path", ""))
                type_label = {
                    "remark": "P1 (remark)",
                    "date_amount": "P2 (日期+金额)",
                }.get(match_type, match_type)
                lines.append(f"| {inv_date} | {vendor} | {type_label} | `{fol_name}` | `{inv_name}` |")
            lines.append("")
        if fallback:
            lines.append("### P3 同日兜底匹配（低可信度）\n")
            lines.append("| 销售方 | 匹配方式 | 入住 / 退房 (OCR) | 水单 | 发票 |")
            lines.append("|--------|----------|-------------------|:----:|:----:|")
            for m in fallback:
                inv_rec = (m["invoice"].get("_record") or {})
                fol_rec = (m["folio"].get("_record") or {})
                vendor = (inv_rec.get("ocr") or {}).get("vendorName") or inv_rec.get("merchant", "?")
                arrival = m.get("folio_arrival_date") or "?"
                departure = m.get("folio_departure_date") or "?"
                inv_name = os.path.basename(inv_rec.get("path", ""))
                fol_name = os.path.basename(fol_rec.get("path", ""))
                lines.append(
                    f"| {vendor} | P3 (仅日期)⚠️ | {arrival} / {departure} "
                    f"| `{fol_name}` | `{inv_name}` |"
                )
            lines.append("")
        if hotel.get("unmatched_invoices"):
            lines.append(f"### ⚠️ 无水单的酒店发票（{len(hotel['unmatched_invoices'])} 张）\n")
            for inv in hotel["unmatched_invoices"]:
                rec = inv.get("_record") or {}
                ocr = rec.get("ocr") or {}
                d = ocr.get("transactionDate") or rec.get("date", "?")
                v = ocr.get("vendorName") or rec.get("merchant", "?")
                a = ocr.get("transactionAmount")
                amt = f"¥{a:.2f}" if a else "?"
                lines.append(f"- [{d}] **{v}** {amt} `{os.path.basename(rec.get('path',''))}`")
            lines.append("")
        if hotel.get("unmatched_folios"):
            lines.append(f"### ⚠️ 无发票的水单（{len(hotel['unmatched_folios'])} 份）\n")
            for fol in hotel["unmatched_folios"]:
                rec = fol.get("_record") or {}
                ocr = rec.get("ocr") or {}
                d = ocr.get("checkOutDate") or ocr.get("departureDate") or rec.get("date", "?")
                v = ocr.get("hotelName") or ocr.get("vendorName") or rec.get("merchant", "?")
                a = ocr.get("balance") or ocr.get("transactionAmount")
                amt = f"¥{a:.2f}" if a else "?"
                lines.append(f"- [{d}] **{v}** {amt} `{os.path.basename(rec.get('path',''))}`")
            lines.append("")

    # ── Ride-hailing matching ──
    rh = matching_result.get("ridehailing", {})
    rh_matched = rh.get("matched", [])
    if rh_matched or rh.get("unmatched_invoices") or rh.get("unmatched_receipts"):
        lines.append("## 🚖 网约车配对\n")
        if rh_matched:
            lines.append("| 日期 | 销售方 | 金额 | 发票 | 行程单 |")
            lines.append("|------|--------|------|:----:|:------:|")
            for m in rh_matched:
                inv_rec = (m["invoice"].get("_record") or {})
                rec_rec = (m["receipt"].get("_record") or {})
                d = m["invoice"].get("transactionDate") or inv_rec.get("date", "?")
                v = (inv_rec.get("ocr") or {}).get("vendorName") or inv_rec.get("merchant", "?")
                a = m["invoice"].get("transactionAmount")
                amt = f"¥{a:.2f}" if a else "?"
                inv_name = os.path.basename(inv_rec.get("path", ""))
                rec_name = os.path.basename(rec_rec.get("path", ""))
                lines.append(f"| {d} | {v} | {amt} | `{inv_name}` | `{rec_name}` |")
            lines.append("")
        if rh.get("unmatched_invoices"):
            lines.append(f"### ⚠️ 无行程单的网约车发票（{len(rh['unmatched_invoices'])} 张）\n")
            for inv in rh["unmatched_invoices"]:
                rec = inv.get("_record") or {}
                lines.append(f"- `{os.path.basename(rec.get('path',''))}`")
            lines.append("")
        if rh.get("unmatched_receipts"):
            lines.append(f"### ⚠️ 无发票的行程单（{len(rh['unmatched_receipts'])} 份）\n")
            for r in rh["unmatched_receipts"]:
                rec = r.get("_record") or {}
                lines.append(f"- `{os.path.basename(rec.get('path',''))}`")
            lines.append("")

    # ── Meals (always shown if present) ──
    meals = matching_result.get("meal", [])
    if meals:
        lines.append(f"## 🍽️ 餐饮发票（{len(meals)} 张，按商户聚合）\n")
        lines.append("餐饮发票不自动关联酒店入住（开票日可能合并多天就餐）。\n")
        by_m = defaultdict(list)
        for inv in meals:
            v = (inv.get("ocr") or {}).get("vendorName") or inv.get("merchant", "?")
            by_m[v].append(inv)
        for m, invs in sorted(by_m.items(), key=lambda x: -len(x[1])):
            lines.append(f"### {m} × {len(invs)}")
            for inv in sorted(invs, key=lambda x: (x.get("ocr") or {}).get("transactionDate", "") or ""):
                ocr = inv.get("ocr") or {}
                d = ocr.get("transactionDate") or "?"
                a = ocr.get("transactionAmount")
                amt = f"¥{a:.2f}" if a else ""
                lines.append(f"- [{d}] {amt} `{os.path.basename(inv.get('path',''))}`")
        lines.append("")

    # ── Trains / Taxi / Mobile / Tolls / Unknown ──
    for bucket, label in [
        ("train", "🚄 火车票"),
        ("taxi", "🚕 出租车发票"),
        ("mobile", "📱 话费发票"),
        ("tolls", "🛣️ 通行费"),
        ("unknown", "📄 其他发票"),
    ]:
        items = matching_result.get(bucket, [])
        if not items:
            continue
        lines.append(f"## {label}（{len(items)} 张）\n")
        for inv in items:
            ocr = inv.get("ocr") or {}
            d = ocr.get("transactionDate") or inv.get("date", "?")
            v = ocr.get("vendorName") or inv.get("merchant", "?")
            a = ocr.get("transactionAmount")
            amt = f"¥{a:.2f}" if a else ""
            lines.append(f"- [{d}] **{v}** {amt} `{os.path.basename(inv.get('path',''))}`")
        lines.append("")

    # ── Unparsed (LLM failed) ──
    unparsed = matching_result.get("unparsed", [])
    if unparsed:
        lines.append(f"## ⚠️ 需人工核查（LLM OCR 失败，{len(unparsed)} 份）\n")
        for rec in unparsed:
            err = rec.get("error") or "unknown"
            lines.append(f"- `{os.path.basename(rec.get('path',''))}` — {err[:80]}")
        lines.append("")

    # ── v5.7 IGNORED 非报销票据 + learned_exclusions CTA ──
    if ignored_records:
        lines.append(f"## 📭 已忽略的非报销票据 ({len(ignored_records)})\n")
        lines.append(
            "以下票据被识别为非发票 / 非水单 / 非行程单，已自动过滤，"
            "不进入 CSV / 打包 zip。文件仍保留在 PDFs 目录下以 `IGNORED_` "
            "前缀标记，可人工核查。\n"
        )
        # Per-record listing: show sender_email + amount.
        # LLM-returned transactionAmount is unvalidated JSON — it can come
        # back as a string like "120.00" or garbage. Coerce to float the
        # same way postprocess._to_float does; unconvertible → "金额未识别"
        # (same fallback as missing).
        for rec in ignored_records:
            sender_email = rec.get("sender_email") or ""
            label = sender_email if sender_email else "未知发件人"
            ocr = rec.get("ocr") or {}
            raw_amount = ocr.get("transactionAmount")
            try:
                amount = float(raw_amount) if raw_amount not in (None, "") else None
            except (TypeError, ValueError):
                amount = None
            if amount is not None:
                sym = currency_symbol(ocr.get("currency"))
                lines.append(f"- {label}：{sym}{amount:.2f}")
            else:
                lines.append(f"- {label}：金额未识别")
        lines.append("")

        # CTA: aggregate by full email domain, render -from:<domain> lines.
        # Intentionally different from Unit 3's filename `domain_label` (which
        # is split-on-first-dot, e.g. "termius" for a friendly tag). Here we
        # need the whole domain because Gmail's `-from:termius.com` operator
        # requires a full domain for suffix matching.
        domain_counts = {}
        for rec in ignored_records:
            sender_email = rec.get("sender_email") or ""
            if "@" not in sender_email:
                continue
            domain = sender_email.split("@", 1)[-1]
            if not domain:
                continue
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

        if domain_counts:
            lines.append(
                "💡 下次避免 OCR 成本：可把这些 sender 加到 "
                "`learned_exclusions.json`\n"
            )
            lines.append("```")
            for domain in sorted(domain_counts.keys()):
                n = domain_counts[domain]
                lines.append(f"-from:{domain}       # 已过滤 {n} 次")
            lines.append("```")
            lines.append("")

    # ── v5.5 跨季度边界项（无需补搜） ──
    if out_of_range_items:
        lines.append(
            f"## ℹ️ 跨季度边界项（无需补搜，{len(out_of_range_items)} 项）\n"
        )
        lines.append(
            f"以下项目的业务日期不在本批次时间范围"
            f"（{date_range[0]} ~ {date_range[1]}）内，已跳过自动补搜。"
            f"如需一并报销，请单独跑对应季度的批次。\n"
        )
        for orr in out_of_range_items:
            needed_for = orr.get("needed_for", "?")
            bdate = orr.get("business_date", "?")
            merchant = orr.get("expected_merchant") or ""
            suffix = f" — {merchant}" if merchant else ""
            lines.append(f"- `{needed_for}`（业务日期 {bdate}）{suffix}")
        lines.append("")

    # ── 补搜建议 ──
    total_missing = (
        len(hotel.get("unmatched_invoices", []))
        + len(hotel.get("unmatched_folios", []))
        + len(rh.get("unmatched_invoices", []))
        + len(rh.get("unmatched_receipts", []))
    )
    if total_missing:
        lines.append("## 🔍 补搜建议\n")
        lines.append(f"共 {total_missing} 项未匹配。详见 `missing.json`。\n")
        lines.append("Agent 按 Loop Playbook 决策：")
        lines.append("```")
        lines.append("if status == 'needs_retry': 跑 supplemental 调用")
        lines.append("if status == 'converged' or 'max_iterations_reached': 交付")
        lines.append("```")
        lines.append("")

    # ── Failed downloads (non-OCR) ──
    if failed:
        lines.append(f"## ❌ 下载失败（{len(failed)} 项）\n")
        for f in failed:
            lines.append(f"- {f.get('subject', '?')[:70]}: {f.get('reason', '?')}")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ─── Main ──────────────────────────────────────────────────────────────────

def _previous_convergence_hash(output_dir):
    """Read previous iteration's convergence_hash from missing.json if present."""
    path = os.path.join(output_dir, "missing.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("convergence_hash")
    except (json.JSONDecodeError, OSError):
        return None


def _previous_iteration(output_dir):
    """Read previous iteration number from missing.json, default 0."""
    path = os.path.join(output_dir, "missing.json")
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(json.load(f).get("iteration", 0))
    except (json.JSONDecodeError, OSError, ValueError):
        return 0


def _collect_this_run_pdf_paths(downloaded_all) -> set:
    """Return the set of PDF paths produced by this run (v5.7.1 fix).

    Simpler than iterating matching_result: every record rename_by_ocr
    touched lives in ``downloaded_all`` with a current ``path`` field
    (post-rename on happy/UNPARSED/IGNORED paths). Dedup losers that
    were physically unlinked drop off os.walk naturally — no need to
    whitelist them here. IGNORED paths are in the set too; the
    ``IGNORED_`` prefix filter in ``zip_output`` handles their exclusion
    so the record listing stays authoritative.

    Anything in ``output_dir/pdfs/`` not in this set is a cross-batch
    leftover (previous-run residue) and will be kept on disk for audit
    but not shipped in the zip.
    """
    paths: set = set()
    for rec in downloaded_all or []:
        if not isinstance(rec, dict):
            continue
        p = rec.get("path")
        if p:
            paths.add(p)
    return paths


def _count_leftover_pdfs(pdfs_dir: str, this_run_paths: set) -> int:
    """Count PDFs in pdfs_dir that are not part of this run's whitelist.

    Used to print a user-visible info line about cross-batch residue.
    IGNORED_* and UNPARSED_* are still "this run" if they were produced
    this run; leftovers are files neither in this_run_paths nor prefixed
    for exclusion. Treat IGNORED_-prefixed files as excluded-by-design
    (not counted as leftovers — they're current-run but intentionally
    not in the whitelist).
    """
    if not os.path.isdir(pdfs_dir):
        return 0
    whitelist_abs = {os.path.abspath(p) for p in this_run_paths}
    leftover = 0
    for fn in os.listdir(pdfs_dir):
        if not fn.lower().endswith(".pdf"):
            continue
        if fn.startswith("IGNORED_"):
            continue
        fp = os.path.abspath(os.path.join(pdfs_dir, fn))
        if fp not in whitelist_abs:
            leftover += 1
    return leftover


def _inspect_existing_output_dir(output_dir: str) -> dict:
    """Inspect output_dir before a fresh run to detect prior-batch content.

    v5.7.2: feeds the "use a new folder" nudge. Returns:
      - pdf_count: number of .pdf files in output_dir/pdfs/ that are NOT
        IGNORED_* (IGNORED files are v5.7 classifier artifacts that live
        alongside the batch by design, not a signal of prior-run residue).
      - has_state: True iff any of step4_downloaded.json / missing.json /
        下载报告.md / 发票汇总.csv exists (strong prior-run signatures).
      - is_empty: True when both pdf_count == 0 and has_state is False —
        safe to proceed without a warning.

    A non-existent output_dir is treated as empty (fresh-run ideal).
    """
    result = {"pdf_count": 0, "has_state": False, "is_empty": True}
    if not os.path.isdir(output_dir):
        return result

    pdfs_dir = os.path.join(output_dir, "pdfs")
    if os.path.isdir(pdfs_dir):
        for fn in os.listdir(pdfs_dir):
            if not fn.lower().endswith(".pdf"):
                continue
            if fn.startswith("IGNORED_"):
                continue
            result["pdf_count"] += 1

    state_signatures = (
        "step4_downloaded.json",
        "missing.json",
        "下载报告.md",
        "发票汇总.csv",
    )
    for sig in state_signatures:
        if os.path.exists(os.path.join(output_dir, sig)):
            result["has_state"] = True
            break

    result["is_empty"] = (result["pdf_count"] == 0 and not result["has_state"])
    return result


def _run_postprocess_only(
    *,
    output_dir: str,
    use_llm: bool,
    iteration_cap: int,
    run_start_date,
    run_end_date,
) -> int:
    """Re-run Step 6-10 against an existing output dir.

    Reads pdfs/ directly (not step4_downloaded.json — that is stale once
    probe rescues land). Produces fresh three deliverables + zip.

    Returns an exit code (does not sys.exit).
    """
    if not os.path.isdir(output_dir):
        print(
            f"\nREMEDIATION: --output={output_dir!r} does not exist. "
            f"Pass a directory that holds an existing pdfs/ subdirectory.",
            file=sys.stderr,
        )
        return EXIT_UNKNOWN

    pdfs_dir = os.path.join(output_dir, "pdfs")
    os.makedirs(pdfs_dir, exist_ok=True)

    # Open run.log in append mode so we chain onto the original fetch's log
    log_path = os.path.join(output_dir, "run.log")
    log = open(log_path, "a")
    import atexit as _atexit
    _atexit.register(lambda f=log: (f.flush(), f.close()) if not f.closed else None)

    def say(msg):
        print(msg)
        print(msg, file=log, flush=True)

    iteration = _previous_iteration(output_dir) + 1
    say("=" * 70)
    say(f"Gmail Invoice Downloader — POSTPROCESS-ONLY iteration={iteration}")
    say(f"Run started @ {datetime.datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST")
    say("=" * 70)
    say(f"LLM provider: {os.environ.get('LLM_PROVIDER', 'bedrock')}"
        + (" [disabled]" if not use_llm else ""))

    # Build synthetic records from pdfs/ — Step 6 forward works off `path`
    # + `valid` + optional `internal_date`.
    records = []
    for fname in sorted(os.listdir(pdfs_dir)):
        if not fname.lower().endswith(".pdf"):
            continue
        records.append({
            "path": os.path.join(pdfs_dir, fname),
            "valid": True,
            "method": "POSTPROCESS_ONLY",
            "merchant": None,
            "date": None,
            "doc_type": "UNKNOWN",
            "message_id": fname,
            "subject": fname,
            "internal_date": None,
        })
    say(f"Found {len(records)} PDF(s) in {pdfs_dir}")

    # --- Step 6: OCR ---
    if use_llm and records:
        say("\n--- Step 6: LLM OCR + classify + plausibility ---")
        t0 = time.time()
        try:
            # max_workers intentionally omitted — reads LLM_OCR_CONCURRENCY env
            # var, defaults to 5. Set LLM_OCR_CONCURRENCY=2 for Anthropic tier-1.
            analyses = analyze_pdf_batch(
                records,
                use_llm=use_llm,
                logger=log,
            )
        except (LLMAuthError, LLMConfigError) as e:
            say(f"❌ LLM config error: {e}")
            print(f"\nREMEDIATION: {e}", file=sys.stderr)
            log.close()
            return EXIT_LLM_CONFIG
        say(f"  OCR done for {len(analyses)} files in {time.time()-t0:.1f}s")

        # --- Step 7: rename by OCR ---
        say("\n--- Step 7: rename files by OCR ---")
        renamed = 0
        for rec in records:
            analysis = analyses.get(rec.get("path")) or {}
            old_name = os.path.basename(rec.get("path", ""))
            rename_by_ocr(rec, analysis, pdfs_dir)
            new_name = os.path.basename(rec.get("path", ""))
            if new_name != old_name:
                renamed += 1
        say(f"  renamed {renamed}/{len(records)} files")
    else:
        say("\n--- Step 6+7: skipped (LLM disabled or no PDFs) ---")
        for rec in records:
            rec["category"] = "UNPARSED"
            rec["ocr"] = None

    # --- Step 8: matching ---
    say("\n--- Step 8: matching ---")
    matching_result = do_all_matching(records)

    # --- Step 8.5: aggregation ---
    dedup_removed_ids = {id(r) for r in matching_result.get("dedup_removed", [])}
    valid_records = [
        d for d in records
        if d.get("valid") and id(d) not in dedup_removed_ids
    ]
    # v5.7 Unit 3: same IGNORED split as main().
    ignored_records = [d for d in valid_records if d.get("category") == "IGNORED"]
    reimbursable_records = [d for d in valid_records if d.get("category") != "IGNORED"]
    aggregation = build_aggregation(matching_result, reimbursable_records)

    # --- Step 9c: missing.json (computed first so report can render
    #     out_of_range_items[] subsection) ---
    missing_path = os.path.join(output_dir, "missing.json")
    prev_hash = _previous_convergence_hash(output_dir)
    missing_payload = write_missing_json(
        missing_path,
        batch_dir=output_dir,
        iteration=iteration,
        iteration_cap=iteration_cap,
        matching_result=matching_result,
        unparsed_records=matching_result.get("unparsed", []),
        previous_convergence_hash=prev_hash,
        run_start_date=run_start_date,    # v5.5 — cross-quarter routing
        run_end_date=run_end_date,
    )

    # --- Step 9a: 下载报告.md ---
    report_path = os.path.join(output_dir, "下载报告.md")
    write_report_md(
        report_path,
        downloaded_all=records,
        failed=[],
        skipped=[],
        matching_result=matching_result,
        date_range=(run_start_date or "?", run_end_date or "?"),
        iteration=iteration,
        supplemental=False,
        aggregation=aggregation,
        out_of_range_items=missing_payload.get("out_of_range_items", []),
        ignored_records=ignored_records,
    )
    say(f"\n✅ Report:   {report_path}")

    # --- Step 9b: 发票汇总.csv ---
    csv_path = os.path.join(output_dir, "发票汇总.csv")
    n_csv = write_summary_csv(csv_path, aggregation)
    say(f"✅ CSV:      {csv_path}  ({n_csv} rows)")
    say(f"✅ missing.json: {missing_path}  "
        f"(status={missing_payload['status']}, "
        f"next={missing_payload['recommended_next_action']}, "
        f"items={len(missing_payload['items'])})")

    # --- Step 10: zip ---
    # v5.7.1: same whitelist + leftover-info pattern as main(). In postprocess-
    # only flow `records` is the in-memory list of this run's PDFs; prior-run
    # residue (files already in pdfs_dir before we started) falls through.
    this_run_paths = _collect_this_run_pdf_paths(records)
    leftover_count = _count_leftover_pdfs(pdfs_dir, this_run_paths)
    if leftover_count > 0:
        say(
            f"ℹ️  跨批次残留：pdfs/ 目录里有 {leftover_count} 份 PDF 未出现在本次 run "
            f"的记录里，未打包进本次 zip（仍保留在磁盘上供审计）。"
        )
    zip_path = None
    try:
        zip_path = zip_output(output_dir, include_pdf_paths=this_run_paths)
        say(f"✅ Zip:      {zip_path}")
    except RuntimeError as e:
        say(f"⚠️  zip skipped: {e}")

    # --- Step 11: OpenClaw chat summary (stdout + run.log). MUST be called
    #     before log.close() — writer=say dual-writes to the still-open log.
    #     Mirrors main()'s Step 11 call so auto-probe rescues don't silently
    #     drop the v5.4 aggregated chat summary.
    say("")
    print_openclaw_summary(
        aggregation,
        output_dir=output_dir,
        zip_path=zip_path,
        csv_path=csv_path,
        md_path=report_path,
        log_path=log_path,
        missing_status=missing_payload["recommended_next_action"],
        date_range=(run_start_date or "?", run_end_date or "?"),
        writer=say,
        ignored_count=len(ignored_records),
    )

    # --- Exit semantics ---
    if not records:
        print(
            "\nREMEDIATION: no PDFs found in pdfs/. Nothing to postprocess. "
            "Run a normal Gmail fetch first or add PDFs manually.",
            file=sys.stderr,
        )
        log.close()
        return EXIT_PARTIAL

    hotel = matching_result.get("hotel", {})
    rh = matching_result.get("ridehailing", {})
    unparsed = matching_result.get("unparsed", [])
    if (unparsed or hotel.get("unmatched_invoices") or hotel.get("unmatched_folios")
            or rh.get("unmatched_invoices") or rh.get("unmatched_receipts")):
        print(
            "\nREMEDIATION: partial result — inspect missing.json for items "
            "needing follow-up (run_supplemental, probe, or user action).",
            file=sys.stderr,
        )
        log.close()
        return EXIT_PARTIAL

    log.close()
    return EXIT_OK


def main():
    ap = argparse.ArgumentParser(
        description="Gmail Invoice Downloader — search, download, OCR, match, report.",
    )
    # --start / --end are required for normal runs. In --postprocess-only mode
    # (Step 6-10 only, no Gmail) the date range is irrelevant; we check + enforce
    # the non-postprocess-only case manually below so argparse doesn't reject
    # the agent's probe-rescue invocation.
    ap.add_argument("--start", required=False, help="Gmail date: 2026/01/01")
    ap.add_argument("--end", required=False, help="Gmail date (exclusive): 2026/05/01")
    ap.add_argument("--output", required=True, help="Output directory")
    ap.add_argument("--creds", default=DEFAULT_CREDS)
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    ap.add_argument("--max-results", type=int, default=1000)

    # v5.3 supplemental / loop flags
    ap.add_argument("--supplemental", action="store_true",
                    help="Merge new downloads into existing step4_downloaded.json "
                         "instead of overwriting. Used by Agent loop.")
    ap.add_argument("--iteration", type=int, default=None,
                    help="Loop iteration number (auto-incremented if omitted).")
    ap.add_argument("--iteration-cap", type=int, default=3,
                    help="Max loop iterations before status=max_iterations_reached.")
    ap.add_argument("--query", default=None,
                    help="Override default INVOICE_KEYWORDS (supplemental narrow search).")
    ap.add_argument(
        "--postprocess-only",
        action="store_true",
        help=(
            "Skip Gmail search/download (Step 1-5). Re-run OCR + matching + "
            "deliverables (Step 6-10) against existing <output>/pdfs/. Use "
            "after curling a rescued PDF into pdfs/ (see SKILL.md auto-probe)."
        ),
    )

    # v5.3 LLM provider flags
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip LLM OCR entirely. All valid PDFs classified as UNPARSED, "
                         "no vendor/amount/date extraction. For cost-sensitive dry runs.")
    ap.add_argument(
        "--llm-provider",
        choices=[
            "bedrock", "anthropic", "anthropic-compatible",
            "openai", "openai-compatible", "none",
        ],
        default=None,
        help="Override LLM_PROVIDER env var.",
    )
    # --ascii-names was declared in v5.3 but never implemented; removed to keep
    # the CLI contract honest. If you need pinyin filenames later, pipe the CSV
    # through a downstream renamer, or re-add with real implementation.

    # v5.3 preflight
    ap.add_argument("--skip-preflight", action="store_true",
                    help="Skip doctor.py preflight checks (not recommended).")

    args = ap.parse_args()

    # Enforce --start / --end outside --postprocess-only (they were declared
    # non-required so the postprocess-only path stays ergonomic, but the Gmail
    # flow genuinely requires them).
    if not args.postprocess_only and (not args.start or not args.end):
        print(
            "\nREMEDIATION: --start and --end are required for normal runs. "
            "Pass --postprocess-only to skip Gmail and re-run Step 6-10 against "
            "existing <output>/pdfs/.",
            file=sys.stderr,
        )
        sys.exit(EXIT_UNKNOWN)

    # --- --postprocess-only: skip Gmail (Step 1-5), just redo Step 6-10 ---
    if args.postprocess_only:
        if args.iteration is not None:
            print(
                "\nREMEDIATION: --iteration is not supported with "
                "--postprocess-only. The iteration number is auto-derived "
                "from the existing missing.json in --output. Drop --iteration.",
                file=sys.stderr,
            )
            sys.exit(EXIT_UNKNOWN)
        # Propagate --no-llm / --llm-provider to env so the singleton picks it up.
        if args.no_llm:
            os.environ["LLM_PROVIDER"] = "none"
        elif args.llm_provider:
            os.environ["LLM_PROVIDER"] = args.llm_provider
        sys.exit(_run_postprocess_only(
            output_dir=os.path.expanduser(args.output),
            use_llm=(os.environ.get("LLM_PROVIDER", "bedrock") != "none"),
            iteration_cap=args.iteration_cap,
            run_start_date=args.start,
            run_end_date=args.end,
        ))

    # --- Preflight ---
    if not args.skip_preflight:
        sys.path.insert(0, SCRIPT_DIR)
        from doctor import run_preflight
        # In supplemental mode, LLM config failures are tolerated (Agent may have
        # intentionally dropped creds); in initial mode, we want them to surface.
        pre_code = run_preflight(verbose=True)
        if pre_code != 0 and not args.supplemental:
            print("\nERROR: preflight failed. Fix the above or pass --skip-preflight.",
                  file=sys.stderr)
            sys.exit(EXIT_AUTH)
        print()  # blank line between preflight and main output

    # --- Propagate CLI → env for llm_client singleton ---
    if args.no_llm:
        os.environ["LLM_PROVIDER"] = "none"
    elif args.llm_provider:
        os.environ["LLM_PROVIDER"] = args.llm_provider
    use_llm = os.environ.get("LLM_PROVIDER", "bedrock") != "none"

    # --- Setup ---
    output_dir = os.path.expanduser(args.output)
    pdfs_dir = os.path.join(output_dir, "pdfs")
    os.makedirs(pdfs_dir, exist_ok=True)
    log_path = os.path.join(output_dir, "run.log")
    log = open(log_path, "a" if args.supplemental else "w")
    # Ensure the log flushes + closes on interpreter exit, even if main() bails
    # via an uncaught exception. Explicit log.close() calls below still work;
    # atexit becomes a no-op once a file is already closed.
    import atexit as _atexit
    _atexit.register(lambda f=log: (f.flush(), f.close()) if not f.closed else None)

    def say(msg):
        print(msg)
        print(msg, file=log, flush=True)

    # Determine iteration number
    if args.iteration is not None:
        iteration = args.iteration
    elif args.supplemental:
        iteration = _previous_iteration(output_dir) + 1
    else:
        iteration = 1

    say("=" * 70)
    mode = "SUPPLEMENTAL" if args.supplemental else "INITIAL"
    say(f"Gmail Invoice Downloader — {mode} iteration={iteration}")
    say(f"Run started @ {datetime.datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST")
    say("=" * 70)
    say(f"Date range: {args.start} → {args.end}")
    say(f"LLM provider: {os.environ.get('LLM_PROVIDER', 'bedrock')}"
        + (" [disabled]" if not use_llm else ""))

    # --- v5.7.2 output-dir preflight ---
    # Warn — but do not block — when INITIAL run reuses a dir that already
    # has prior-batch content. The v5.7.1 zip whitelist makes this safe for
    # correctness, but mixing batches in one dir is confusing. SUPPLEMENTAL
    # runs skip the warning because reusing the dir is by design. Agents
    # should construct a fresh --output path per SKILL.md § Agent First-Run
    # Procedure; this info line is the runtime safety net.
    if not args.supplemental:
        preflight = _inspect_existing_output_dir(output_dir)
        if not preflight["is_empty"]:
            signals = []
            if preflight["pdf_count"] > 0:
                signals.append(f"pdfs/ 有 {preflight['pdf_count']} 份前次批次的 PDF")
            if preflight["has_state"]:
                signals.append("存在之前的状态文件 (step4_downloaded.json / missing.json / 报告)")
            say(
                f"⚠️  output_dir 不是空的：{output_dir}\n"
                f"    " + "；".join(signals) + "。\n"
                f"    继续跑仍然安全（v5.7.1 zip 白名单已去重跨批次残留），但首次跑建议用新目录：\n"
                f"    --output ~/invoices/{{YYYY-QN}}-{{timestamp}}\n"
                f"    补搜请加 --supplemental flag。"
            )

    # --- Query ---
    exclusions = load_exclusions(SKILL_DIR)
    if args.query:
        query = f"after:{args.start} before:{args.end} ({args.query})"
        say(f"Custom query: {args.query}")
    else:
        query = build_query(args.start, args.end, exclusions)
        say(f"Exclusions: {len(exclusions)} rules (from learned_exclusions.json)")

    # --- Step 2: Search ---
    say("\n--- Step 2: Gmail search ---")
    t0 = time.time()
    try:
        client = GmailClient(args.creds, args.token)
        msg_refs = client.search(query, max_results=args.max_results)
    except GmailQuotaError as e:
        say(f"❌ Gmail quota exhausted: {e}")
        print(
            f"\nREMEDIATION: wait 60s (or honor Retry-After header) then rerun. "
            f"Consider --max-results=N to reduce load.",
            file=sys.stderr,
        )
        log.close()
        sys.exit(EXIT_GMAIL_QUOTA)
    except Exception as e:
        say(f"❌ Gmail search failed: {e}")
        print(
            f"\nREMEDIATION: run `python3 scripts/gmail-auth.py` to refresh token.json.",
            file=sys.stderr,
        )
        log.close()
        sys.exit(EXIT_AUTH)
    say(f"  {len(msg_refs)} messages matched in {time.time()-t0:.1f}s")

    # --- Step 3: Classify ---
    say("\n--- Step 3: Fetch + classify ---")
    t0 = time.time()
    classified = []
    for i, ref in enumerate(msg_refs):
        try:
            msg = client.get_full_message(ref["id"])
            c = classify_email(msg)
            c["message_id"] = ref["id"]
            c["internal_date"] = msg.get("internalDate")
            classified.append(c)
            if (i + 1) % 10 == 0:
                say(f"  {i+1}/{len(msg_refs)}")
        except Exception as e:
            say(f"  ⚠️ classify fail for {ref['id']}: {e}")
    say(f"  classified {len(classified)} in {time.time()-t0:.1f}s")

    with open(os.path.join(output_dir, "step3_classified.json"), "w") as f:
        json.dump(
            [{k: v for k, v in c.items() if k != "payload"} for c in classified],
            f, ensure_ascii=False, indent=2, default=str,
        )

    by_method = Counter(c["method"] for c in classified)
    by_type = Counter(c["doc_type"] for c in classified)
    say(f"  by method: {dict(by_method)}")
    say(f"  by doc_type: {dict(by_type)}")

    # --- Steps 4-5: Download + validate ---
    say("\n--- Steps 4-5: Download + validate ---")
    t0 = time.time()
    downloaded, failed, skipped = [], [], []
    for i, c in enumerate(classified):
        method = c["method"]
        if method in ("MANUAL", "IGNORE"):
            skipped.append({
                "subject": c.get("subject"), "sender": c.get("sender"),
                "doc_type": c["doc_type"], "method": method,
                "reason": c.get("ignore_reason", ""),
            })
            continue
        try:
            if method == "ATTACHMENT":
                d, fl = download_attachment(client, c, pdfs_dir, log)
            elif method == "ATTACHMENT_ZIP":
                d, fl = download_zip(client, c, pdfs_dir, log)
            elif method in ("LINK_FAPIAO_COM", "LINK_XFORCEPLUS", "LINK_BAIWANG",
                            "LINK_NUONUO", "LINK_CHINATAX", "LINK_BWJF",
                            "LINK_JINCAI", "LINK_KERUYUN"):
                known_paths = [r["path"] for r in downloaded if r.get("path")]
                d, fl = download_link(c, pdfs_dir, log, known_paths=known_paths)
            else:
                d, fl = [], [{"subject": c.get("subject"), "reason": f"unknown method {method}"}]
            downloaded.extend(d)
            failed.extend(fl)
        except Exception as e:
            failed.append({"subject": c.get("subject"), "reason": str(e), "method": method})
            say(f"  ❌ exception on {c.get('subject','')[:50]}: {e}")
    say(f"  downloaded {len(downloaded)} / failed {len(failed)} / skipped {len(skipped)} in {time.time()-t0:.1f}s")

    # --- Step 6: LLM OCR + classify + plausibility ---
    step4_path = os.path.join(output_dir, "step4_downloaded.json")
    if use_llm:
        say("\n--- Step 6: LLM OCR + classify + plausibility ---")
        t0 = time.time()
        try:
            # max_workers intentionally omitted — reads LLM_OCR_CONCURRENCY env
            # var, defaults to 5. Set LLM_OCR_CONCURRENCY=2 for Anthropic tier-1.
            analyses = analyze_pdf_batch(
                downloaded,
                use_llm=use_llm,
                logger=log,
            )
        except (LLMAuthError, LLMConfigError) as e:
            say(f"❌ LLM config error: {e}")
            print(f"\nREMEDIATION: {e}", file=sys.stderr)
            log.close()
            sys.exit(EXIT_LLM_CONFIG)
        say(f"  OCR done for {len(analyses)} files in {time.time()-t0:.1f}s")

        # --- Step 7: rename by OCR ---
        say("\n--- Step 7: rename files by OCR ---")
        renamed = 0
        for rec in downloaded:
            if not rec.get("valid"):
                continue
            analysis = analyses.get(rec.get("path")) or {}
            old_name = os.path.basename(rec.get("path", ""))
            rename_by_ocr(rec, analysis, pdfs_dir)
            new_name = os.path.basename(rec.get("path", ""))
            if new_name != old_name:
                renamed += 1
        say(f"  renamed {renamed}/{len(downloaded)} files")
    else:
        say("\n--- Step 6+7: skipped (LLM disabled) ---")
        # Tag every record as UNPARSED so matching/CSV handles it consistently
        for rec in downloaded:
            if rec.get("valid"):
                rec["category"] = "UNPARSED"
                rec["ocr"] = None

    # --- Merge supplemental if requested (BEFORE matching/reporting) ---
    if args.supplemental and os.path.exists(step4_path):
        say("\n--- Supplemental merge ---")
        before = len(downloaded)
        merged = merge_supplemental_downloads(step4_path, downloaded)
        say(f"  merged {before} fresh → {len(merged)} total in step4_downloaded.json")
        downloaded_all = merged
    else:
        # Initial run: step4 is just this run's downloaded
        downloaded_all = downloaded

    # --- Step 8: matching ---
    say("\n--- Step 8: matching (P1 remark / P2 date+amount / P3 date-only) ---")
    t0 = time.time()
    matching_result = do_all_matching(downloaded_all)
    hotel_matched = len(matching_result["hotel"]["matched"])
    hotel_unmatched_inv = len(matching_result["hotel"]["unmatched_invoices"])
    hotel_unmatched_fol = len(matching_result["hotel"]["unmatched_folios"])
    rh_matched = len(matching_result["ridehailing"]["matched"])
    rh_unmatched_inv = len(matching_result["ridehailing"]["unmatched_invoices"])
    rh_unmatched_rec = len(matching_result["ridehailing"]["unmatched_receipts"])
    say(f"  hotel: {hotel_matched} matched, {hotel_unmatched_inv} invoices + {hotel_unmatched_fol} folios unmatched")
    say(f"  ridehailing: {rh_matched} matched, {rh_unmatched_inv} invoices + {rh_unmatched_rec} receipts unmatched")
    say(f"  done in {time.time()-t0:.1f}s")

    # --- Persist state before writing user-facing artifacts ---
    if not args.supplemental:
        # Initial run — overwrite
        with open(step4_path, "w") as f:
            json.dump({"downloaded": downloaded_all, "failed": failed, "skipped": skipped},
                      f, ensure_ascii=False, indent=2, default=str)

    # --- Step 8.5: build aggregation (single source for CSV/MD/message) ---
    # do_all_matching physically dedupes OCR-business-key duplicates; exclude
    # those from valid_records so build_aggregation's completeness assertion
    # sees the same record set the matching buckets were populated from.
    dedup_removed_ids = {id(r) for r in matching_result.get("dedup_removed", [])}
    valid_records = [
        d for d in downloaded_all
        if d.get("valid") and id(d) not in dedup_removed_ids
    ]
    # v5.7 Unit 3: split IGNORED out before aggregation. matching and
    # build_aggregation only see reimbursable records; ignored_records is
    # passed separately to the report writer and OpenClaw summary (Unit 4).
    ignored_records = [d for d in valid_records if d.get("category") == "IGNORED"]
    reimbursable_records = [d for d in valid_records if d.get("category") != "IGNORED"]
    aggregation = build_aggregation(matching_result, reimbursable_records)

    # --- Step 9c: write missing.json (first, so report can render
    #     out_of_range_items[] subsection) ---
    missing_path = os.path.join(output_dir, "missing.json")
    prev_hash = _previous_convergence_hash(output_dir)
    missing_payload = write_missing_json(
        missing_path,
        batch_dir=output_dir,
        iteration=iteration,
        iteration_cap=args.iteration_cap,
        matching_result=matching_result,
        unparsed_records=matching_result.get("unparsed", []),
        previous_convergence_hash=prev_hash,
        run_start_date=args.start,    # v5.5 — cross-quarter routing
        run_end_date=args.end,
    )

    # --- Step 9a: write 下载报告.md ---
    report_path = os.path.join(output_dir, "下载报告.md")
    write_report_md(
        report_path,
        downloaded_all=downloaded_all, failed=failed, skipped=skipped,
        matching_result=matching_result,
        date_range=(args.start, args.end),
        iteration=iteration,
        supplemental=args.supplemental,
        aggregation=aggregation,
        out_of_range_items=missing_payload.get("out_of_range_items", []),
        ignored_records=ignored_records,
    )
    say(f"\n✅ Report:   {report_path}")

    # --- Step 9b: write 发票汇总.csv ---
    csv_path = os.path.join(output_dir, "发票汇总.csv")
    n_csv = write_summary_csv(csv_path, aggregation)
    say(f"✅ CSV:      {csv_path}  ({n_csv} rows)")
    say(f"✅ missing.json: {missing_path}  "
        f"(status={missing_payload['status']}, next={missing_payload['recommended_next_action']}, "
        f"items={len(missing_payload['items'])})")

    # --- Step 10: zip the output dir (DEC-6: degrade to None on failure) ---
    # v5.7.1: pass include_pdf_paths so cross-batch leftovers in pdfs_dir
    # (files from previous runs that accumulated across invocations) stay
    # on disk for audit but don't get packaged into the deliverable zip.
    this_run_paths = _collect_this_run_pdf_paths(downloaded_all)
    leftover_count = _count_leftover_pdfs(pdfs_dir, this_run_paths)
    if leftover_count > 0:
        say(
            f"ℹ️  跨批次残留：pdfs/ 目录里有 {leftover_count} 份 PDF 来自之前的批次，"
            f"未打包进本次 zip（仍保留在磁盘上供审计）。"
            f"如需清理：rm {pdfs_dir}/*.pdf 后重跑。"
        )
    zip_path = None
    try:
        zip_path = zip_output(output_dir, include_pdf_paths=this_run_paths)
        say(f"✅ Zip:      {zip_path}")
    except RuntimeError as e:
        say(f"⚠️  zip skipped: {e}")

    say(f"\n✅ PDFs:     {pdfs_dir}/ ({sum(1 for d in downloaded_all if d.get('valid'))} files)")

    # --- Step 11: OpenClaw chat summary (stdout + run.log). MUST be called
    #     before log.close() — writer=say dual-writes to the still-open log.
    say("")
    print_openclaw_summary(
        aggregation,
        output_dir=output_dir,
        zip_path=zip_path,
        csv_path=csv_path,
        md_path=report_path,
        log_path=log_path,
        missing_status=missing_payload["recommended_next_action"],
        date_range=(args.start, args.end),
        writer=say,
        ignored_count=len(ignored_records),
    )

    # --- Exit code ---
    has_unparsed = any(d.get("category") == "UNPARSED" for d in downloaded_all if d.get("valid"))
    log.close()
    if has_unparsed or failed:
        sys.exit(EXIT_PARTIAL)
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    # Wrap main() so any uncaught exception surfaces as EXIT_UNKNOWN with a
    # REMEDIATION line pointing agents at run.log for the traceback. SystemExit
    # from inside main() (explicit sys.exit(N)) passes through unchanged.
    import traceback
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nREMEDIATION: interrupted by user; partial state may exist in output dir.",
              file=sys.stderr)
        sys.exit(130)
    except Exception as _e:
        traceback.print_exc(file=sys.stderr)
        print(
            f"\nREMEDIATION: unexpected error ({type(_e).__name__}); check run.log "
            f"in the output dir for full traceback.",
            file=sys.stderr,
        )
        sys.exit(EXIT_UNKNOWN)
