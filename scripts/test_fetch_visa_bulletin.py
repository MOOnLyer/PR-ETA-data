#!/usr/bin/env python3
"""Network-routing regression tests for fetch_visa_bulletin.py."""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import fetch_visa_bulletin as bulletin


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body.encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def http_error(request, code):
    return urllib.error.HTTPError(
        request.full_url, code, f"HTTP {code}", {}, io.BytesIO(b"error"))


def valid_bulletin_html(year, month):
    """A complete page fixture that satisfies the downloader's integrity checks."""
    name = bulletin.MONTH_NAMES[month - 1].title()

    def table(identity, section, labels):
        rows = "".join(f"<tr><td>{label}</td><td>C</td></tr>" for label in labels)
        return (
            f"<table id=\"{identity}\">"
            f"<tr><td>{section}</td>"
            "<td>All Chargeability Areas Except Those Listed</td></tr>"
            f"{rows}</table>"
        )

    family_labels = ["F1", "F2A", "F2B", "F3", "F4"]
    employment_labels = ["1st", "2nd", "3rd", "4th", "5th Unreserved"]
    parts = [
        f"<html><head><title>Visa Bulletin For {name} {year}</title></head><body>",
        "<p>final action date</p>",
        table("family-final", "Family-Sponsored", family_labels),
        "<p>final action date</p>",
        table("employment-final", "Employment-Based", employment_labels),
    ]
    if (year, month) >= (2015, 10):
        parts.extend([
            "<p>dates for filing</p>",
            table("family-filing", "Family-Sponsored", family_labels),
            "<p>dates for filing</p>",
            table("employment-filing", "Employment-Based", employment_labels),
        ])
    parts.append("</body></html>")
    return "".join(parts)


class FetchPageTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _record(self, request, timeout):
        self.calls.append((request, timeout))
        return urllib.parse.urlsplit(request.full_url)

    def _fetch(self, year, month, cache_dir=None, retries=3, scraperapi=None):
        with redirect_stderr(io.StringIO()):
            return bulletin.fetch_page(
                year, month, cache_dir, retries=retries, scraperapi=scraperapi)

    def test_primary_403_uses_official_fallback_without_retrying(self):
        expected = valid_bulletin_html(2026, 8)

        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            if parsed.hostname == "travel.state.gov":
                raise http_error(request, 403)
            if parsed.hostname == "childabduction.state.gov":
                return FakeResponse(expected)
            self.fail(f"unexpected host: {parsed.hostname}")

        with patch.object(bulletin.urllib.request, "urlopen", side_effect=open_url):
            body = self._fetch(2026, 8)

        self.assertEqual(body, expected)
        self.assertEqual(
            [urllib.parse.urlsplit(call[0].full_url).hostname for call in self.calls],
            ["travel.state.gov", "childabduction.state.gov"])

    def test_primary_404_advances_slug_without_paid_fallback(self):
        expected = valid_bulletin_html(2012, 10)

        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            if "visa-bulletin-for-" in parsed.path:
                raise http_error(request, 404)
            if parsed.hostname == "travel.state.gov":
                return FakeResponse(expected)
            self.fail(f"unexpected request: {request.full_url}")

        with patch.dict(os.environ, {"SCRAPERAPI_KEY": "top-secret"}):
            with patch.object(bulletin.urllib.request, "urlopen", side_effect=open_url):
                body = self._fetch(2012, 10)

        self.assertEqual(body, expected)
        self.assertEqual(
            [urllib.parse.urlsplit(call[0].full_url).hostname for call in self.calls],
            ["travel.state.gov", "travel.state.gov"])

    def test_scraperapi_is_last_resort_and_key_is_only_in_header(self):
        api_key = "top-secret"
        expected = valid_bulletin_html(2026, 8)

        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            if parsed.hostname in {"travel.state.gov", "childabduction.state.gov"}:
                raise http_error(request, 403)
            if parsed.hostname == "api.scraperapi.com":
                return FakeResponse(expected)
            self.fail(f"unexpected host: {parsed.hostname}")

        with patch.dict(os.environ, {"SCRAPERAPI_KEY": api_key}):
            with patch.object(bulletin.urllib.request, "urlopen", side_effect=open_url):
                body = self._fetch(2026, 8)

        self.assertEqual(body, expected)
        self.assertEqual(
            [urllib.parse.urlsplit(call[0].full_url).hostname for call in self.calls],
            ["travel.state.gov", "childabduction.state.gov", "api.scraperapi.com"])
        proxy_request, proxy_timeout = self.calls[-1]
        proxy_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(proxy_request.full_url).query)
        proxy_headers = {key.lower(): value for key, value in proxy_request.header_items()}
        self.assertEqual(proxy_query["url"], [bulletin.bulletin_urls(2026, 8)[0]])
        self.assertNotIn("api_key", proxy_query)
        self.assertNotIn(api_key, proxy_request.full_url)
        self.assertEqual(proxy_headers["x-sapi-api_key"], api_key)
        self.assertEqual(proxy_headers["x-sapi-premium"], "true")
        self.assertEqual(proxy_headers["x-sapi-max_cost"], "10")
        self.assertEqual(proxy_timeout, 75)

    def test_scraperapi_refuses_non_state_targets(self):
        with self.assertRaisesRegex(ValueError, "refusing to proxy non-State URL"):
            bulletin._download_via_scraperapi(
                "https://example.com/private", "top-secret")

    def test_proxy_is_not_used_when_official_fallback_says_404(self):
        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            if parsed.hostname == "travel.state.gov":
                raise http_error(request, 403)
            if parsed.hostname == "childabduction.state.gov":
                raise http_error(request, 404)
            self.fail(f"paid fallback should not be called: {request.full_url}")

        with patch.dict(os.environ, {"SCRAPERAPI_KEY": "top-secret"}):
            with patch.object(bulletin.urllib.request, "urlopen", side_effect=open_url):
                body = self._fetch(2026, 9)

        self.assertIsNone(body)
        self.assertEqual(len(self.calls), 4)  # two slugs, one request per official host

    def test_missing_scraperapi_key_fails_gracefully_after_official_routes(self):
        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            if parsed.hostname in {"travel.state.gov", "childabduction.state.gov"}:
                raise http_error(request, 403)
            self.fail(f"unconfigured paid fallback was called: {request.full_url}")

        with patch.dict(os.environ, {"SCRAPERAPI_KEY": ""}):
            with patch.object(bulletin.urllib.request, "urlopen", side_effect=open_url):
                body = self._fetch(2026, 8)

        self.assertIsNone(body)
        self.assertEqual(len(self.calls), 4)  # two slugs, one request per official host

    def test_non_403_outage_does_not_activate_paid_fallback(self):
        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            if parsed.hostname in {"travel.state.gov", "childabduction.state.gov"}:
                raise urllib.error.URLError("temporary outage")
            self.fail(f"paid fallback should require a bot-block signal: {request.full_url}")

        with patch.dict(os.environ, {"SCRAPERAPI_KEY": "top-secret"}):
            with patch.object(bulletin.urllib.request, "urlopen", side_effect=open_url):
                body = self._fetch(2026, 8, retries=1)

        self.assertIsNone(body)
        self.assertEqual(len(self.calls), 4)

    def test_paid_fallback_has_a_shared_per_run_request_limit(self):
        fallback = bulletin.ScraperAPIFallback("top-secret", max_requests=1)

        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            if parsed.hostname in {"travel.state.gov", "childabduction.state.gov"}:
                raise http_error(request, 403)
            if parsed.hostname == "api.scraperapi.com":
                raise http_error(request, 500)
            self.fail(f"unexpected host: {parsed.hostname}")

        with patch.object(bulletin.urllib.request, "urlopen", side_effect=open_url):
            body = self._fetch(2026, 8, retries=1, scraperapi=fallback)

        self.assertIsNone(body)
        self.assertEqual(fallback.remaining, 0)
        self.assertEqual(sum(
            urllib.parse.urlsplit(request.full_url).hostname == "api.scraperapi.com"
            for request, _timeout in self.calls), 1)

    def test_wrong_month_page_is_rejected_before_caching(self):
        wrong = valid_bulletin_html(2026, 7)
        expected = valid_bulletin_html(2026, 8)

        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            if parsed.hostname == "travel.state.gov":
                return FakeResponse(wrong)
            if parsed.hostname == "childabduction.state.gov":
                return FakeResponse(expected)
            self.fail(f"unexpected host: {parsed.hostname}")

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            with patch.object(bulletin.urllib.request, "urlopen", side_effect=open_url):
                body = self._fetch(2026, 8, cache_dir)
            cached = (cache_dir / "2026-08.html").read_text()

        self.assertEqual(body, expected)
        self.assertEqual(cached, expected)
        self.assertNotEqual(cached, wrong)

    def test_partial_page_is_rejected_before_caching(self):
        partial = (
            "<html><head><title>Visa Bulletin For August 2026</title></head><body>"
            + bulletin.SELF_TEST_HTML
            + "</body></html>"
        )
        expected = valid_bulletin_html(2026, 8)

        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            if parsed.hostname == "travel.state.gov":
                return FakeResponse(partial)
            if parsed.hostname == "childabduction.state.gov":
                return FakeResponse(expected)
            self.fail(f"unexpected host: {parsed.hostname}")

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            with patch.object(bulletin.urllib.request, "urlopen", side_effect=open_url):
                body = self._fetch(2026, 8, cache_dir)
            cached = (cache_dir / "2026-08.html").read_text()

        self.assertEqual(body, expected)
        self.assertEqual(cached, expected)
        self.assertNotEqual(cached, partial)

    def test_legacy_title_wording_is_accepted_from_cache(self):
        variants = ["Visa Bulletin October 2009", "October 2009 Visa Bulletin"]
        for index, title in enumerate(variants):
            with self.subTest(title=title):
                page = valid_bulletin_html(2009, 10).replace(
                    "Visa Bulletin For October 2009", title)
                with tempfile.TemporaryDirectory() as directory:
                    cache_dir = Path(directory)
                    (cache_dir / "2009-10.html").write_text(page)
                    with patch.object(
                            bulletin.urllib.request, "urlopen",
                            side_effect=AssertionError("valid cache should avoid network")):
                        body = self._fetch(2009, 10, cache_dir)
                self.assertEqual(body, page, f"variant {index}")

    def test_invalid_proxy_page_is_not_cached_and_next_slug_is_tried(self):
        challenge = "<html><title>Attention Required! | Cloudflare</title></html>"
        expected = valid_bulletin_html(2012, 10)

        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            target_url = urllib.parse.parse_qs(parsed.query).get(
                "url", [request.full_url])[0]
            target_path = urllib.parse.urlsplit(target_url).path
            if "visa-bulletin-for-" in target_path:
                if parsed.hostname in {"travel.state.gov", "childabduction.state.gov"}:
                    raise http_error(request, 403)
                if parsed.hostname == "api.scraperapi.com":
                    return FakeResponse(challenge)
            if parsed.hostname == "travel.state.gov":
                raise http_error(request, 403)
            if parsed.hostname == "childabduction.state.gov":
                return FakeResponse(expected)
            self.fail(f"unexpected request: {request.full_url}")

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            with patch.dict(os.environ, {"SCRAPERAPI_KEY": "top-secret"}):
                with patch.object(
                        bulletin.urllib.request, "urlopen", side_effect=open_url):
                    body = self._fetch(2012, 10, cache_dir)
            cached = (cache_dir / "2012-10.html").read_text()

        self.assertEqual(body, expected)
        self.assertEqual(cached, expected)
        self.assertNotIn(challenge, cached)

    def test_proxy_failure_never_logs_or_urls_the_api_key(self):
        api_key = "never-log-this"

        def open_url(request, timeout):
            parsed = self._record(request, timeout)
            if parsed.hostname in {"travel.state.gov", "childabduction.state.gov"}:
                raise http_error(request, 403)
            if parsed.hostname == "api.scraperapi.com":
                raise http_error(request, 403)
            self.fail(f"unexpected host: {parsed.hostname}")

        stderr = io.StringIO()
        with patch.dict(os.environ, {"SCRAPERAPI_KEY": api_key}):
            with patch.object(bulletin.urllib.request, "urlopen", side_effect=open_url):
                with redirect_stderr(stderr):
                    body = bulletin.fetch_page(2026, 8, None, retries=3)

        self.assertIsNone(body)
        self.assertNotIn(api_key, stderr.getvalue())
        self.assertTrue(all(api_key not in request.full_url
                            for request, _timeout in self.calls))
        self.assertEqual(sum(
            urllib.parse.urlsplit(request.full_url).hostname == "api.scraperapi.com"
            for request, _timeout in self.calls), 1)

    def test_main_prioritizes_newest_fetch_but_writes_chronologically(self):
        fetched = []

        def fetch_page(year, month, _cache_dir, **_kwargs):
            fetched.append((year, month))
            return valid_bulletin_html(year, month)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bulletins.json"
            argv = [
                "fetch_visa_bulletin.py",
                "--start", "2026-07",
                "--end", "2026-08",
                "--out", str(output),
                "--delay", "0",
            ]
            with patch.object(sys, "argv", argv):
                with patch.object(bulletin, "fetch_page", side_effect=fetch_page):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        result = bulletin.main()
            rows = json.loads(output.read_text())

        written_months = list(dict.fromkeys(row["bulletinMonth"] for row in rows))
        self.assertEqual(result, 0)
        self.assertEqual(fetched, [(2026, 8), (2026, 7)])
        self.assertEqual(written_months, ["2026-07", "2026-08"])


if __name__ == "__main__":
    unittest.main()
