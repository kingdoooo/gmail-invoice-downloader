"""
invoice_helpers.py — Bundled helper functions for gmail-invoice-downloader skill.
Pre-loaded into run_python namespace when the skill is active.
"""
import re
import os
import base64
import zipfile


# ─── Email Body Extraction ───────────────────────────────────────────

def get_body_text(payload):
    """Recursively extract body text (HTML + plain) from Gmail message payload."""
    text = ""
    if payload.get("body", {}).get("data"):
        text += base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text += get_body_text(part)
    return text


def find_pdf_attachments(payload):
    """Recursively find PDF attachments in Gmail message payload.
    Returns list of {filename, attachmentId, mimeType, size}."""
    atts = []
    fn = payload.get("filename", "")
    if fn and payload.get("body", {}).get("attachmentId"):
        if fn.lower().endswith(".pdf"):
            atts.append({
                "filename": fn,
                "attachmentId": payload["body"]["attachmentId"],
                "mimeType": payload.get("mimeType", ""),
                "size": payload.get("body", {}).get("size", 0),
            })
    for part in payload.get("parts", []):
        atts.extend(find_pdf_attachments(part))
    return atts


def find_zip_attachments(payload):
    """Recursively find ZIP attachments in Gmail message payload.
    Returns list of {filename, attachmentId, mimeType, size}."""
    atts = []
    fn = payload.get("filename", "")
    if fn and payload.get("body", {}).get("attachmentId"):
        if fn.lower().endswith(".zip"):
            atts.append({
                "filename": fn,
                "attachmentId": payload["body"]["attachmentId"],
                "mimeType": payload.get("mimeType", ""),
                "size": payload.get("body", {}).get("size", 0),
            })
    for part in payload.get("parts", []):
        atts.extend(find_zip_attachments(part))
    return atts


def extract_pdfs_from_zip(zip_path, extract_dir):
    """Extract only PDF files from a ZIP archive. Returns list of extracted file paths."""
    extracted = []
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for name in zf.namelist():
            if name.lower().endswith('.pdf'):
                zf.extract(name, extract_dir)
                extracted.append(os.path.join(extract_dir, name))
    return extracted


# ─── URL Extraction ──────────────────────────────────────────────────

def extract_real_urls(html):
    """Extract URLs from both href attributes AND <a> tag display text.
    Critical because email tracking platforms wrap real URLs in expiring redirects,
    but the display text often contains the actual download URL."""
    urls = set()
    # 1. href values
    for href in re.findall(r'href="([^"]+)"', html):
        if href.startswith("http"):
            urls.add(href)
    # 2. Display text of <a> tags
    for match in re.finditer(r'<a[^>]*>(.*?)</a>', html, re.DOTALL):
        link_text = re.sub(r'<[^>]+>', '', match.group(1)).strip()
        for url in re.findall(r'https?://[^\s<>"\']+', link_text):
            urls.add(url)
    # 3. Plain text URLs
    for url in re.findall(r'https?://[^\s<>"\']+', html):
        urls.add(url)
    return list(urls)


def extract_fapiao_com_url(body):
    """Extract fapiao.com PDF download URL from email body.
    The request= token is 100+ chars — use permissive regex to avoid truncation."""
    urls = re.findall(r'https://www\.fapiao\.com/dzfp-web/pdf/download\?request=[^\s"<>]+', body)
    if urls:
        return urls[0].rstrip('"\'>')
    # Also check <a> tag display text
    for match in re.finditer(r'<a[^>]*>(.*?)</a>', body, re.DOTALL):
        link_text = re.sub(r'<[^>]+>', '', match.group(1)).strip()
        found = re.findall(r'https://www\.fapiao\.com/dzfp-web/pdf/download\?request=[^\s"<>]+', link_text)
        if found:
            return found[0].rstrip('"\'>')
    return None


def extract_baiwang_download_url(body):
    """Extract 百望云 PDF download URL from email body.
    Supports FOUR known email templates:
      1. Preview URL: pis.baiwang.com/smkp-vue/previewInvoiceAllEle?param={HEX}
         → construct: pis.baiwang.com/bwmg/mix/bw/downloadFormat?param={HEX}&formatType=pdf
      2. Short link:  u.baiwang.com/{TOKEN}
         → Marker: 'BAIWANG_SHORT:{short_url}'. Follow 301 → construct downloadFormat URL.
      3. New short link: bwfp.baiwang.com/{TOKEN}
         → Marker: 'BAIWANG_BWFP:{short_url}'. Follow 302 chain to get paramList →
           call sapi/invoice/issue-scan/get-detail.do → extract `data.invoiceSimpleVo.url`.
    Returns None when no pattern matches.
    """
    # Template 1: pis.baiwang.com preview
    params = re.findall(
        r'https?://pis\.baiwang\.com/smkp-vue/previewInvoiceAllEle\?param=([A-Fa-f0-9]+)', body
    )
    if not params:
        for match in re.finditer(r'<a[^>]*>(.*?)</a>', body, re.DOTALL):
            link_text = re.sub(r'<[^>]+>', '', match.group(1)).strip()
            found = re.findall(
                r'https?://pis\.baiwang\.com/smkp-vue/previewInvoiceAllEle\?param=([A-Fa-f0-9]+)',
                link_text,
            )
            if found:
                params = found
                break
    if params:
        return f"https://pis.baiwang.com/bwmg/mix/bw/downloadFormat?param={params[0]}&formatType=pdf"

    # Template 2: u.baiwang.com short link
    short = re.findall(r'https?://u\.baiwang\.com/\w+', body)
    if short:
        return f"BAIWANG_SHORT:{short[0].rstrip(chr(34) + chr(39) + chr(62))}"

    # Template 3: bwfp.baiwang.com short link (newer; uses issue-scan API)
    bwfp = re.findall(r'https?://bwfp\.baiwang\.com/[\w]+', body)
    # Exclude SPA paths like /fp/... and /sapi/... — short links are top-level
    bwfp = [u for u in bwfp if not re.search(r'/(fp|sapi|qr|invoice)(/|$)', u)]
    if bwfp:
        return f"BAIWANG_BWFP:{bwfp[0].rstrip(chr(34) + chr(39) + chr(62))}"
    return None


def resolve_baiwang_short_url(short_url):
    """Follow 301 on u.baiwang.com/{token} to extract the `param` hex, then
    return the final PDF download URL. Requires network.
    Returns the downloadFormat URL, or None if resolution fails.
    """
    import urllib.request
    try:
        req = urllib.request.Request(short_url, method="GET")
        # Do NOT follow redirects — we only need the Location header of the first 301
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def http_error_301(self, req, fp, code, msg, headers):
                return headers
            http_error_302 = http_error_301
            http_error_303 = http_error_301
            http_error_307 = http_error_301
        opener = urllib.request.build_opener(NoRedirect)
        resp = opener.open(req, timeout=15)
        loc = resp.get("Location") if hasattr(resp, "get") else None
        if not loc:
            # Newer urllib may raise or return differently; fall back to reading
            return None
        m = re.search(r'\?param=([A-Fa-f0-9]+)', loc)
        if not m:
            return None
        return f"https://pis.baiwang.com/bwmg/mix/bw/downloadFormat?param={m.group(1)}&formatType=pdf"
    except Exception:
        return None


def resolve_baiwang_bwfp_short_url(short_url):
    """Resolve bwfp.baiwang.com/{TOKEN} short link to direct PDF URL.

    Flow (same shape as Nuonuo):
      1. GET short URL, follow 302 chain manually (HEAD returns 403), until
         final URL contains `paramList={税号}!!!{发票号}!false`.
      2. POST/GET `https://bwfp.baiwang.com/sapi/invoice/issue-scan/get-detail.do?paramList=...`
      3. JSON response's `data.invoiceSimpleVo.url` is the PDF URL
         (lives on `fp.baiwang.com/format/d?d=HEX`).
    """
    import urllib.request, urllib.parse, http.client, json
    try:
        url = short_url
        for _ in range(5):
            p = urllib.parse.urlparse(url)
            conn = http.client.HTTPSConnection(p.netloc, timeout=15)
            path_q = p.path + ('?' + p.query if p.query else '')
            conn.request("GET", path_q, headers={"User-Agent": "Mozilla/5.0"})
            resp = conn.getresponse()
            loc = resp.getheader('Location')
            conn.close()
            if not loc:
                break
            if loc.startswith('/'):
                loc = f"{p.scheme}://{p.netloc}{loc}"
            url = loc
            if 'paramList=' in url:
                break
        m = re.search(r'paramList=([^&]+)', url)
        if not m:
            return None
        param = urllib.parse.unquote(m.group(1))

        api = (f"https://bwfp.baiwang.com/sapi/invoice/issue-scan/get-detail.do"
               f"?paramList={urllib.parse.quote(param)}")
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return data.get('data', {}).get('invoiceSimpleVo', {}).get('url')
    except Exception:
        return None


def extract_merchant_from_body(body):
    """Extract merchant/seller name from invoice email body.
    Common patterns in Chinese invoice emails:
      - '【XXX】为您开具' / 'XXX为您开具了电子发票'
      - '销售方名称：XXX' / '开票方名称：XXX'
      - '来自XXX的电子发票'
    Returns the longest match (more specific) or None.
    """
    text = re.sub(r'<[^>]+>', ' ', body)
    text = re.sub(r'\s+', ' ', text)
    candidates = []
    patterns = [
        r'【([^】]{4,60})】\s*为您开具',
        r'【([^】]{4,60})】\s*开具',
        r'([\u4e00-\u9fa5A-Za-z0-9（）()]{4,60}?)\s*为您开具了电子发票',
        r'销售方[名称]*[：:]\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{4,60})',
        r'开票方[名称]*[：:]\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{4,60})',
        r'开票单位[：:]\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{4,60})',
        r'来自\s*【?([^】\s]{4,60}?)】?\s*(?:开具|的电子发票)',
    ]
    for p in patterns:
        for m in re.finditer(p, text):
            name = m.group(1).strip()
            # Drop obvious buyer-side names
            if any(bad in name for bad in ['购买方', '用户', '友商']):
                continue
            candidates.append(name)
    if not candidates:
        return None
    # Prefer the longest — usually the most specific
    return max(candidates, key=len)


def extract_merchant_from_attachment_filename(filename):
    """Some hotel/finance senders name PDFs as `dzfp_{invoiceNo}_{merchant}_{ts}.pdf`.

    ⚠️ Ambiguity: the {merchant} field may be either the SELLER or the BUYER
    depending on who sent the email:
      - 酒店财务代发 (e.g. `1XXXXXXXXXX@163.com` — 前台手机号前缀) → seller (“鲁能泰山...万豪酒店”)
      - 百望云/诺诺网代发 → buyer (“亚马逊信息服务...有限公司”)

    To stay safe, reject obvious buyer-side names (Kent's company markers).
    Prefer reading the seller directly from the PDF content (see extract_seller_from_pdf).
    """
    if not filename:
        return None
    m = re.match(r'dzfp_\d+_(.+?)_\d{14}\.pdf$', filename)
    if not m:
        return None
    name = m.group(1)
    # Reject buyer-side names (configurable; add more domains/markers as needed)
    buyer_markers = ["亚马逊", "amazon"]
    if any(marker in name.lower() for marker in buyer_markers):
        return None
    return name


def extract_seller_from_pdf(pdf_path):
    """Parse the seller name (销售方/开票方) from a Chinese e-invoice PDF.
    Looks for the standard layout `购 名称：XXX  销 名称：YYY`.
    Requires `pdftotext` in PATH. Returns None on failure.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        text = r.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None
    # Primary pattern: “销 名称：XXX” (with flexible whitespace)
    m = re.search(r'销\s*名称\s*[：:]\s*([^\s\n]+(?:\s[^\s\n]+)*?)(?:\s{2,}|\n|$)', text)
    if m:
        return m.group(1).strip()
    # Alternative: “销售方名称：XXX”
    m = re.search(r'销售方\s*名称\s*[：:]\s*([^\n]+?)(?:\s{2,}|\n|$)', text)
    if m:
        return m.group(1).strip()
    return None


def extract_hotel_from_folio_pdf(pdf_path):
    """Parse the hotel property name from a folio PDF's header.

    Folios (水单) don't have a structured 销售方 field. Instead the hotel brand
    appears at the very top of the page in all-caps English or the Chinese
    full name. Common patterns:
      - `HILTON SUZHOU NEW DISTRICT` (all-caps on line 1)
      - `DOUBLETREE BY HILTON WUXI`
      - `INFORMATION INVOICE` + later `...无锡万豪酒店...`
      - `Guest Folio` page with hotel name embedded in JSON $Param or HC: code

    Returns a cleaned brand name, or None on failure.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        text = r.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None

    # Look at first 30 lines
    lines = text.split("\n")[:30]

    # Pattern 1: Hilton-brand header — uppercase brand on early lines
    for line in lines[:8]:
        s = line.strip()
        # e.g. "HILTON SUZHOU NEW DISTRICT" or "DOUBLETREE BY HILTON WUXI"
        if re.match(r'^(HILTON|DOUBLETREE|WALDORF|CONRAD|HAMPTON|HILTON GARDEN INN)[\s\w]+$', s):
            return s.title()
        # Marriott-style: "Marriott Wuxi Lihu Lake"
        if re.match(r'^(Marriott|Courtyard|Sheraton|Westin|Renaissance|Ritz)[\s\w]+$', s):
            return s

    # Pattern 2: Chinese hotel name in body (flexible match)
    # e.g. "快捳 for HHonors members at the Hilton Suzhou ..."
    for line in lines[:20]:
        m = re.search(r'(HILTON[\s\w]+(?:DISTRICT|HOTEL|INN|SUITES)?)', line)
        if m:
            return m.group(1).strip().title()

    # Pattern 3: Hotel code (HC:WUXML etc) mapping
    m = re.search(r'HC:(\w+)', text)
    if m:
        code_map = {
            "WUXML": "无锡万豪酒店",        # Wuxi Marriott Lihu Lake
            "WUXLL": "无锡万怡酒店",        # Wuxi Courtyard Marriott
            "WUXLP": "无锡福朗喜来登酒店",  # Wuxi Four Points
            "NKGEE": "南京多伦多酒店",        # Nanjing property
            "NKGSC": "Marriott Nanjing South Hotel",
            "NKGSS": "Marriott Nanjing South",
        }
        if m.group(1) in code_map:
            return code_map[m.group(1)]
        return f"Hotel-{m.group(1)}"

    # Pattern 4: IHG/Marriott "at the XXX" in subject-like line
    for line in lines[:10]:
        m = re.search(r'at the (HILTON[\s\w]+?)(?:\s{2,}|[,.]|$)', line, re.I)
        if m:
            return m.group(1).strip().title()

    # Pattern 5: Hotel brand name appearing mid-document (Hilton/IHG water bills)
    # e.g. "Hilton Garden Inn Nanjing Hexi Olympic Sports Center 29/07/2025"
    for line in lines:
        m = re.search(
            r'\b(Hilton(?: Garden Inn| Suzhou| Honors Club)?[^\d\n]+|'
            r'DoubleTree[\s\w]+|'
            r'Waldorf Astoria[\s\w]+|'
            r'Conrad [A-Z][\s\w]+|'
            r'InterContinental [A-Z][\s\w]+|'
            r'Crowne Plaza [A-Z][\s\w]+|'
            r'Holiday Inn [A-Z][\s\w]+|'
            r'Indigo [A-Z][\s\w]+)',
            line,
        )
        if m:
            brand = m.group(1).strip()
            # Chop off trailing date/time garbage
            brand = re.sub(r'\s+\d{1,2}/\d{1,2}/\d{2,4}.*$', '', brand)
            brand = re.sub(r'\s+\d{2}:\d{2}.*$', '', brand)
            return brand.strip()

    # Pattern 6: IHG IHG-branded water bill with minimal header — detect by
    # 'IHG ONE Rewards' and city from address
    if 'IHG ONE Rewards' in text or 'IHG One Rewards' in text:
        # Try to pick up address second-line city (e.g. 'Wuhan HUB 430073')
        for line in lines:
            m = re.search(r'(Wuhan|Nanjing|Shanghai|Beijing|Hangzhou|Suzhou|Wuxi|Shenzhen|Guangzhou|Chengdu|Ningbo)\s+[A-Z]{2,4}\s+\d{6}', line)
            if m:
                return f"IHG {m.group(1)}"
        return "IHG Hotel"

    return None


def extract_invoice_date_from_body(body):
    """Extract the authoritative invoice issuance date from email body.
    Pattern: '开具日期：2026年03月19日' → '20260319'
    Returns YYYYMMDD string or None.
    """
    text = re.sub(r'<[^>]+>', ' ', body)
    m = re.search(r'开具日期[：:]\s*(\d{4})年(\d{1,2})月(\d{1,2})日', text)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return None


def extract_xforceplus_pdf_url(body):
    """Extract xforceplus short URL labeled as PDF from email body.
    Emails list 3 links: PDF, XML, OFD — we must pick the one labeled PDF."""
    # Match pattern: 发票下载地址(PDF)：https://s.xforceplus.com/XXXXX
    match = re.search(r'(?:PDF|pdf)[\uff09)]*[\uff1a:]?\s*(https://s\.xforceplus\.com/\w+)', body)
    if match:
        return match.group(1)
    # Fallback: first xforceplus URL (less reliable)
    all_urls = re.findall(r'https://s\.xforceplus\.com/\w+', body)
    return all_urls[0] if all_urls else None


# ─── Nuonuo (诺诺网) ──────────────────────────────────────────────────
#
# 邮件正文包含短链: https://nnfp.jss.com.cn/{TOKEN}
# 短链 302 链：k0.do → scanUI_k0 → printQrcode?paramList={税号}!!!{发票号}!false
# API: GET /sapi/scan2/getIvcDetailShow.do?paramList=...
# 返回 JSON 的 data.invoiceSimpleVo.url 即 PDF 直链。

def extract_nuonuo_short_url(body):
    """Find the 诺诺网 short link in email body. Returns marker for
    two-step resolution.

    Excludes API/utility paths (/allow /invoice /scan /sapi /scan-invoice)
    that share the nnfp.jss.com.cn host but are not redirectable short
    links. Without this filter, the /allow QR-image URL (which appears
    before the real short link in Nuonuo's HTML) wins at urls[0] and
    downstream resolution fails.
    """
    urls = re.findall(r'https?://nnfp\.jss\.com\.cn/[\w\-=]+', body)
    EXCLUDED = ('/allow', '/invoice', '/scan', '/sapi', '/scan-invoice')
    urls = [
        u for u in urls
        if not any(p + '/' in u or u.endswith(p) for p in EXCLUDED)
    ]
    if urls:
        return f"NUONUO_SHORT:{urls[0]}"
    return None


def resolve_nuonuo_short_url(short_url):
    """Follow 302 chain to extract paramList, then query detail API for real PDF URL.
    Returns the direct PDF download URL, or None on failure.
    """
    import urllib.request, urllib.parse, http.client, json
    try:
        url = short_url
        for _ in range(5):
            p = urllib.parse.urlparse(url)
            conn = http.client.HTTPSConnection(p.netloc, timeout=15)
            path_q = p.path + ('?' + p.query if p.query else '')
            conn.request("GET", path_q, headers={"User-Agent": "Mozilla/5.0"})
            resp = conn.getresponse()
            loc = resp.getheader('Location')
            conn.close()
            if not loc:
                break
            if loc.startswith('/'):
                loc = f"{p.scheme}://{p.netloc}{loc}"
            url = loc
            if 'paramList=' in url:
                break
        m = re.search(r'paramList=([^&]+)', url)
        if not m:
            return None
        param = urllib.parse.unquote(m.group(1))

        api = (f"https://nnfp.jss.com.cn/sapi/scan2/getIvcDetailShow.do"
               f"?paramList={urllib.parse.quote(param)}"
               "&aliView=true&shortLinkSource=1&wxApplet=0")
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return data.get('data', {}).get('invoiceSimpleVo', {}).get('url')
    except Exception:
        return None


# ─── 百旺金穗云 (广东税务局平台) ────────────────────────────────────────
#
# 邮件正文直接包含三个格式的下载 URL：
#   https://dppt.guangdong.chinatax.gov.cn:8443/kpfw/fpjfzz/v1/exportDzfpwjEwm?Wjgs=PDF&...
# 取 Wjgs=PDF 的那个，直接 curl。注意必须保留 Jym/Fphm/Kprq/Czsj 全部 query 参数。

def extract_gdbwjf_url(body):
    """Extract download URL from Chinese provincial tax bureau invoice platforms.

    Shared URL pattern across 广东百旺金穗云 / 江苏智云发票 (etc):
        https://dppt.{province}.chinatax.gov.cn:8443/kpfw/fpjfzz/v1/exportDzfpwjEwm?
            Wjgs={PDF|OFD|XML}&Jym=...&Fphm=...&Kprq=...&Czsj=...

    If the body has `Wjgs=PDF` directly, use that. Otherwise take any
    `Wjgs=OFD` or `Wjgs=XML` URL and flip the format flag to `PDF` — the
    tax bureau API accepts the same token triple for all three formats.

    Returns the PDF URL or None.
    """
    pattern = r'https://dppt\.[a-z]+\.chinatax\.gov\.cn[^\s<>"\']+'
    urls = re.findall(pattern, body)
    # 1) Prefer a direct PDF URL
    for u in urls:
        if 'Wjgs=PDF' in u:
            return u
    # 2) Synthesize: flip Wjgs=OFD/XML to Wjgs=PDF
    for u in urls:
        if 'Wjgs=OFD' in u or 'Wjgs=XML' in u:
            return re.sub(r'Wjgs=(?:OFD|XML)', 'Wjgs=PDF', u)
    return None


# ─── 云票 (bwjf.cn) ───────────────────────────────────────────────────
#
# 邮件正文含短链: https://fp.bwjf.cn/u/{TOKEN}
# 短链 302 到 https://www.bwjf.cn/allEleDeliverySuccess?...&pdfUrl={URLEncoded}&...
# 解析 pdfUrl query 参数，重新 URL-encode 中文（商户名含中文），再 curl。

def extract_bwjf_short_url(body):
    """Find 云票 short link. Returns marker for two-step resolution."""
    urls = re.findall(r'https?://fp\.bwjf\.cn/u/[\w\-]+', body)
    if urls:
        return f"BWJF_SHORT:{urls[0]}"
    return None


def resolve_bwjf_short_url(short_url):
    """Follow 302, extract pdfUrl query parameter, re-encode any non-ASCII chars.
    Returns the safe PDF URL, or None.
    """
    import urllib.request, urllib.parse, http.client
    try:
        p = urllib.parse.urlparse(short_url)
        conn = http.client.HTTPSConnection(p.netloc, timeout=15)
        path_q = p.path + ('?' + p.query if p.query else '')
        conn.request("GET", path_q, headers={"User-Agent": "Mozilla/5.0"})
        resp = conn.getresponse()
        loc = resp.getheader('Location')
        conn.close()
        if not loc:
            return None
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
        pdf_url = qs.get('pdfUrl', [''])[0]
        if not pdf_url:
            return None
        p2 = urllib.parse.urlparse(pdf_url)
        new_query = urllib.parse.urlencode(urllib.parse.parse_qsl(p2.query))
        return urllib.parse.urlunparse(
            (p2.scheme, p2.netloc, p2.path, p2.params, new_query, p2.fragment)
        )
    except Exception:
        return None


# ─── 金财数科 / jcsk100 (阿里/客如云使用) ────────────────────────
#
# 邮件正文直接包含三个格式的下载 URL，格式用结尾区分：
#   https://roc.jcsk100.com/external/d/a/{tag}_{发票号}_{..}_{..}_pdf
#   https://roc.jcsk100.com/external/d/a/{...}_ofd
#   https://roc.jcsk100.com/external/d/a/{...}_xml
# 阿里 krystore@service.alibaba.com / invoice.keruyun.com/s/XXX 短链 302 后的落地也是 jcsk100。

def extract_jincai_url(body):
    """Extract 金财数科 (jcsk100) PDF URL.
    Pattern ends with `_pdf` (not `_ofd` or `_xml`).
    """
    urls = re.findall(r'https://roc\.jcsk100\.com/external/d/a/[^\s<>"\']+', body)
    for u in urls:
        if u.endswith('_pdf'):
            return u
    # Fallback: flip _ofd/_xml to _pdf
    for u in urls:
        if u.endswith('_ofd') or u.endswith('_xml'):
            return re.sub(r'_(?:ofd|xml)$', '_pdf', u)
    return None


def extract_keruyun_short_url(body):
    """Find 阿里克如云 short link. Returns marker for single-step 302 resolution."""
    urls = re.findall(r'https?://invoice\.keruyun\.com/s/\w+', body)
    if urls:
        return f"KERUYUN_SHORT:{urls[0]}"
    return None


def resolve_keruyun_short_url(short_url):
    """Follow single 302 → jcsk100 direct URL."""
    import urllib.parse, http.client
    try:
        p = urllib.parse.urlparse(short_url)
        conn = http.client.HTTPSConnection(p.netloc, timeout=15)
        path_q = p.path + ('?' + p.query if p.query else '')
        conn.request("GET", path_q, headers={"User-Agent": "Mozilla/5.0"})
        resp = conn.getresponse()
        loc = resp.getheader('Location')
        conn.close()
        if not loc:
            return None
        # Ensure PDF format (some short links go to _pdf directly, some to a different format)
        if loc.endswith('_ofd') or loc.endswith('_xml'):
            loc = re.sub(r'_(?:ofd|xml)$', '_pdf', loc)
        return loc
    except Exception:
        return None


# ─── Email Classification ────────────────────────────────────────────

def classify_email(msg_data):
    """Classify an email into document type + download method.
    
    Returns dict with:
      - doc_type: TAX_INVOICE | HOTEL_FOLIO | TRIP_RECEIPT | OTHER_RECEIPT | UNKNOWN
      - method: ATTACHMENT | LINK_FAPIAO_COM | LINK_BAIWANG | LINK_XFORCEPLUS | MANUAL
      - pdf_attachments: list of attachment info (if any)
      - download_url: extracted URL (if link-based)
      - hotel_name: extracted hotel name (if hotel-related)
    """
    payload = msg_data.get("payload", {})
    subject = ""
    sender = ""
    sender_email = ""
    for h in payload.get("headers", []):
        if h["name"] == "Subject":
            subject = h["value"]
        elif h["name"] == "From":
            sender = h["value"]
    
    body = get_body_text(payload)
    pdf_atts = find_pdf_attachments(payload)
    zip_atts = find_zip_attachments(payload)
    
    # Extract bare email from "Name <email>" format
    m = re.search(r'<([^>]+)>', sender)
    sender_email = m.group(1).lower() if m else sender.lower()
    
    attachment_filenames = [a["filename"] for a in pdf_atts]
    hotel_name = extract_hotel_name(subject, body, sender, attachment_filenames)
    result = {
        "doc_type": "UNKNOWN",
        "method": "MANUAL",
        "pdf_attachments": pdf_atts,
        "download_url": None,
        "hotel_name": hotel_name,
        "merchant": hotel_name or extract_merchant_from_body(body),
        "invoice_date": extract_invoice_date_from_body(body),
        "subject": subject,
        "sender": sender,
        "zip_attachments": zip_atts,
    }
    
    # Classify document type by subject/filename
    invoice_kw = ["发票号", "发票号码", "发票代码", "电子发票", "数电发票", "发票金额"]
    folio_kw = ["水单", "账单", "folio", "e-folio", "电子账单",
                # Hilton sends empty-subject folios; detect by sender+content:
                "enjoyed your stay",  # Hilton/IHG folio email subject
                "your stay at the",
                "information invoice", "information bill",
                "guest folio", "客房账单"]
    trip_kw = ["行程报销", "报销单"]
    
    combined = subject + " " + " ".join(a["filename"] for a in pdf_atts)
    
    # 12306 train ticket — check sender first
    if sender_email == "12306@rails.com.cn":
        # 12306 payment-notification emails carry no attachment; the actual ticket
        # invoice must be downloaded from 12306.cn web UI manually.
        if not pdf_atts and not zip_atts:
            result["doc_type"] = "IGNORE"
            result["method"] = "IGNORE"
            result["ignore_reason"] = "12306 payment notification (no attachment, ticket invoice must be downloaded from 12306.cn)"
            return result
        result["doc_type"] = "TRAIN_TICKET"
        result["method"] = "ATTACHMENT" if pdf_atts else "ATTACHMENT_ZIP"
        return result
    
    if any(kw in combined for kw in folio_kw):
        result["doc_type"] = "HOTEL_FOLIO"
    elif any(kw in combined for kw in invoice_kw):
        result["doc_type"] = "TAX_INVOICE"
    elif any(kw in combined for kw in trip_kw):
        result["doc_type"] = "TRIP_RECEIPT"
    elif pdf_atts:
        result["doc_type"] = "OTHER_RECEIPT"
    
    # Sender-based folio hints (empty subject + hotel property system sender)
    hotel_folio_senders = [
        "receipt@hilton.com",    # Hilton post-stay folio delivery
        "@nkgss.com", "@nkgee.com",  # Marriott property sub-codes
        "@hualuxewuxi.com", "@intercon-indigowuxi.com",
        "gsm@",  # Generic hotel Guest Services Manager
        "fd@",   # Front Desk
        "dm@",   # Duty Manager
    ]
    if result["doc_type"] == "UNKNOWN" or result["doc_type"] == "OTHER_RECEIPT":
        if any(pat in sender_email for pat in hotel_folio_senders):
            result["doc_type"] = "HOTEL_FOLIO"

    # Determine download method
    if pdf_atts:
        result["method"] = "ATTACHMENT"
        return result
    
    # Has ZIP but no standalone PDF — extract PDFs from ZIP
    if zip_atts:
        result["method"] = "ATTACHMENT_ZIP"
        # doc_type may already be set from subject analysis above
        if result["doc_type"] == "UNKNOWN":
            result["doc_type"] = "OTHER_RECEIPT"
        return result
    
    # No attachment — check body for platform-specific download links
    fapiao_url = extract_fapiao_com_url(body)
    if fapiao_url:
        result["method"] = "LINK_FAPIAO_COM"
        result["download_url"] = fapiao_url
        if result["doc_type"] == "UNKNOWN":
            result["doc_type"] = "TAX_INVOICE"
        return result
    
    baiwang_url = extract_baiwang_download_url(body)
    if baiwang_url:
        result["method"] = "LINK_BAIWANG"
        result["download_url"] = baiwang_url
        if result["doc_type"] == "UNKNOWN":
            result["doc_type"] = "TAX_INVOICE"
        return result
    
    xfp_url = extract_xforceplus_pdf_url(body)
    if xfp_url:
        result["method"] = "LINK_XFORCEPLUS"
        result["download_url"] = xfp_url
        if result["doc_type"] == "UNKNOWN":
            result["doc_type"] = "TAX_INVOICE"
        return result

    nuonuo_marker = extract_nuonuo_short_url(body)
    if nuonuo_marker:
        result["method"] = "LINK_NUONUO"
        result["download_url"] = nuonuo_marker
        if result["doc_type"] == "UNKNOWN":
            result["doc_type"] = "TAX_INVOICE"
        return result

    gdbwjf_url = extract_gdbwjf_url(body)
    if gdbwjf_url:
        result["method"] = "LINK_CHINATAX"
        result["download_url"] = gdbwjf_url
        if result["doc_type"] == "UNKNOWN":
            result["doc_type"] = "TAX_INVOICE"
        return result

    bwjf_marker = extract_bwjf_short_url(body)
    if bwjf_marker:
        result["method"] = "LINK_BWJF"
        result["download_url"] = bwjf_marker
        if result["doc_type"] == "UNKNOWN":
            result["doc_type"] = "TAX_INVOICE"
        return result

    jincai_url = extract_jincai_url(body)
    if jincai_url:
        result["method"] = "LINK_JINCAI"
        result["download_url"] = jincai_url
        if result["doc_type"] == "UNKNOWN":
            result["doc_type"] = "TAX_INVOICE"
        return result

    keruyun_marker = extract_keruyun_short_url(body)
    if keruyun_marker:
        result["method"] = "LINK_KERUYUN"
        result["download_url"] = keruyun_marker
        if result["doc_type"] == "UNKNOWN":
            result["doc_type"] = "TAX_INVOICE"
        return result

    return result


# ─── Hotel Name Extraction ───────────────────────────────────────────

def extract_hotel_name(subject, body, sender, attachment_filenames=None):
    """Extract hotel/merchant name from subject → attachment filename → body → sender.
    Preference order (most specific wins):
      1. subject patterns with brackets or explicit markers
      2. attachment filename (dzfp_{no}_{merchant}_{ts}.pdf)
      3. body patterns ('XXX 为您开具', '销售方：XXX')
      4. sender domain fallback (generic hotel brand)
    """
    # 1. Subject patterns
    m = re.search(r'来自【(.+?)】', subject)
    if m:
        return m.group(1)
    m = re.search(r'入住(.+?)的电子', subject)
    if m:
        return m.group(1)
    m = re.search(r'【(.+?)】开具', subject)
    if m:
        return m.group(1)
    m = re.search(r'E-Folio of (.+?) From', subject, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # 2. Attachment filename
    if attachment_filenames:
        for fn in attachment_filenames:
            got = extract_merchant_from_attachment_filename(fn)
            if got:
                return got

    # 3. Body patterns
    if body:
        got = extract_merchant_from_body(body)
        if got:
            return got

    # 4. Sender domain fallback (brand-level, not specific hotel)
    sender_lower = (sender or "").lower()
    hotel_domains = {
        "hualuxe": "华邑酒店",
        "intercon": "洲际酒店",
        "marriott": "万豪酒店",
        "courtyard": "万怡酒店",
    }
    for domain, name in hotel_domains.items():
        if domain in sender_lower:
            return name

    return None


# ─── File Validation ─────────────────────────────────────────────────

def validate_pdf_header(file_path):
    """Check if a file starts with %PDF magic bytes.
    Returns (is_valid, actual_type_description)."""
    if not os.path.exists(file_path):
        return False, "FILE_NOT_FOUND"
    
    with open(file_path, "rb") as f:
        header = f.read(8)
    
    if header[:4] == b'%PDF':
        return True, "Valid PDF"
    elif header[:2] == b'PK':
        return False, "ZIP/OFD (not PDF)"
    elif b'<!do' in header[:8].lower() or b'<htm' in header[:8].lower():
        return False, "HTML error page"
    elif header[:4] == b'\x89PNG':
        return False, "PNG image"
    elif header[:2] == b'\xff\xd8':
        return False, "JPEG image"
    else:
        return False, f"Unknown ({header[:4].hex()})"


def make_unique_path(output_dir, filename, max_attempts=1000):
    """Generate unique file path with (N) suffix if file already exists.

    Caps at max_attempts to avoid an infinite loop if something pathological
    happens (e.g., a directory with thousands of name collisions). Raises
    RuntimeError on exhaustion so callers see the failure instead of hanging.
    """
    path = os.path.join(output_dir, filename)
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(filename)
    for n in range(1, max_attempts + 1):
        new_path = os.path.join(output_dir, f"{base} ({n}){ext}")
        if not os.path.exists(new_path):
            return new_path
    raise RuntimeError(
        f"cannot find unique path for {filename} in {output_dir} "
        f"after {max_attempts} attempts"
    )


# ─── Unified Naming ──────────────────────────────────────────────────

# ─── Invoice Category Classification from PDF content ──────────────

def classify_invoice_category(pdf_path):
    """Read a Chinese e-invoice PDF and return its service category.

    Returns one of:
      - 'LODGING'    (住宿服务 / 房费)
      - 'DINING'     (餐饮服务 / 餐费 / 食品)
      - 'TRANSPORT'  (交通服务 / 打车 / 客运)
      - 'OTHER'      (anything else, e.g. 洗涤 / 会议room / 商品)
      - 'UNKNOWN'    (parse failed)

    Requires `pdftotext` (poppler) in PATH. Falls back to UNKNOWN on errors.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=10,
        )
        if r.returncode != 0:
            return "UNKNOWN"
        text = r.stdout.decode("utf-8", errors="replace")
    except Exception:
        return "UNKNOWN"

    # Look for *category* markers used in Chinese e-invoices
    # e.g. `*住宿服务*房费` or `*餐饮服务*餐费`
    category_patterns = [
        ("LODGING", r'\*?住宿服务\*?|\*?房费\*?|\*?住宿费\*?'),
        ("DINING", r'\*?餐饮服务\*?|\*?餐费\*?|\*?食品\*?'),
        ("TRANSPORT", r'\*?交通运输\*?|\*?客运服务\*?|\*?网约车\*?|\*?出租汽车\*?'),
    ]
    for cat, pat in category_patterns:
        if re.search(pat, text):
            return cat
    return "OTHER"
# ─── Folio/Invoice Pairing Philosophy ─────────────────────────────
#
# v5.2 rule: one person cannot check in at two hotels on the same day.
# Therefore a 水单 and a \*住宿服务\* invoice sharing the same date belong
# to the same stay — no brand/city normalization needed.
# Dining invoices are NEVER auto-paired (issue date ≠ dining date).
#
# Removed in v5.2: HOTEL_BRAND_KEYWORDS, CITY_KEYWORDS,
# extract_city(), normalize_hotel_brand().


# ─── Unified Naming ────────────────────────────────────────

DOC_TYPE_LABELS = {
    "TAX_INVOICE": "发票",
    "HOTEL_FOLIO": "水单",
    "TRIP_RECEIPT": "行程单",
    "TRAIN_TICKET": "火车票",
    "OTHER_RECEIPT": "收据",
    "UNKNOWN": "文件",
}


def generate_filename(date_str, merchant, doc_type):
    """Generate unified filename: {日期}_{商户}_{类型}.pdf

    Args:
        date_str: Date string in YYYYMMDD format (e.g. '20260319')
        merchant: Merchant/hotel name (e.g. '滴滴出行', '万豪酒店')
        doc_type: Classification type (TAX_INVOICE, HOTEL_FOLIO, etc.)

    Returns:
        Filename string like '20260319_滴滴出行_发票.pdf'
    """
    label = DOC_TYPE_LABELS.get(doc_type, "文件")
    merchant = re.sub(r'[\\/:*?"<>|]', '', merchant).strip()
    if not merchant:
        merchant = "未知商户"
    if not date_str or len(date_str) != 8:
        date_str = "00000000"
    return f"{date_str}_{merchant}_{label}.pdf"


def extract_date_from_email(subject, body, filename="", max_date=None):
    """Try to extract a date (YYYYMMDD) from email subject, body, or attachment filename.

    Ordering:
      1. '开具日期：YYYY年MM月DD日' in body (authoritative)
      2. YYYYMMDD in filename/body/subject (attachment timestamps use this)
      3. YYYY-MM-DD or YYYY/MM/DD
      4. DD/MM/YY or YY/MM/DD (ambiguous) — only used when unambiguous

    `max_date` is a YYYYMMDD string; any extracted date beyond it is rejected
    (invoices should never be in the future relative to 'now'). Default: today (Asia/Shanghai).
    """
    import datetime
    if max_date is None:
        cst = datetime.timezone(datetime.timedelta(hours=8))
        max_date = datetime.datetime.now(tz=cst).strftime("%Y%m%d")

    def _ok(d):
        return d and "20240101" <= d <= max_date

    # 1. Authoritative body date
    if body:
        got = extract_invoice_date_from_body(body)
        if _ok(got):
            return got

    combined = (subject or "") + " " + (body or "") + " " + (filename or "")

    # 2. Compact YYYYMMDD (e.g. 20260319 — strongest signal, common in attachment timestamps)
    for m in re.finditer(r'(20\d{6})', combined):
        if _ok(m.group(1)):
            return m.group(1)

    # 3. YYYY-MM-DD or YYYY/MM/DD
    for m in re.finditer(r'(20\d{2})[-/](\d{1,2})[-/](\d{1,2})', combined):
        got = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
        if _ok(got):
            return got

    # 4. Chinese 'YYYY年MM月DD日'
    for m in re.finditer(r'(20\d{2})年(\d{1,2})月(\d{1,2})日', combined):
        got = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
        if _ok(got):
            return got

    # 5. Ambiguous DD/MM/YY — DROPPED intentionally.
    #    This pattern caused 28/01/26 to be parsed as 2028-01-26 (bug).
    #    Caller should fall back to email internalDate with CST tz instead.
    return None
