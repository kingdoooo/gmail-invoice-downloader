#!/usr/bin/env python3
"""
probe-platform.py — Reverse engineer an unknown invoice platform.

Run this on any URL from a MANUAL email (classify_email failed to handle it)
to get a diagnosis printout: what kind of platform it is, what API (if any)
the SPA calls, and guesses for the real PDF URL.

Usage:
    python3 probe-platform.py <url>

Example:
    python3 probe-platform.py "https://nnfp.jss.com.cn/97zRdPcuZg-KYHJ"
    python3 probe-platform.py "https://fp.bwjf.cn/u/1UgpI3A1D6J"

Requires: standard library only. For Step 3 (SPA API inspection), you need
OpenClaw's `browser` tool available (not covered by this standalone script;
see references/platforms.md Step 3 for the browser-based workflow).
"""
import argparse
import http.client
import json
import re
import sys
import urllib.parse


def probe_redirect_chain(url, max_hops=8):
    """Follow 30x redirects manually (no auto-follow), return list of steps."""
    chain = []
    current = url
    for hop in range(max_hops):
        try:
            p = urllib.parse.urlparse(current)
            cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
            conn = cls(p.netloc, timeout=15)
            path_q = p.path + ('?' + p.query if p.query else '')
            conn.request("GET", path_q, headers={"User-Agent": "Mozilla/5.0"})
            resp = conn.getresponse()
            content_type = resp.getheader('Content-Type', '')
            location = resp.getheader('Location')
            status = resp.status
            chain.append({
                "hop": hop,
                "status": status,
                "url": current,
                "location": location,
                "content_type": content_type,
            })
            conn.close()
            if not location or status not in (301, 302, 303, 307, 308):
                break
            if location.startswith('/'):
                location = f"{p.scheme}://{p.netloc}{location}"
            current = location
        except Exception as e:
            chain.append({"hop": hop, "url": current, "error": str(e)})
            break
    return chain


def analyze_url(url):
    """Classify the URL by heuristics and propose a download strategy."""
    hints = []

    # Direct PDF signals
    if url.lower().endswith('.pdf'):
        hints.append("✅ URL 以 `.pdf` 结尾 — 很可能直接就是 PDF，curl -sL 即可")
    if 'Wjgs=PDF' in url:
        hints.append("✅ 含 `Wjgs=PDF` — 中国税务局平台 PDF 直链")
    if url.endswith('_pdf'):
        hints.append("✅ 以 `_pdf` 结尾 — jcsk100 风格直链")
    if '/downloadFormat' in url and 'formatType=pdf' in url:
        hints.append("✅ 百望云 downloadFormat API — 直接 curl")

    # Format-flip opportunities
    if 'Wjgs=OFD' in url or 'Wjgs=XML' in url:
        flipped = re.sub(r'Wjgs=(?:OFD|XML)', 'Wjgs=PDF', url)
        hints.append(f"💡 含 `Wjgs=OFD/XML` — 试试改成 PDF：\n    {flipped}")
    if url.endswith('_ofd') or url.endswith('_xml'):
        flipped = re.sub(r'_(?:ofd|xml)$', '_pdf', url)
        hints.append(f"💡 以 `_ofd/_xml` 结尾 — 改成 _pdf 试试：\n    {flipped}")

    # Known-good markers
    if re.search(r'https?://[\w.]+/kpfw/fpjfzz/v1/exportDzfpwjEwm', url):
        hints.append("🏛️  中国税务局平台（dppt.*.chinatax.gov.cn 或兼容）")
    if 'pis.baiwang.com/smkp-vue' in url:
        hints.append("🔵 百望云 pis 预览 — 提取 param 后构造 downloadFormat")
    if 'u.baiwang.com/' in url:
        hints.append("🔵 百望云 u.baiwang.com 短链 — 跟 301 拿 param")
    if 'bwfp.baiwang.com/' in url:
        hints.append("🔵 百望云 bwfp 短链 — 跟 302 链 + issue-scan API")
    if 'nnfp.jss.com.cn' in url:
        hints.append("🟢 诺诺网短链 — 跟 302 链 + getIvcDetailShow API")
    if 'fp.bwjf.cn/u/' in url:
        hints.append("🟡 云票短链 — 跟一次 302 拿 pdfUrl query 参数")
    if 'jcsk100.com' in url or 'invoice.keruyun.com' in url:
        hints.append("🟠 金财数科/克如云 — 直链或 302 到 jcsk100")

    return hints


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="Suspicious URL from an invoice email")
    args = ap.parse_args()

    print("=" * 70)
    print(f"Probing: {args.url}")
    print("=" * 70)

    # Heuristic analysis
    print("\n🔎 Heuristic analysis")
    print("-" * 70)
    hints = analyze_url(args.url)
    if hints:
        for h in hints:
            print(f"  {h}")
    else:
        print("  (no known pattern matched — proceed to redirect probe)")

    # Redirect chain
    print("\n🔗 Redirect chain (follow GET, don't auto-redirect)")
    print("-" * 70)
    chain = probe_redirect_chain(args.url)
    for step in chain:
        if 'error' in step:
            print(f"  [{step['hop']}] ERROR: {step['error']}")
            continue
        print(f"  [{step['hop']}] HTTP {step['status']}  {step.get('content_type', '')[:50]}")
        print(f"        {step['url'][:110]}")
        if step.get('location'):
            print(f"        → Location: {step['location'][:110]}")

    final = chain[-1] if chain else None
    if not final:
        print("\n❌ Unable to probe the URL.")
        return

    # Analyze final URL
    final_url = final['url']
    final_ct = final.get('content_type', '')
    print(f"\n🎯 Final URL: {final_url[:120]}")
    print(f"   Content-Type: {final_ct}")

    if 'pdf' in final_ct.lower() or final_url.lower().endswith('.pdf'):
        print("\n✅ Verdict: DIRECT PDF. Download with:")
        print(f"   curl -sL --max-time 60 -H 'User-Agent: Mozilla/5.0' -o out.pdf '{final_url}'")
        print("   Don't forget to validate `%PDF` header.")
        return

    # Parse query params for common patterns
    final_q = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)

    # Pattern: pdfUrl query param (bwjf)
    if 'pdfUrl' in final_q:
        pdf_url = final_q['pdfUrl'][0]
        print(f"\n✅ Verdict: URL has `pdfUrl` query param (bwjf-style).")
        print(f"   Raw: {pdf_url[:120]}")
        print(f"   Re-encode Chinese chars and fetch it.")
        return

    # Pattern: paramList (Nuonuo / bwfp)
    if 'paramList' in final_q:
        param = final_q['paramList'][0]
        print(f"\n🔵 Verdict: `paramList={param}` detected (Nuonuo/bwfp-style).")
        print(f"   Likely API (try these in order):")
        print(f"     {urllib.parse.urlparse(final_url).netloc}/sapi/scan2/getIvcDetailShow.do?paramList=...")
        print(f"     {urllib.parse.urlparse(final_url).netloc}/sapi/invoice/issue-scan/get-detail.do?paramList=...")
        print(f"   Call the right API, then extract data.invoiceSimpleVo.url.")
        return

    # HTML SPA case
    if 'html' in final_ct.lower():
        print("\n📱 Verdict: Landed on an HTML/SPA page. Next step:")
        print("   1. Use OpenClaw `browser` tool to open the URL")
        print("   2. browser(action='act', request={'kind':'evaluate',")
        print("      'fn': \"() => performance.getEntriesByType('resource').filter(r => r.initiatorType === 'xmlhttprequest').map(r => r.name)\" })")
        print("   3. Look for `*.do`, `/api/`, `/sapi/` URLs in the list")
        print("   4. Replay one of those with fetch() to find the PDF URL")
        print("   See references/platforms.md § Adding support for a new platform, Step 3.")
        return

    print("\n❓ Verdict: unknown pattern. Inspect manually.")


if __name__ == "__main__":
    main()
