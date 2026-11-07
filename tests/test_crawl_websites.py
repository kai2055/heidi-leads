"""Tests for crawl_websites.py with fake websites (no network)."""

import csv
import json
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import crawl_websites as cw  # noqa: E402

HOME = """
<html><head><script>var x = 1;</script><style>.a{}</style></head><body>
<nav><a href="/leistungen">Unsere Leistungen</a>
<a href="/ueber-uns">Über uns</a>
<a href="/karriere">Karriere</a>
<a href="/impressum">Impressum</a>
<a href="/agb.pdf">AGB</a>
<a href="https://www.facebook.com/firma">Facebook</a>
<a href="mailto:m.mueller@hv-test.de">Mail</a></nav>
<h1>Hausverwaltung Test GmbH</h1>
<p>Wir verwalten über 1.200 Wohneinheiten in Berlin.</p>
</body></html>
"""

IMPRESSUM = """<html><body><h1>Impressum</h1>
<p>Geschäftsführer: Max Müller. E-Mail: info@hv-test.de</p></body></html>"""


class FakeResponse:
    def __init__(self, url, status=200, text="", content_type="text/html; charset=utf-8"):
        self.url = url
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"


class FakeSession:
    """Maps URL -> FakeResponse or an exception to raise."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.requested = []

    def get(self, url, timeout, allow_redirects):
        self.requested.append(url)
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(url, status=404)
        if isinstance(route, Exception):
            raise route
        return route


def site_routes(robots="User-agent: *\nAllow: /"):
    base = "https://www.hv-test.de"
    return {
        f"{base}/robots.txt": FakeResponse(f"{base}/robots.txt", text=robots, content_type="text/plain"),
        f"{base}/": FakeResponse(f"{base}/", text=HOME),
        f"{base}/leistungen": FakeResponse(f"{base}/leistungen", text="<p>WEG-Verwaltung und Mietverwaltung</p>"),
        f"{base}/ueber-uns": FakeResponse(f"{base}/ueber-uns", text="<p>Seit 1995, 12 Mitarbeiter</p>"),
        f"{base}/karriere": FakeResponse(f"{base}/karriere", text="<p>Wir suchen Immobilienkaufleute</p>"),
        f"{base}/impressum": FakeResponse(f"{base}/impressum", text=IMPRESSUM),
    }


def crawler(routes):
    return cw.SiteCrawler(session=FakeSession(routes), sleep=lambda s: None)


# ----------------------------------------------------------------- helpers
def test_clean_text_removes_scripts_and_styles():
    text = cw.clean_text(HOME)
    assert "var x" not in text and ".a{}" not in text
    assert "Wir verwalten über 1.200 Wohneinheiten in Berlin." in text


def test_pick_pages_finds_each_type_once_and_skips_offsite_and_files():
    picks = cw.pick_pages(HOME, "https://www.hv-test.de/", "hv-test.de")
    assert picks == {
        "services": "https://www.hv-test.de/leistungen",
        "about": "https://www.hv-test.de/ueber-uns",
        "careers": "https://www.hv-test.de/karriere",
        "impressum": "https://www.hv-test.de/impressum",
    }


@pytest.mark.parametrize("domain,expected", [
    ("facebook.com", True), ("de-de.facebook.com", True), ("immobilienscout24.de", True),
    ("hv-test.de", False), ("myfacebook-fan.de", False),
    ("bundespolizei.de", True), ("service.berlin.de", True),
])
def test_is_directory(domain, expected):
    assert cw.is_directory(domain) is expected


def test_find_emails_from_text_and_mailto():
    emails = cw.find_emails(HOME + IMPRESSUM, cw.clean_text(IMPRESSUM))
    assert emails == ["info@hv-test.de", "m.mueller@hv-test.de"]


# ----------------------------------------------------------------- crawl one site
def test_crawl_saves_home_and_four_subpages():
    result, pages = crawler(site_routes()).crawl("https://www.hv-test.de/")
    assert result["status"] == "ok"
    assert {p["page_type"] for p in pages} == {"home", "services", "about", "careers", "impressum"}
    impressum = next(p for p in pages if p["page_type"] == "impressum")
    assert "Geschäftsführer: Max Müller" in impressum["text"]


def test_robots_disallow_blocks_subpage_but_not_home():
    routes = site_routes(robots="User-agent: *\nDisallow: /karriere")
    result, pages = crawler(routes).crawl("https://www.hv-test.de/")
    assert result["status"] == "ok"
    assert "careers" not in {p["page_type"] for p in pages}


def test_robots_disallow_all_blocks_site():
    routes = site_routes(robots="User-agent: *\nDisallow: /")
    result, pages = crawler(routes).crawl("https://www.hv-test.de/")
    assert result["status"] == "robots_blocked"
    assert pages == []


def test_directory_website_is_skipped_without_requests():
    c = crawler({})
    result, pages = c.crawl("https://www.facebook.com/hausverwaltung.x")
    assert result["status"] == "skipped_directory"
    assert c.session.requested == []


def test_ssl_and_timeout_are_recorded():
    base = "https://www.hv-test.de"
    routes = {f"{base}/": requests.exceptions.SSLError("bad cert")}
    assert crawler(routes).crawl(f"{base}/")[0]["status"] == "ssl_error"
    routes = {f"{base}/": requests.exceptions.ReadTimeout("slow")}
    assert crawler(routes).crawl(f"{base}/")[0]["status"] == "timeout"


def test_missing_subpage_does_not_fail_site():
    routes = site_routes()
    del routes["https://www.hv-test.de/karriere"]          # 404
    result, pages = crawler(routes).crawl("https://www.hv-test.de/")
    assert result["status"] == "ok"
    assert len(pages) == 4


def test_very_short_site_is_marked_low_text():
    base = "https://www.js-only.de"
    routes = {f"{base}/": FakeResponse(f"{base}/", text="<div id='app'></div>")}
    result, _ = crawler(routes).crawl(f"{base}/")
    assert result["status"] == "low_text"


# ----------------------------------------------------------------- run
def write_enriched(tmp_path, rows):
    fields = ["place_id", "name", "selection_group", "website"]
    with open(tmp_path / "enriched_candidates.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_run_groups_by_domain_writes_outputs_and_resumes(tmp_path):
    write_enriched(tmp_path, [
        {"place_id": "A", "name": "HV Test", "selection_group": "all_likely", "website": "https://www.hv-test.de/"},
        {"place_id": "B", "name": "HV Test Filiale", "selection_group": "all_likely", "website": "https://hv-test.de/kontakt"},
        {"place_id": "C", "name": "Ohne Web", "selection_group": "all_likely", "website": ""},
        {"place_id": "D", "name": "Nicht gewählt", "selection_group": "not_selected", "website": "https://other.de/"},
    ])
    summary = cw.run(crawler(site_routes()), data_dir=str(tmp_path), workers=2)

    assert summary["websites_total"] == 1                  # A and B share one domain
    assert summary["by_status"] == {"ok": 1}
    status = list(csv.DictReader(open(tmp_path / "crawl_status.csv", encoding="utf-8")))
    assert status[0]["place_ids"] == "A|B"
    pages = [json.loads(line) for line in open(tmp_path / "pages.jsonl", encoding="utf-8")]
    assert len(pages) == 5 and pages[0]["place_ids"] == ["A", "B"]

    second = crawler(site_routes())
    cw.run(second, data_dir=str(tmp_path), workers=2)
    assert second.session.requested == []                 # resume: nothing fetched again