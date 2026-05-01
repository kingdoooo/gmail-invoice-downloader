# Chinese Invoice Platform Download Patterns

Reference doc for `gmail-invoice-downloader`. Each section covers how a specific
invoice platform / sender delivers PDFs and what the skill's helper functions
do with them. When adding support for a new platform, document it here first.

**Design principle**: identify by **email content**, not by sender. Invoicing
entities rename/merge frequently; senders come from personal emails, brand
emails, or third-party platforms. The download pattern is the stable signal.

## Table of contents

**Platforms with full PDF support** (classify_email decision tree routes here):

- [百望云 (Baiwang)](#百望云-baiwang--the-big-one) — 3 templates (direct attachment / `pis.baiwang.com` preview / `u.baiwang.com` short link)
- [fapiao.com](#fapiaocom) — direct PDF URL with token
- [xforceplus 平台](#xforceplus-平台-tim-hortons-and-others) — Tim Hortons and others
- [诺诺网 (Nuonuo)](#诺诺网-nuonuo) — SPA + hidden API pattern
- [云票 (bwjf.cn)](#云票-bwjfcn) — `pdfUrl=` query param in redirect
- [百旺金穗云 (广东税务局)](#百旺金穗云-广东税务局) — direct PDF with `Wjgs=PDF`
- [滴滴出行 (Didi)](#滴滴出行-didi) — electronic invoice + trip receipt pairing
- [12306 (火车票/行程报销单)](#12306-火车票行程报销单) — payment notifications filtered out; real tickets via 12306.cn
- [中国移动 (China Mobile)](#中国移动-china-mobile--话费发票) — 话费发票
- [51发票 (51fapiao)](#51发票-51fapiao)
- [票通 (vpiaotong)](#票通-vpiaotong)
- [麦当劳 (McDonald's)](#麦当劳-mcdonalds)
- [Hotel folios (水单/账单)](#hotel-folios-水单账单) — Marriott, Hilton, IHG, various Chinese chains

**Operational sections**:

- [Platforms that CANNOT deliver PDF invoices by email](#platforms-that-cannot-deliver-pdf-invoices-by-email)
- [Adding support for a new platform (reverse engineering playbook)](#adding-support-for-a-new-platform-reverse-engineering-playbook) — the 5-step playbook
- [Cross-platform universal tips](#cross-platform-universal-tips)
- [Sender → platform quick reference](#sender--platform-quick-reference)

---

## 百望云 (Baiwang) — the big one

Covers many hotels, restaurants, and SMEs. **Three distinct email templates**
need separate handling.

### Senders (not reliable — for reference only)

- `*@baiwang.com`, `*@vip.baiwang.com` (e.g. `yun1@vip.baiwang.com`, `yun2@vip.baiwang.com`)
- `fapiao@yun.baiwang.com`
- `系统服务 <yun1@vip.baiwang.com>`

### Template 1: direct PDF attachment

Subject: `您有一张来自【商户名】开具的发票【发票号码：XXX】`

- PDF attachment directly on the email (`dzfp_{发票号}_{商户名}_{YYYYMMDDHHMMSS}.pdf`)
- Download via Gmail API, no URL handling needed
- **Attachment filename is a goldmine for merchant name extraction** — the
  `{商户名}` segment is the authoritative seller. `extract_merchant_from_attachment_filename()`
  exploits this.

### Template 2: `pis.baiwang.com` preview URL (no attachment)

Subject: `电子发票下载`

Body contains a preview URL:
```
https://pis.baiwang.com/smkp-vue/previewInvoiceAllEle?param={HEX}
```

**⚠️ Do NOT curl this URL directly** — it returns HTML (a Vue SPA preview page).

**Correct handling**: extract `{HEX}` from the query string, then construct the
direct download URL:

```
https://pis.baiwang.com/bwmg/mix/bw/downloadFormat?param={HEX}&formatType=pdf
```

`formatType` can be `pdf` / `ofd` / `xml`. Always use `pdf`.

**Frontend source** (for reference):
```javascript
// Vue method: downloadInvoice
function(type) {
  location.href = location.origin + "/bwmg/mix/bw/downloadFormat?param=" + this.param + "&formatType=" + type
}
```

Helper: `extract_baiwang_download_url(body)` returns the fully-constructed
downloadFormat URL when it sees the preview pattern.

### Template 3: `u.baiwang.com` short link (no attachment)

Subject: `电子发票下载`

Body contains ONLY a short link:
```
http://u.baiwang.com/4hJBiMB8dmd
```

**⚠️ `curl -sL` on the short link lands on the Vue preview page (HTML)** — same
dead end as Template 2. The short link redirects to
`pis.baiwang.com/smkp-vue/previewInvoiceAllEle?param=XXX`, not to the PDF.

**Correct handling**: two-step resolution.

1. Request the short URL **without following redirects**; grab the `Location`
   header of the 301 response.
2. Extract the `param=` value from the redirect target.
3. Construct the same `downloadFormat` URL as Template 2.

Helper: `extract_baiwang_download_url()` returns `"BAIWANG_SHORT:{short_url}"`
as a marker. The download pipeline recognizes this prefix and calls
`resolve_baiwang_short_url()` to do the 301 dance, then curls the final URL.

**Why `curl -sL` sometimes "works"**: if the server happens to redirect
through `/downloadFormat` in the chain, you get the PDF. But most of the time
the chain ends at the Vue preview. Always use the two-step resolution.

### Historical pitfall (for context)

> Older versions of `download-invoices.py` filtered URLs to include only
> `u.baiwang.com` but not `pis.baiwang.com`, and never constructed the
> downloadFormat URL. Template 2/3 invoices all failed until this was fixed.

---

## fapiao.com

### Sender

`service@fapiao.com.cn`

### Pattern

Email body contains three format links:
```
发票PDF下载：https://www.fapiao.com/dzfp-web/pdf/download?request={TOKEN}
发票OFD下载：https://www.fapiao.com/DownLoad/ofd/download?request={TOKEN}
发票XML下载：https://www.fapiao.com/DownLoad/xml/download?request={TOKEN}
```

Always pick the **PDF** link (first one). `curl -sL` works directly.

### ⚠️ Token truncation is the #1 bug here

`{TOKEN}` is **100+ characters** and contains URL-encoded chars like `%5E`.
Naive regex `https?://[^\s]+` or `href="([^"]+)"` can truncate the token
mid-string. Truncated URLs return **HTTP 200 + HTML error page "文件下载失败"**
(not 404), so the download script thinks it succeeded until `%PDF` validation
catches the bad file.

**Correct regex** (only exclude terminating chars):
```python
re.findall(r'https://www\.fapiao\.com/dzfp-web/pdf/download\?request=[^\s"<>]+', body)
```

Helper: `extract_fapiao_com_url(body)`.

### Link expiry

Valid for **at least 3 months** in tests. Earlier guesses about "~30 days"
were wrong — the observed "文件下载失败" error was caused by URL truncation,
not expiry.

---

## xforceplus 平台 (Tim Hortons, and others)

### Senders

- `Invoice@store.timschina.com` (Tim Hortons China)
- Other merchants that use xforceplus for e-invoice delivery

### Pattern

Body contains three labeled short links:
```
发票下载地址(PDF)：https://s.xforceplus.com/XXXXX
发票下载地址(XML)：https://s.xforceplus.com/YYYYY
发票下载地址(OFD)：https://s.xforceplus.com/ZZZZZ
```

**Always pick the PDF-labeled one**. Order varies by merchant — some only
provide partial formats. Regex:

```python
re.search(r'(?:PDF|pdf)[）\)]*[：:]\s*(https://s\.xforceplus\.com/\w+)', body)
```

Helper: `extract_xforceplus_pdf_url(body)`.

### ⚠️ Some xforceplus emails also carry a ZIP attachment

The ZIP typically contains OFD + XML only (no PDF). **Ignore the ZIP and find
the PDF link in the body instead** — classification logic in `classify_email`
already does this when the ZIP has no PDF inside.

---

## 诺诺网 (Nuonuo)

### Sender

`invoice@info.nuonuo.com`

### Pattern

Body contains a short link: `https://nnfp.jss.com.cn/{TOKEN}`

The short link redirects through **3 hops** (k0.do → scanUI_k0 → printQrcode) and
finally lands on a Vue SPA preview page. The SPA then makes an API call to fetch
the actual PDF URL.

### Two-step resolution

1. Manually follow 302 chain until the URL contains `paramList={税号}!!!{发票号}!false`
   (Note the `!!!` triple-bang separator)
2. Call the detail API:
   ```
   GET https://nnfp.jss.com.cn/sapi/scan2/getIvcDetailShow.do?paramList={paramList}&aliView=true&shortLinkSource=1&wxApplet=0
   ```
3. Parse JSON response; the PDF URL is at `data.invoiceSimpleVo.url`
   (lives on `inv.jss.com.cn/fp2/{TOKEN}.pdf`, requires no auth once obtained)
4. `curl -sL` that URL → `%PDF` bytes

Helpers: `extract_nuonuo_short_url(body)` returns `"NUONUO_SHORT:{short_url}"` marker.
`resolve_nuonuo_short_url(short_url)` does the 302 chain + API call.

### ⚠️ HEAD requests to short link return 403

The SLB rejects `HEAD` with 403 Forbidden, but `GET` returns a proper 302.
Use `http.client.HTTPSConnection.request("GET", path)` and inspect the
`Location` header without letting urllib auto-follow.

---

## 云票 (bwjf.cn)

### Sender

`fapiao@yjts.bwjf.cn`

### Pattern

Body contains short link: `https://fp.bwjf.cn/u/{TOKEN}`

Short link 302-redirects to a success page URL where the PDF download URL is
embedded as a query parameter:

```
https://www.bwjf.cn/allEleDeliverySuccess?...&pdfUrl={URLEncoded-download-URL}&ofdUrl=...&xmlUrl=...
```

### Resolution

1. Follow single 302 on short link, grab Location header
2. Parse query string, extract `pdfUrl` parameter (`urllib.parse.parse_qs` auto-decodes)
3. **Re-encode the extracted URL** because the decoded value contains raw Chinese
   characters (e.g. `sellerName=杭州余杭区仓前街道乔村餐厅`) that `urllib.request`
   rejects as "control characters"
4. `curl -sL` or `urllib.urlopen` the safe URL

Helpers: `extract_bwjf_short_url(body)` returns `"BWJF_SHORT:{short_url}"` marker.
`resolve_bwjf_short_url(short_url)` does the resolve + re-encode dance.

### ⚠️ Re-encoding is mandatory

```python
# WRONG — fails with InvalidURL
urllib.request.urlopen(pdf_url_decoded)

# RIGHT — rebuild query string with proper percent-encoding
p = urlparse(pdf_url)
new_query = urlencode(parse_qsl(p.query))
safe_url = urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))
```

---

## 百旺金穗云 (广东税务局)

### Sender

`gdbwjf.dzfp@gdfapiao.com`

### Pattern

Body contains three direct download URLs (no short link, no 302 chain!):

```
https://dppt.guangdong.chinatax.gov.cn:8443/kpfw/fpjfzz/v1/exportDzfpwjEwm?Wjgs=PDF&Jym=E4AB&Fphm=25442000000673831496&Kprq=20251031093346&Czsj=1761874431758
https://dppt.guangdong.chinatax.gov.cn:8443/kpfw/fpjfzz/v1/exportDzfpwjEwm?Wjgs=OFD&...
https://dppt.guangdong.chinatax.gov.cn:8443/kpfw/fpjfzz/v1/exportDzfpwjEwm?Wjgs=XML&...
```

`Wjgs=PDF` → 文件格式=PDF。Pick that URL, `curl -sL` it directly.

### ⚠️ Must preserve all query parameters

The URL requires `Jym` (verification code), `Fphm` (invoice number), `Kprq`
(issue date), and `Czsj` (operation timestamp). Dropping any of them returns:

```json
{"Response": {"Result": "FAIL", ...}}
```

Helper: `extract_gdbwjf_url(body)` — simple regex, no two-step resolution needed.

### Note: hosted on 广东省税务局 official domain

`dppt.guangdong.chinatax.gov.cn:8443` is a government tax platform. URLs are
stable and don't seem to expire quickly.

---

## 滴滴出行 (Didi)

### Sender

`didifapiao@mailgate.xiaojukeji.com`

### Pattern

Two PDF attachments per email: invoice + trip receipt (行程报销单). Both come
directly via Gmail API.

### Naming

Multiple orders on the same day produce multiple emails with identical subjects
("滴滴出行电子发票及行程报销单"). `generate_filename()` appends `-1`/`-2`
for multi-attachment emails, and `make_unique_path()` adds `(1)`/`(2)` for
filename collisions across emails.

---

## 12306 (火车票/行程报销单)

### Sender

`12306@rails.com.cn`

### Pattern — TWO different email types

**Type A: 支付通知**
- Subject: `网上购票系统-用户支付通知`
- **No PDF attachment** — just an HTML receipt of the payment
- **Not a reimbursable invoice**
- `classify_email` returns `doc_type=IGNORE` + `method=IGNORE`; also covered
  by the `-subject:用户支付通知` exclusion rule in `learned_exclusions.json`

**Type B: 行程报销单 (actual ticket)**
- Has PDF or ZIP attachment
- `classify_email` returns `doc_type=TRAIN_TICKET`, downloaded normally
- In practice, 12306 **rarely sends Type B by email** — users usually need to
  log in to 12306.cn and download the 行程报销单 manually

### Historical pitfall

17 Type A emails in one batch were all classified as TRAIN_TICKET → MANUAL
(because there was no attachment to download). Fixed by adding the "no
attachment → IGNORE" guard specifically for the 12306 sender.

---

## 中国移动 (China Mobile) — 话费发票

### Sender

`10086@139.com`

### Pattern

ZIP attachment containing PDF + OFD + XML. `extract_pdfs_from_zip()` keeps
only `.pdf`, discards everything else.

### Merchant extraction

The subject is generic (`【移动发票】您的电子发票已送达，敬请查阅！`) and has no
seller name. Body contains: `开票单位：中国移动通信集团湖北有限公司`.

`extract_merchant_from_body()` now includes a `开票单位：XXX` pattern for
exactly this case.

---

## 51发票 (51fapiao)

### Sender

`dzfp@51fapiao.cloud`

### Pattern

PDF and ZIP attachments directly in email. Gmail API works.

### Subject clue

`【电子发票】您收到一张来自【销售方】...` — `extract_hotel_name()`'s
`【XXX】` pattern catches the merchant.

---

## 票通 (vpiaotong)

### Sender

`kefu@service.vpiaotong.com`

### Pattern

PDF attachment directly. Subject: `您收到一张来自XXX的电子发票【发票金额：XXX】`.

---

## 麦当劳 (McDonald's)

### Sender

`e-invoice@mcd.cn`

### Pattern

PDF attachment directly. Subject is generic `【电子发票】您收到一张新的电子发票[发票号码:XXX]`
— merchant comes from sender domain fallback.

---

## Hotel folios (水单/账单)

**Unlike invoices, folios are sender-diverse and platform-agnostic.**

### Sender formats seen

- `mhrs.*.gsm@marriott.com` (Marriott brand sys emails; `*` = property code, e.g. `mhrs.wuxml.gsm` = Wuxi Marriott Lihu)
- Hotel front desk emails: `dm@xxx.com`, `fd@xxx.com`, `Sunny.Liu2@marriott.com`
- Individual staff emails: `17768335659@163.com`, `xxx@qq.com`
- `Duty Manager <dm@hualuxewuxi.com>`

### Identification (by content, not sender)

- Subject contains `水单` / `账单` / `folio` / `e-folio` → HOTEL_FOLIO
- `E-Folio of Marriott Nanjing South Hotel From 28/01/26 To 29/01/26` → hotel name from subject
- Empty subject + attachment like `wuxml_folio_ef_sj_gc984773374.pdf` → hotel from sender domain
  - `wuxml` → Wuxi Marriott Lihu Lake
  - `wuxll` → Wuxi Courtyard
  - `wuxlp` → Wuxi Liangxi Four Points

### ⚠️ Not all marriott.com emails are folios

Exclude these subjects (already in `learned_exclusions.json`):
- `预订确认` / `预订取消` / `客房升级` (reservation confirmations)
- `退票` / `改签` / `还款` / `贷款`
- `eStatement` / `月结单`

### Pairing with invoices

v5.2 rule: same-date `HOTEL_FOLIO` + `LODGING`-category invoice = same stay.
See SKILL.md § Step 7 for the full rationale (tl;dr: one person can't check
in at two hotels on the same day, so date alone is sufficient).

---

## Platforms that CANNOT deliver PDF invoices by email

These always require manual download from the platform's own website/app:

| Sender | Platform | Where to get the PDF |
|--------|----------|----------------------|
| `no_reply@email.apple.com` | Apple | HTML receipts only; invoices require invoice.apple.com |
| `12306@rails.com.cn` (支付通知) | 12306 | Log in to 12306.cn → 行程信息 → 下载行程报销单 |
| `googleplay-noreply@google.com` | Google Play | Google Wallet / Play Orders page |

Already in the exclusion list.

---

## Adding support for a new platform (reverse engineering playbook)

New Chinese invoice platforms pop up faster than anyone can document. When
`download-invoices.py` reports `MANUAL` for a sender you haven't seen,
follow this playbook to reverse-engineer the download URL **yourself**.

### Prerequisites (one-time setup)

- **Chrome/Chromium with CDP** (Chrome DevTools Protocol) for JS-heavy pages:
  ```bash
  # OpenClaw's built-in browser tool handles this. Verify with:
  #   browser(action=status)
  # If `running: false`, start it with:
  #   browser(action=start)
  ```
- **Playwright's Chromium** is what OpenClaw uses by default
  (`~/.cache/ms-playwright/chromium-*/chrome-linux/chrome`).
  To install manually on a fresh host:
  ```bash
  pip install playwright --break-system-packages
  playwright install chromium
  ```
- Alternative lighter-weight tools: `curl -sIL` for 302 chain inspection,
  `python -c "import urllib.request, http.client; ..."` for non-redirect-following
  HEAD/GET to inspect Location headers directly.

### Step 1 — Inspect the email body

```python
from invoice_helpers import get_body_text
import re

body = get_body_text(msg["payload"])  # from Gmail API full-fetch
urls = re.findall(r'https?://[^\s<>"\']+', body)

# Drop tracking/CDN/image URLs
invoice_urls = [u for u in urls
                if not any(x in u for x in ['sendcloud', 'track', 'oss', 'aliyun',
                                             '.png', '.jpg', '.gif',
                                             'baidu.com', 'google.com'])]
```

Print these URLs and look for:
- **Direct format markers**: `?format=pdf`, `Wjgs=PDF`, `jflx=pdf`, `_pdf`, `.pdf`
- **Short link domains**: 2-3 character paths on `fp.xxx.com`, `u.xxx.com`, `s.xxx.com`
- **Seller name / invoice number in URL path or query** (a strong hint there's an API waiting)

### Step 2 — Probe with curl first

Before firing up a browser, try non-redirecting requests:

```bash
# HEAD often gets 403 from invoice platforms (anti-bot)
curl -sI --max-time 10 -H "User-Agent: Mozilla/5.0" "$URL"

# GET without following redirects reveals the Location chain
curl -sIL --max-time 10 -H "User-Agent: Mozilla/5.0" "$URL" 2>&1 | grep -iE '^(Location|HTTP/)'
```

Three possible outcomes:

| What you see | Meaning | Next step |
|--------------|---------|-----------|
| `Location: https://.../xxx.pdf` | Direct PDF, done | `curl -sL` + validate `%PDF` header |
| `Location: https://.../...?paramList=XXX!!!YYY!false` | Nuonuo / bwfp pattern (3-hop) | Find the detail API (see Step 3) |
| `Location: https://www.bwjf.cn/...?pdfUrl=URLENCODED` | bwjf pattern | Extract `pdfUrl` query param |
| `HTTP/2 200 text/html` with SPA markup | JavaScript app, needs browser | Go to Step 3 (browser) |
| `HTTP/1.1 403 Forbidden` on HEAD but 302 on GET | Anti-bot: HEAD blocked only | Use GET in production code |

### Step 3 — Use the browser to find the hidden API

When the SPA loads, it fetches the invoice detail from some API endpoint.
Capture that call:

```python
# 1. Open the short link
browser(action="open", url="https://platform.example.com/{TOKEN}")

# 2. Wait for the page to hydrate
browser(action="act",
        request={"kind": "wait", "timeMs": 3000},
        targetId=target)

# 3. List the XHR/fetch requests the page made
browser(action="act",
        request={
            "kind": "evaluate",
            "fn": """() => performance.getEntriesByType('resource')
                        .filter(r => r.initiatorType === 'xmlhttprequest'
                                  || r.initiatorType === 'fetch')
                        .map(r => r.name)"""
        },
        targetId=target)
```

Look for endpoints like:
- `/sapi/scan2/getIvcDetailShow.do` (Nuonuo)
- `/sapi/invoice/issue-scan/get-detail.do` (bwfp)
- `/kpfw/fpjfzz/v1/exportDzfpwjEwm` (China Tax)
- `/api/invoice/detail` or similar

### Step 4 — Replay the API call

```python
# Call the detail API with the paramList you extracted from the redirect chain
browser(action="act",
        request={
            "kind": "evaluate",
            "fn": f"""async () => {{
              const res = await fetch('{api_url}?paramList={param_list}',
                                     {{credentials: 'include'}});
              return {{status: res.status, text: (await res.text()).substring(0, 3000)}};
            }}"""
        })
```

The JSON response usually contains `data.invoiceSimpleVo.url` or similar
field that is **the real PDF URL**. Fetch that and save.

### Step 5 — Write it up

Once the download works, codify it:

1. Add `extract_<platform>_url(body)` to `invoice_helpers.py` — returns a
   marker like `"PLATFORM_SHORT:{short_url}"` for 2-step, or the direct URL
2. Add `resolve_<platform>_short_url(url)` if 2-step is needed
3. Add `LINK_<PLATFORM>` to `classify_email`'s decision tree
4. Add `resolve_<platform>_short_url` to `download-invoices.py`'s import list
5. Add the `LINK_<PLATFORM>` case to `download-invoices.py`'s method dispatch
6. Document the quirks here in `platforms.md` following the format of existing sections

### 🔑 Recurring patterns worth knowing

After reverse-engineering 9 platforms, a few patterns show up repeatedly:

1. **`paramList={税号}!!!{发票号}!false`** — Nuonuo, bwfp.baiwang.com
   both use this as the "invoice identifier". The `!!!` triple-bang seems to
   be a Chinese dev shop convention.
2. **`Wjgs=PDF|OFD|XML`** — the China Tax Bureau API (`chinatax.gov.cn`) uses
   this; if you see `Wjgs=OFD`, flip to `Wjgs=PDF` and the same URL works.
3. **`_pdf` / `_ofd` / `_xml` suffix** — jcsk100 uses filename suffixes for
   format. Same substitute-and-fetch trick.
4. **Vue SPA preview page** — if a short link lands on an HTML page titled
   "诺诺发票" / "百望发票" / similar, the SPA almost certainly has a JSON API
   that returns the real PDF URL. Use browser capture to find it.
5. **User-Agent header** — some platforms 403 without a browser UA string.
   Always send `User-Agent: Mozilla/5.0` in probes.
6. **Keep all query params** — for China Tax URLs, dropping any of
   `Jym/Fphm/Kprq/Czsj` causes the API to return a JSON error instead of a PDF.

---

## Cross-platform universal tips

### 1. Tracking links expire, display text does not

Email marketing platforms (SendCloud, Mailchimp, etc.) wrap `<a href="...">`
in tracking redirects that expire in 7-30 days. The `<a>` tag's **display
text** often contains the real URL.

```python
for match in re.finditer(r'<a[^>]*>(.*?)</a>', html, re.DOTALL):
    link_text = re.sub(r'<[^>]+>', '', match.group(1)).strip()
    if re.match(r'https?://', link_text):
        real_urls.append(link_text)
```

All `extract_*_url()` helpers in `invoice_helpers.py` check both href AND
display text.

### 2. QR codes as backup

Some platforms embed base64 QR code images. When URLs are gone, decoding the
QR may recover a direct download URL (requires `pyzbar` + `pillow`). Not
currently implemented — falls through to MANUAL.

### 3. Chinese e-invoices come as triples

PDF + OFD + XML. Reimbursement only needs PDF.
`extract_pdfs_from_zip()` and all link selection logic prefer PDF-specific
URLs/files.

### 4. Always validate `%PDF` magic bytes

Chinese platforms respond to invalid URLs with **HTTP 200 + `text/html`
content-type + Vue SPA page** that renders "文件下载失败". The HTTP status
alone is useless; only the file header tells the truth.

| Header (hex) | Actual content | Common cause |
|--------------|----------------|--------------|
| `25504446` (`%PDF`) | ✅ Valid PDF | — |
| `3c21646f` / `3c21444f` (`<!do` / `<!DO`) | HTML | Preview page, not download URL |
| `504b0304` (`PK..`) | ZIP/OFD | Wrong format picked |
| `89504e47` (`.PNG`) | PNG | QR code image |
| `ffd8` | JPEG | Image, not PDF |

`validate_pdf_header()` decodes these and reports back to the caller.

### 5. Platform identification first, generic fallback last

Before generic URL extraction, try platform-specific patterns. Generic
extraction often picks tracking/preview URLs and produces HTML.

Order in `classify_email`:
1. Sender-specific (12306)
2. Body contains fapiao.com PDF URL
3. Body contains pis.baiwang.com preview
4. Body contains u.baiwang.com short link
5. Body contains xforceplus `(PDF)`-labeled link
6. Otherwise MANUAL

---

## Sender → platform quick reference

**⚠️ Reference only — never hard-rule on sender. Always verify by content.**

| Sender pattern | Platform | Method |
|----------------|----------|--------|
| `*@baiwang.com` / `*@vip.baiwang.com` | 百望云 | PDF attachment (T1) or link extraction (T2/T3) |
| `service@fapiao.com.cn` | fapiao.com | PDF link in body, long token |
| `invoice@info.nuonuo.com` | 诺诺网 | Short link → 302 chain → API call → PDF URL |
| `fapiao@yjts.bwjf.cn` | 云票 (bwjf) | Short link → 302 → pdfUrl query param → re-encode |
| `gdbwjf.dzfp@gdfapiao.com` | 百旺金穗云 (广东税务) | Direct PDF URL in body, `Wjgs=PDF` |
| `Invoice@store.timschina.com` | xforceplus | Labeled link in body |
| `didifapiao@mailgate.xiaojukeji.com` | 滴滴 | PDF attachment (invoice + trip receipt) |
| `10086@139.com` | 中国移动 | ZIP with PDF + OFD |
| `dzfp@51fapiao.cloud` | 51发票 | PDF attachment |
| `kefu@service.vpiaotong.com` | 票通 | PDF attachment |
| `e-invoice@mcd.cn` | 麦当劳 | PDF attachment |
| `mhrs.*.gsm@marriott.com` | Marriott | PDF folio attachment |
| `dm@*` / `fd@*` / personal emails | Hotel front desk | PDF folio or invoice attachment |
| `12306@rails.com.cn` + `支付通知` subject | 12306 | IGNORE (not an invoice) |
| `no_reply@email.apple.com` | Apple | Excluded (no PDF) |
| `googleplay-noreply@google.com` | Google Play | Excluded |
| Bank / brokerage domains | — | Excluded (not reimbursable) |
