#!/usr/bin/env python3
"""
Gmail Invoice Downloader — end-to-end CLI (v5.3).

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
    classify_invoice_category,
    validate_pdf_header,
    make_unique_path,
    generate_filename,
    extract_date_from_email,
    extract_pdfs_from_zip,
    get_body_text,
    extract_seller_from_pdf,
    extract_hotel_from_folio_pdf,
    resolve_baiwang_short_url,
    resolve_baiwang_bwfp_short_url,
    resolve_nuonuo_short_url,
    resolve_bwjf_short_url,
    resolve_keruyun_short_url,
    DOC_TYPE_LABELS,
)

# v5.3 additions
from v53_pipeline import (  # noqa: E402
    analyze_pdf_batch,
    rename_by_ocr,
    do_all_matching,
    write_summary_csv,
    write_missing_json,
    zip_output,
    merge_supplemental_downloads,
    CATEGORY_LABELS,
)
from core.llm_client import LLMAuthError, LLMConfigError  # noqa: E402


# v5.3 exit codes
EXIT_OK = 0
EXIT_UNKNOWN = 1
EXIT_AUTH = 2
EXIT_LLM_CONFIG = 3
EXIT_GMAIL_QUOTA = 4
EXIT_PARTIAL = 5

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

    def _api_get(self, url):
        for _ in range(2):
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {self.token['access_token']}")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self._refresh()
                    continue
                raise
        raise RuntimeError("failed after token refresh")

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
        # If merchant is '未知商户', try to extract from PDF content
        if ok and merchant == "未知商户":
            real_name = None
            if actual_type == "TAX_INVOICE":
                real_name = extract_seller_from_pdf(out)
            elif actual_type == "HOTEL_FOLIO":
                real_name = extract_hotel_from_folio_pdf(out)
            if real_name and real_name != "未知商户":
                new_fname = generate_filename(date_str, real_name, actual_type)
                if len(atts) > 1 or has_mixed:
                    base, ext = os.path.splitext(new_fname)
                    suffix = fname[fname.rfind("_发票")+len("_发票"):fname.rfind(".pdf")] if "_发票" in fname else ""
                    new_fname = f"{base}{suffix}{ext}"
                new_out = make_unique_path(pdfs_dir, new_fname)
                os.rename(out, new_out)
                out = new_out
                merchant = real_name
        print(f"  {'✅' if ok else '⚠️'} {os.path.basename(out)} ({len(data)//1024}KB)", file=log)
        rec = {
            "path": out, "valid": ok, "info": info,
            "subject": entry.get("subject"), "method": "ATTACHMENT",
            "merchant": merchant, "date": date_str, "doc_type": actual_type,
            "message_id": msg_id,
            "attachment_part_id": att.get("attachmentId"),
            "internal_date": entry.get("internal_date"),
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
                }
                (downloaded if ok else failed).append(rec)
    return downloaded, failed


def download_link(entry, pdfs_dir, log):
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
    # Dedup: if downloaded content is identical to an existing file in pdfs_dir, drop it
    if ok:
        with open(out, "rb") as f:
            new_hash = hashlib.md5(f.read()).hexdigest()
        for existing in os.listdir(pdfs_dir):
            ex_path = os.path.join(pdfs_dir, existing)
            if ex_path == out or not existing.endswith('.pdf'):
                continue
            if os.path.getsize(ex_path) != os.path.getsize(out):
                continue
            with open(ex_path, "rb") as f:
                if hashlib.md5(f.read()).hexdigest() == new_hash:
                    os.remove(out)
                    print(f"  ♻️  duplicate of {existing}, removed {os.path.basename(out)}", file=log)
                    return [], []
    # If merchant is '未知商户' and this is a tax invoice, try to extract seller from PDF
    if ok and merchant == "未知商户" and entry["doc_type"] == "TAX_INVOICE":
        real_seller = extract_seller_from_pdf(out)
        if real_seller and real_seller != "未知商户":
            new_fname = generate_filename(date_str, real_seller, entry["doc_type"])
            new_out = make_unique_path(pdfs_dir, new_fname)
            os.rename(out, new_out)
            out = new_out
            merchant = real_seller
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
    }
    return ([rec], []) if ok else ([], [{**rec, "reason": info}])


# ─── Pairing (v5.2 — strictly by same date) ───────────────────────────────

def pair_folios_with_invoices(downloaded):
    """Pair each folio with same-day *LODGING* invoices. Dining is never paired."""
    folios_by_date = defaultdict(list)
    lodging_by_date = defaultdict(list)
    all_dining = []
    all_other = []
    for d in downloaded:
        if not d.get("valid"):
            continue
        if d["doc_type"] == "HOTEL_FOLIO":
            folios_by_date[d["date"]].append(d)
        elif d["doc_type"] == "TAX_INVOICE":
            cat = classify_invoice_category(d["path"])
            d["category"] = cat
            if cat == "LODGING":
                lodging_by_date[d["date"]].append(d)
            elif cat == "DINING":
                all_dining.append(d)
            else:
                all_other.append(d)

    pairings = []
    matched = set()
    for date_str in sorted(folios_by_date.keys()):
        for folio in folios_by_date[date_str]:
            lodging = lodging_by_date.get(date_str, [])
            for inv in lodging:
                matched.add(inv["path"])
            pairings.append({"folio": folio, "lodging": lodging})

    unmatched_lodging = [
        inv for invs in lodging_by_date.values() for inv in invs if inv["path"] not in matched
    ]
    return pairings, unmatched_lodging, all_dining, all_other


# ─── Report generation ─────────────────────────────────────────────────────

def write_report(path, downloaded, failed, skipped, pairings, unmatched_lodging, all_dining, all_other, date_range):
    now = datetime.datetime.now(CST)
    valid_downloads = [d for d in downloaded if d.get("valid")]
    folios = [d for d in valid_downloads if d["doc_type"] == "HOTEL_FOLIO"]
    lodging = [d for d in valid_downloads if d["doc_type"] == "TAX_INVOICE" and d.get("category") == "LODGING"]
    other_docs = [d for d in valid_downloads if d["doc_type"] not in ("HOTEL_FOLIO", "TAX_INVOICE")]

    lines = []
    lines.append(f"# Gmail 发票下载报告\n")
    lines.append(f"**日期范围**：{date_range[0]} → {date_range[1]}  ")
    lines.append(f"**生成时间**：{now.strftime('%Y-%m-%d %H:%M')} CST  ")
    lines.append(f"**文件数**：{len(valid_downloads)} 份 PDF  ")
    lines.append(f"**配对规则**：水单 ↔ 住宿发票严格同日。餐饮不自动关联（开票日 ≠ 就餐日）。\n")

    lines.append("## 📊 摘要\n")
    lines.append("| | |\n|---|---|")
    lines.append(f"| 酒店水单 | {len(folios)} |")
    lines.append(f"| 住宿发票 | {len(lodging)} |")
    lines.append(f"| 餐饮发票 | {len(all_dining)} |")
    lines.append(f"| 其他发票 | {len(all_other)} |")
    lines.append(f"| 其他单据 | {len(other_docs)} |")
    lines.append(f"| 下载失败 | {len(failed)} |")
    lines.append(f"| 跳过 (MANUAL/IGNORE) | {len(skipped)} |\n")

    lines.append("## 🏨 酒店入住配对\n")
    lines.append("| 退房日 | 水单开票方 | 水单 | 住宿发票 | 状态 |\n|--------|-------------|:----:|:--------:|:----:|")
    for p in sorted(pairings, key=lambda x: x["folio"]["date"]):
        f = p["folio"]
        fd = f["date"]
        st = "✅" if p["lodging"] else "⚠️ 同日无住宿发票"
        lines.append(f"| {fd[:4]}-{fd[4:6]}-{fd[6:]} | {f['merchant']} | 1 | {len(p['lodging'])} | {st} |")
    lines.append("\n### 配对详情\n")
    for p in sorted(pairings, key=lambda x: x["folio"]["date"]):
        f = p["folio"]
        fd = f["date"]
        lines.append(f"\n**{fd[:4]}-{fd[4:6]}-{fd[6:]}  （{f['merchant']}）**\n")
        lines.append(f"- 📋 水单 `{os.path.basename(f['path'])}`")
        for inv in p["lodging"]:
            lines.append(f"- 🏨 住宿发票 `{os.path.basename(inv['path'])}` _(销售方: {inv['merchant']})_")
        if not p["lodging"]:
            lines.append("- ⚠️ 同日无住宿发票")

    if unmatched_lodging:
        lines.append(f"\n## ⚠️ 未匹配的住宿发票（{len(unmatched_lodging)} 张）\n")
        lines.append("同日无水单，可能是水单未到邮箱。\n")
        for inv in sorted(unmatched_lodging, key=lambda x: x["date"]):
            d = inv['date']
            lines.append(f"- [{d[:4]}-{d[4:6]}-{d[6:]}] **{inv['merchant']}** `{os.path.basename(inv['path'])}`")

    if all_dining:
        lines.append(f"\n## 🍽️ 餐饮发票（{len(all_dining)} 张，按商户聚合）\n")
        lines.append("餐饮发票不自动关联酒店（开票日可能合并多天就餐）。\n")
        by_m = defaultdict(list)
        for inv in all_dining:
            by_m[inv["merchant"]].append(inv)
        for m, invs in sorted(by_m.items(), key=lambda x: -len(x[1])):
            ds = sorted(set(i['date'] for i in invs))
            lines.append(f"### {m} × {len(invs)}")
            for inv in sorted(invs, key=lambda x: x["date"]):
                d = inv['date']
                lines.append(f"- [{d[:4]}-{d[4:6]}-{d[6:]}] `{os.path.basename(inv['path'])}`")

    if all_other:
        lines.append(f"\n## 📄 其他发票（{len(all_other)} 张）\n")
        for inv in sorted(all_other, key=lambda x: x["date"]):
            d = inv['date']
            lines.append(f"- [{d[:4]}-{d[4:6]}-{d[6:]}] **{inv['merchant']}** `{os.path.basename(inv['path'])}`")

    if other_docs:
        lines.append(f"\n## 🚖 非发票单据（{len(other_docs)} 份）\n")
        by_t = defaultdict(list)
        for d in other_docs:
            by_t[d["doc_type"]].append(d)
        for t, items in sorted(by_t.items()):
            lines.append(f"- **{DOC_TYPE_LABELS.get(t, t)}** × {len(items)}")

    if failed:
        lines.append(f"\n## ❌ 下载失败（{len(failed)} 项）\n")
        for f in failed:
            lines.append(f"- {f.get('subject', '?')[:70]}: {f.get('reason', '?')}")

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ─── v5.3 report ───────────────────────────────────────────────────────────

def write_report_v53(
    path, *,
    downloaded_all, failed, skipped,
    matching_result,
    date_range,
    iteration: int,
    supplemental: bool,
):
    """Emit 下载报告.md reflecting v5.3 matching (P1/P2/P3 + ride-hailing + unparsed)
    and supplemental loop context."""
    now = datetime.datetime.now(CST)
    valid = [d for d in downloaded_all if d.get("valid")]

    def _cat_count(cat):
        return sum(1 for d in valid if d.get("category") == cat)

    lines = []
    lines.append("# Gmail 发票下载报告 (v5.3)\n")
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
        if matched:
            lines.append("| 退房日 | 销售方 | 匹配方式 | 水单 | 酒店发票 |")
            lines.append("|--------|--------|----------|:----:|:--------:|")
            for m in matched:
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
                    "date_only (v5.2 fallback)": "P3 (仅日期)⚠️",
                }.get(match_type, match_type)
                lines.append(f"| {inv_date} | {vendor} | {type_label} | `{fol_name}` | `{inv_name}` |")
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


def main():
    ap = argparse.ArgumentParser(
        description="Gmail Invoice Downloader v5.3 — search, download, OCR, match, report.",
    )
    ap.add_argument("--start", required=True, help="Gmail date: 2026/01/01")
    ap.add_argument("--end", required=True, help="Gmail date (exclusive): 2026/05/01")
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
    ap.add_argument("--ascii-names", action="store_true",
                    help="(not yet implemented) transliterate vendor to pinyin for "
                         "easier email forwarding.")

    # v5.3 preflight
    ap.add_argument("--skip-preflight", action="store_true",
                    help="Skip doctor.py preflight checks (not recommended).")

    args = ap.parse_args()

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
    say(f"Gmail Invoice Downloader v5.3 — {mode} iteration={iteration}")
    say(f"Run started @ {datetime.datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST")
    say("=" * 70)
    say(f"Date range: {args.start} → {args.end}")
    say(f"LLM provider: {os.environ.get('LLM_PROVIDER', 'bedrock')}"
        + (" [disabled]" if not use_llm else ""))

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
    except Exception as e:
        say(f"❌ Gmail search failed: {e}")
        print(f"\nREMEDIATION: run `python3 scripts/gmail-auth.py` to refresh token.json.",
              file=sys.stderr)
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
                d, fl = download_link(c, pdfs_dir, log)
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
            analyses = analyze_pdf_batch(
                downloaded,
                max_workers=2,
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

    # --- Step 9a: write 下载报告.md ---
    report_path = os.path.join(output_dir, "下载报告.md")
    write_report_v53(
        report_path,
        downloaded_all=downloaded_all, failed=failed, skipped=skipped,
        matching_result=matching_result,
        date_range=(args.start, args.end),
        iteration=iteration,
        supplemental=args.supplemental,
    )
    say(f"\n✅ Report:   {report_path}")

    # --- Step 9b: write 发票汇总.csv ---
    csv_path = os.path.join(output_dir, "发票汇总.csv")
    n_csv = write_summary_csv(csv_path, [d for d in downloaded_all if d.get("valid")])
    say(f"✅ CSV:      {csv_path}  ({n_csv} rows)")

    # --- Step 9c: write missing.json ---
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
    )
    say(f"✅ missing.json: {missing_path}  "
        f"(status={missing_payload['status']}, next={missing_payload['recommended_next_action']}, "
        f"items={len(missing_payload['items'])})")

    # --- Step 10: zip the output dir ---
    try:
        zip_path = zip_output(output_dir)
        say(f"✅ Zip:      {zip_path}")
    except RuntimeError as e:
        say(f"⚠️  zip skipped: {e}")

    say(f"\n✅ PDFs:     {pdfs_dir}/ ({sum(1 for d in downloaded_all if d.get('valid'))} files)")

    # --- Exit code ---
    has_unparsed = any(d.get("category") == "UNPARSED" for d in downloaded_all if d.get("valid"))
    log.close()
    if has_unparsed or failed:
        sys.exit(EXIT_PARTIAL)
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
