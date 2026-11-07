
"""
Step 3 - Crawl company websites and save clean text for the AI step.
 
For every selected company with a website:
- reads robots.txt and only fetches pages the site allows
- opens the homepage and finds up to 4 more pages by their links:
  Impressum, services, about us / team, careers / jobs  (max 5 pages per site)
- saves clean text (no scripts or styles), cut to MAX_CHARS per page
- collects e-mail addresses shown on the pages (for the contact bonus)
 
Politeness and safety
- honest user agent with a link to the public repo
- max 1 request per second to the same website; 8 websites in parallel
- "websites" that are really directory or social pages (Facebook, ImmoScout, ...) are skipped
- RESUME: websites already in data/crawl_status.csv are skipped on a re-run
- TLS certificates are always verified; sites with broken certificates are recorded, not forced
 
Input : data/enriched_candidates.csv (from fetch_details.py)
Output: data/pages.jsonl        one line per saved page (text, url, page type, emails)
        data/crawl_status.csv   one row per website: status, pages saved, error
        data/crawl_log.json     run summary
 
Run
    python crawl_websites.py --limit 10    # small test run first
    python crawl_websites.py               # all websites
"""
 
import argparse
import csv
import json
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
 
import requests
from bs4 import BeautifulSoup
 
USER_AGENT = "heidi-leads-research-crawler/0.1 (+https://github.com/kai2055/heidi-leads)"
TIMEOUT = (5, 15)            # seconds: connect, read
PER_SITE_DELAY = 1.0         # seconds between requests to the same website
WORKERS = 8                  # websites crawled in parallel
MAX_CHARS = 20_000           # text kept per page (keeps the AI step cheap)
MAX_BYTES = 2_000_000        # ignore pages larger than this
LOW_TEXT = 200               # less text than this usually means a JavaScript-only site
 
DATA_DIR = "data"
 
# Pages we want, found by words in the link address or link text (German and English)
PAGE_PATTERNS = {
    "impressum": re.compile(r"impressum|imprint|legal[-_ ]?notice|anbieterkennzeichnung", re.I),
    "services": re.compile(r"leistung|service|angebot|hausverwaltung|weg[-_ ]?verwaltung|"
                           r"mietverwaltung|miethausverwaltung|sondereigentum|\bsev\b", re.I),
    "about": re.compile(r"[uü]ber[-_ ]?uns|ueber[-_ ]?uns|about|unternehmen|team|wir[-_ ]?sind|"
                        r"philosophie|historie|geschichte", re.I),
    "careers": re.compile(r"karriere|jobs?\b|stellen|career|bewerb|offene[-_ ]?stellen", re.I),
}
 
# A "website" on one of these domains is a profile page, not the company's own site
DIRECTORY_DOMAINS = (
    "facebook.com", "instagram.com", "linkedin.com", "xing.com", "twitter.com", "x.com",
    "immobilienscout24.de", "immowelt.de", "immonet.de", "kleinanzeigen.de", "google.com",
    "goo.gl", "g.page", "yelp.de", "gelbeseiten.de", "11880.com", "dasoertliche.de",
    "jacasa.de", "wg-gesucht.de", "youtube.com",
)
 
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
FILE_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".doc", ".docx", ".xls", ".xlsx")
 
STATUS_FIELDS = ["domain", "place_ids", "start_url", "status", "pages_saved",
                 "page_types", "error", "crawled_at"]
 
 
# ----------------------------------------------------------------- helpers
def domain_of(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host
 
 
# Government websites: never a property management company's own site
PUBLIC_DOMAINS = ("bund.de", "berlin.de", "bundespolizei.de", "polizei.de", "bundeswehr.de")
 
 
def is_directory(domain):
    return any(domain == d or domain.endswith("." + d) for d in DIRECTORY_DOMAINS + PUBLIC_DOMAINS)
 
 
def same_site(url, domain):
    host = domain_of(url)
    return host == domain or host.endswith("." + domain)
 
 
def clean_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        tag.decompose()
    text = soup.get_text(" ")
    return re.sub(r"\s+", " ", text).strip()
 
 
def find_emails(html, text):
    found = set(EMAIL_PATTERN.findall(text))
    for href in re.findall(r'href=["\']mailto:([^"\'?]+)', html, re.I):
        found.add(href.strip())
    # drop obvious non-addresses such as image files
    return sorted(e.lower() for e in found if not e.lower().endswith((".png", ".jpg", ".gif", ".webp")))
 
 
def pick_pages(html, base_url, domain):
    """Return {page_type: url} for the first matching same-site link of each type."""
    soup = BeautifulSoup(html, "html.parser")
    picks = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if not same_site(url, domain) or url.lower().endswith(FILE_EXTENSIONS):
            continue
        path_and_text = f"{urlparse(url).path} {a.get_text(' ', strip=True)}"
        for page_type, pattern in PAGE_PATTERNS.items():
            if page_type not in picks and pattern.search(path_and_text):
                picks[page_type] = url
                break
    return picks
 
 
# ----------------------------------------------------------------- crawler
class SiteCrawler:
    def __init__(self, session=None, sleep=time.sleep, delay=PER_SITE_DELAY):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.sleep = sleep
        self.delay = delay
 
    def _get(self, url):
        resp = self.session.get(url, timeout=TIMEOUT, allow_redirects=True)
        return resp
 
    def _robots(self, start_url):
        parsed = urlparse(start_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = RobotFileParser()
        try:
            resp = self._get(robots_url)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            elif resp.status_code in (401, 403):
                parser.disallow_all = True     # robots.txt itself is forbidden: stay out
            else:
                parser.allow_all = True        # no robots.txt = everything allowed
        except requests.RequestException:
            parser.allow_all = True
        return parser
 
    def _fetch_page(self, url):
        """Return (final_url, html) or raise ValueError with a short reason."""
        resp = self._get(url)
        if resp.status_code != 200:
            raise ValueError(f"http_{resp.status_code}")
        if "html" not in resp.headers.get("Content-Type", "text/html").lower():
            raise ValueError("not_html")
        if len(resp.content) > MAX_BYTES:
            raise ValueError("too_large")
        # many small German sites send no charset; guess it so umlauts are not broken
        if "charset" not in resp.headers.get("Content-Type", "").lower():
            resp.encoding = resp.apparent_encoding
        return resp.url, resp.text
 
    def crawl(self, start_url):
        """Crawl one website. Returns (status dict, list of page records)."""
        domain = domain_of(start_url)
        if is_directory(domain):
            return {"status": "skipped_directory", "error": domain}, []
 
        robots = self._robots(start_url)
        if not robots.can_fetch(USER_AGENT, start_url):
            return {"status": "robots_blocked", "error": ""}, []
 
        pages = []
        try:
            self.sleep(self.delay)
            final_url, html = self._fetch_page(start_url)
        except requests.exceptions.SSLError:
            return {"status": "ssl_error", "error": "certificate problem"}, []
        except requests.exceptions.Timeout:
            return {"status": "timeout", "error": ""}, []
        except requests.RequestException as err:
            return {"status": "error", "error": type(err).__name__}, []
        except ValueError as err:
            return {"status": "error", "error": str(err)}, []
 
        final_domain = domain_of(final_url)
        if is_directory(final_domain):
            return {"status": "skipped_directory", "error": f"redirects to {final_domain}"}, []
 
        pages.append(self._record("home", final_url, html))
        for page_type, url in pick_pages(html, final_url, final_domain).items():
            if not robots.can_fetch(USER_AGENT, url):
                continue
            try:
                self.sleep(self.delay)
                page_url, page_html = self._fetch_page(url)
            except (requests.RequestException, ValueError):
                continue        # one missing sub-page should not fail the whole site
            pages.append(self._record(page_type, page_url, page_html))
 
        total_text = sum(len(p["text"]) for p in pages)
        status = "ok" if total_text >= LOW_TEXT else "low_text"
        return {"status": status, "error": ""}, pages
 
    @staticmethod
    def _record(page_type, url, html):
        text = clean_text(html)
        return {
            "page_type": page_type,
            "url": url,
            "text": text[:MAX_CHARS],
            "text_truncated": len(text) > MAX_CHARS,
            "emails": find_emails(html, text),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
 
 
# ----------------------------------------------------------------- run
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))
 
 
def websites_to_crawl(enriched):
    """Group selected companies by website domain: one crawl per website."""
    sites = {}
    for row in enriched:
        url = row.get("website", "").strip()
        if not url or row.get("selection_group", "not_selected") == "not_selected":
            continue
        domain = domain_of(url)
        if domain not in sites:
            sites[domain] = {"domain": domain, "start_url": url, "place_ids": []}
        sites[domain]["place_ids"].append(row["place_id"])
    return list(sites.values())
 
 
def run(crawler, data_dir=DATA_DIR, limit=None, workers=WORKERS):
    enriched = read_csv(os.path.join(data_dir, "enriched_candidates.csv"))
    if not enriched:
        raise SystemExit("No data/enriched_candidates.csv. Run fetch_details.py first.")
 
    status_path = os.path.join(data_dir, "crawl_status.csv")
    pages_path = os.path.join(data_dir, "pages.jsonl")
    done = {r["domain"] for r in read_csv(status_path)}
 
    sites = websites_to_crawl(enriched)
    todo = [s for s in sites if s["domain"] not in done]
    if limit is not None:
        todo = todo[:limit]
    already = sum(1 for s in sites if s["domain"] in done)
    print(f"Websites: {len(sites)} | already crawled: {already} | crawling now: {len(todo)}")
 
    lock = threading.Lock()
    new_status_file = not os.path.exists(status_path)
 
    with open(status_path, "a", newline="", encoding="utf-8") as status_f, \
            open(pages_path, "a", encoding="utf-8") as pages_f:
        writer = csv.DictWriter(status_f, fieldnames=STATUS_FIELDS)
        if new_status_file:
            writer.writeheader()
 
        def work(site):
            result, pages = crawler.crawl(site["start_url"])
            return site, result, pages
 
        finished = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, s) for s in todo]
            for future in as_completed(futures):
                site, result, pages = future.result()
                with lock:
                    for page in pages:
                        record = {"domain": site["domain"], "place_ids": site["place_ids"], **page}
                        pages_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    writer.writerow({
                        "domain": site["domain"],
                        "place_ids": "|".join(site["place_ids"]),
                        "start_url": site["start_url"],
                        "status": result["status"],
                        "pages_saved": len(pages),
                        "page_types": "|".join(p["page_type"] for p in pages),
                        "error": result["error"],
                        "crawled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    })
                    pages_f.flush()
                    status_f.flush()
                    finished += 1
                    if finished % 25 == 0:
                        print(f"  crawled {finished}/{len(todo)}")
 
    all_status = read_csv(status_path)
    page_types = Counter()
    for r in all_status:
        page_types.update(t for t in r["page_types"].split("|") if t)
    summary = {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "websites_total": len(sites),
        "websites_crawled": len(all_status),
        "by_status": dict(Counter(r["status"] for r in all_status)),
        "pages_saved": sum(int(r["pages_saved"]) for r in all_status),
        "sites_with_page_type": dict(page_types),
    }
    with open(os.path.join(data_dir, "crawl_log.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return summary
 
 
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="only crawl this many new websites (for a test run)")
    args = parser.parse_args()
 
    summary = run(SiteCrawler(), limit=args.limit)
    print("\nDone.")
    print(json.dumps(summary, indent=2))
    print("\nSaved: data/pages.jsonl and data/crawl_status.csv")
 
 
if __name__ == "__main__":
    main()

