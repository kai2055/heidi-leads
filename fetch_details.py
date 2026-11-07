"""
Step 2b - Add website and phone from Google Place Details.

Who gets a lookup (to stay inside the free monthly allowance)
- every "likely_pm" candidate
- plus a random sample of "unclear" candidates (fixed seed, so it is repeatable)
- total selection never above --total (default 1000 = the free Place Details Enterprise allowance)

The sample tells us what share of the unclear group are real property managers,
so the rest can be estimated and finished later if it is worth the cost.

Safety
- HARD STOP at --max-calls requests in one run.
- RESUME: places already in data/details.csv are skipped, so a re-run never pays twice.
- PACING: at most --per-minute requests per minute (default 100); if Google still
  answers 429 "too many requests", the script waits a full minute and tries again.
- Every raw response is saved to data/raw/details_responses_<run_id>.jsonl.

Input : data/candidates.csv (from filter_candidates.py)
Output: data/details.csv            one row per looked-up place (appended across runs)
        data/enriched_candidates.csv candidates + website/phone + selection group
        data/details_log.json        run summary

Run
    python fetch_details.py --max-calls 5     # small test run first
    python fetch_details.py                   # full run
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

URL = "https://places.googleapis.com/v1/places/{place_id}"

# All Enterprise-tier fields; asking for them together costs one Enterprise call.
FIELD_MASK = "id,websiteUri,nationalPhoneNumber,internationalPhoneNumber,rating,userRatingCount"

DATA_DIR = "data"
CANDIDATES_CSV = os.path.join(DATA_DIR, "candidates.csv")
DETAILS_CSV = os.path.join(DATA_DIR, "details.csv")
ENRICHED_CSV = os.path.join(DATA_DIR, "enriched_candidates.csv")
LOG_JSON = os.path.join(DATA_DIR, "details_log.json")

DETAILS_FIELDS = ["place_id", "website", "website_domain", "phone", "phone_international",
                  "google_rating", "google_rating_count", "details_status", "fetched_at"]

SAMPLE_SEED = 42


class BudgetReached(Exception):
    pass


class PlacesAPIError(Exception):
    pass


# ----------------------------------------------------------------- selection
def select_places(candidates, total_budget, already_done, seed=SAMPLE_SEED):
    """Return (selected rows, group per place_id).

    Every likely_pm candidate is selected. The rest of `total_budget` is filled with
    unclear candidates: ones already fetched in an earlier run first (they cost nothing),
    then a seeded random sample of new ones.
    """
    likely = [r for r in candidates if r["relevance"] == "likely_pm"]
    unclear = [r for r in candidates if r["relevance"] == "unclear"]
    n_unclear = max(0, total_budget - len(likely))

    unclear_done = [r for r in unclear if r["place_id"] in already_done][:n_unclear]
    unclear_new = sorted((r for r in unclear if r["place_id"] not in already_done),
                         key=lambda r: r["place_id"])
    rng = random.Random(seed)
    sample_new = rng.sample(unclear_new, min(n_unclear - len(unclear_done), len(unclear_new)))

    groups = {r["place_id"]: "all_likely" for r in likely}
    for r in unclear_done + sample_new:
        groups[r["place_id"]] = "unclear_sample"
    return likely + unclear_done + sample_new, groups


# ----------------------------------------------------------------- API client
RATE_LIMIT_WAIT = 60    # seconds to wait after Google says "too many requests per minute"


class DetailsClient:
    def __init__(self, api_key, max_calls, session=None, raw_log=None, sleep=time.sleep,
                 per_minute=100, clock=time.monotonic):
        self.api_key = api_key
        self.max_calls = max_calls
        self.calls = 0
        self.session = session or requests.Session()
        self.raw_log = raw_log
        self.sleep = sleep
        self.clock = clock
        self.min_interval = 60.0 / per_minute if per_minute else 0.0
        self._last_call = None

    def _pace(self):
        """Wait so that we never send more than `per_minute` requests per minute."""
        if self._last_call is not None and self.min_interval:
            wait = self.min_interval - (self.clock() - self._last_call)
            if wait > 0:
                self.sleep(wait)
        self._last_call = self.clock()

    def get(self, place_id):
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": FIELD_MASK}
        for attempt in range(4):
            if self.calls >= self.max_calls:
                raise BudgetReached()
            self._pace()
            self.calls += 1
            resp = self.session.get(URL.format(place_id=place_id), headers=headers, timeout=30)
            data = resp.json() if resp.status_code == 200 else {"error": resp.text[:1000]}
            self._log(place_id, resp.status_code, data)

            if resp.status_code == 200:
                return "ok", data
            if resp.status_code == 404:
                return "not_found", {}
            if resp.status_code == 429 and attempt < 3:
                print(f"  ! HTTP 429 (too many requests per minute), waiting {RATE_LIMIT_WAIT}s")
                self.sleep(RATE_LIMIT_WAIT)
                continue
            if resp.status_code in (500, 503) and attempt < 3:
                wait = 2 ** (attempt + 1)
                print(f"  ! HTTP {resp.status_code}, retrying in {wait}s")
                self.sleep(wait)
                continue
            raise PlacesAPIError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return "failed", {}

    def _log(self, place_id, status, data):
        if self.raw_log is None:
            return
        record = {"call": self.calls,
                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "place_id": place_id, "status": status, "response": data}
        self.raw_log.write(json.dumps(record, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------- parsing
def domain_of(url):
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def to_details_row(place_id, status, data):
    website = data.get("websiteUri", "")
    return {
        "place_id": place_id,
        "website": website,
        "website_domain": domain_of(website),
        "phone": data.get("nationalPhoneNumber", ""),
        "phone_international": data.get("internationalPhoneNumber", ""),
        "google_rating": data.get("rating", ""),
        "google_rating_count": data.get("userRatingCount", ""),
        "details_status": status,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ----------------------------------------------------------------- files
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_details(path, rows):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DETAILS_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def write_enriched(path, candidates, details_by_id, groups):
    fields = list(candidates[0].keys()) + ["selection_group"] + DETAILS_FIELDS[1:]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in candidates:
            out = dict(row)
            out["selection_group"] = groups.get(row["place_id"], "not_selected")
            det = details_by_id.get(row["place_id"], {})
            for key in DETAILS_FIELDS[1:]:
                out[key] = det.get(key, "")
            writer.writerow(out)


# ----------------------------------------------------------------- run
def run(client, total_budget=1000, data_dir=DATA_DIR, seed=SAMPLE_SEED):
    candidates_path = os.path.join(data_dir, "candidates.csv")
    details_path = os.path.join(data_dir, "details.csv")
    candidates = read_csv(candidates_path)
    if not candidates:
        raise SystemExit(f"No candidates found at {candidates_path}. Run filter_candidates.py first.")

    existing = {r["place_id"]: r for r in read_csv(details_path)}
    selected, groups = select_places(candidates, total_budget, set(existing), seed)
    todo = [r for r in selected if r["place_id"] not in existing]

    print(f"Selected {len(selected)} places "
          f"({sum(1 for g in groups.values() if g == 'all_likely')} likely, "
          f"{sum(1 for g in groups.values() if g == 'unclear_sample')} unclear sample). "
          f"Already fetched: {len(selected) - len(todo)}. To fetch now: {len(todo)}.")

    new_rows, stats = [], {"stopped_by_limit": False, "stopped_by_error": ""}
    try:
        for i, row in enumerate(todo, 1):
            status, data = client.get(row["place_id"])
            new_rows.append(to_details_row(row["place_id"], status, data))
            if i % 50 == 0:
                append_details(details_path, new_rows)   # save progress regularly
                new_rows = []
                print(f"  fetched {i}/{len(todo)} | calls used: {client.calls}")
    except BudgetReached:
        stats["stopped_by_limit"] = True
        print(f"\nHard stop reached ({client.max_calls} calls). Saving what we have.")
    except PlacesAPIError as err:
        stats["stopped_by_error"] = str(err)
        print(f"\nStopped by API error: {err}\nSaving what we have.")
    if new_rows:
        append_details(details_path, new_rows)

    details_by_id = {r["place_id"]: r for r in read_csv(details_path)}
    write_enriched(os.path.join(data_dir, "enriched_candidates.csv"), candidates, details_by_id, groups)

    fetched = [details_by_id[p] for p in groups if p in details_by_id]
    summary = {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "api_calls_used_this_run": client.calls,
        "max_calls": client.max_calls,
        "total_budget": total_budget,
        "sample_seed": seed,
        "selected_likely": sum(1 for g in groups.values() if g == "all_likely"),
        "selected_unclear_sample": sum(1 for g in groups.values() if g == "unclear_sample"),
        "unclear_total": sum(1 for r in candidates if r["relevance"] == "unclear"),
        "fetched_total": len(fetched),
        "with_website": sum(1 for r in fetched if r["website"]),
        "with_phone": sum(1 for r in fetched if r["phone"]),
        "not_found": sum(1 for r in fetched if r["details_status"] == "not_found"),
        **stats,
    }
    with open(os.path.join(data_dir, "details_log.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--total", type=int, default=1000,
                        help="total places to look up across all runs (default 1000 = free allowance)")
    parser.add_argument("--max-calls", type=int, default=1000,
                        help="hard stop: maximum API requests in this run")
    parser.add_argument("--per-minute", type=int, default=100,
                        help="maximum requests per minute (Google also has a per-minute limit)")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        sys.exit("Missing GOOGLE_PLACES_API_KEY. Put it in the .env file.")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_dir = os.path.join(DATA_DIR, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    raw_path = os.path.join(raw_dir, f"details_responses_{run_id}.jsonl")

    with open(raw_path, "w", encoding="utf-8") as raw_log:
        client = DetailsClient(api_key, args.max_calls, raw_log=raw_log, per_minute=args.per_minute)
        summary = run(client, total_budget=args.total)

    print("\nDone.")
    print(json.dumps(summary, indent=2))
    print(f"\nSaved: {ENRICHED_CSV}")
    if summary["stopped_by_error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()