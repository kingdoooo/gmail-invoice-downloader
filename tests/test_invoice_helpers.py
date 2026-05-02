"""Tests for scripts/invoice_helpers.py — platform URL extractors."""
from invoice_helpers import extract_nuonuo_short_url


class TestNuonuoShortURLExtraction:
    def test_real_short_link_wins_over_qr_image_url(self):
        """Regression: /allow/service/getEwmImg.do was being picked as `…/allow`.

        Body contains both the QR image URL (with /allow path) and the real
        short link. Extractor must return the real short link.
        """
        body = (
            '<img src="https://nnfp.jss.com.cn/allow/service/getEwmImg.do'
            '?content=https://nnfp.jss.com.cn/atmQ9sGqZg-14Zm6">'
            '<a href="foo">https://nnfp.jss.com.cn/atmQ9sGqZg-14Zm6</a>'
        )
        result = extract_nuonuo_short_url(body)
        assert result == "NUONUO_SHORT:https://nnfp.jss.com.cn/atmQ9sGqZg-14Zm6"

    def test_returns_none_when_only_api_paths_present(self):
        """Defensive: body has only the QR image URL, no real short link.

        Pre-v5.5 this would return the /allow URL and downstream would fail
        with 'failed to resolve nuonuo short link'. v5.5 returns None so
        the email is (correctly) categorized as unresolved.
        """
        body = (
            '<img src="https://nnfp.jss.com.cn/allow/service/getEwmImg.do?'
            'content=xyz">'
        )
        result = extract_nuonuo_short_url(body)
        assert result is None

    def test_scan_invoice_spa_route_still_excluded(self):
        """Regression guard for pre-v5.5 filter (/scan-invoice/ SPA routes)."""
        body = (
            '<a>https://nnfp.jss.com.cn/scan-invoice/printQrcode?paramList=xyz</a>'
            '<a>https://nnfp.jss.com.cn/atmQ9sGqZg-14Zm6</a>'
        )
        result = extract_nuonuo_short_url(body)
        assert result == "NUONUO_SHORT:https://nnfp.jss.com.cn/atmQ9sGqZg-14Zm6"

    def test_empty_body_returns_none(self):
        assert extract_nuonuo_short_url("") is None
        assert extract_nuonuo_short_url("no nuonuo urls here") is None
