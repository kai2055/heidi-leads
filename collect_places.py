"""
Step 1 - Collect Berlin property management companies from Google Places API (New).

What it does
- Searches Google Maps for 4 search terms across a grid that covers Berlin.
- Google returns max 60 results per search. If a grid tile hits 60,
  the tile is split into 4 smaller tiles and searched again (more detail
  where companies are dense, e.g. Mitte, Charlottenburg).
- A full tile with no Berlin company in it (e.g. around Potsdam) is not
  split, so no calls are spent outside Berlin.
- Asks only for "Pro" fields (no website, no phone) -> cheaper SKU with
  5,000 free calls per month. Website + phone come in a later step.
- HARD STOP: never makes more than --max-calls requests (default 1000).
- Saves every raw API response, so every row can be traced and re-checked.

Output (all in data/, which is git-ignored)
- raw_places.csv                      one row per unique place
- collect_log.json                    run summary (calls used, tiles, counts)
- raw/search_responses_<run_id>.jsonl every API request + response, one per line

Run
    python collect_places.py --max-calls 20   # small test run first
    python collect_places.py                  # full run
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

# ----------------------------------------------------------------- settings
URL = "https://places.googleapis.com/v1/places:searchText"

# Only Pro-tier fields. Do NOT add websiteUri, nationalPhoneNumber,
# rating or userRatingCount here: they switch every call to Enterprise pricing.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.addressComponents",
    "places.location",
    "places.types",
    "places.primaryType",
    "places.businessStatus",
    "places.googleMapsUri",
    "nextPageToken",
])

SEARCH_TERMS = [
    "Hausverwaltung",
    "Immobilienverwaltung",
    "WEG-Verwaltung",
    "Mietverwaltung",
]

# Bounding box around Berlin (south, west, north, east)
BERLIN_BBOX = (52.338, 13.088, 52.676, 13.761)
START_GRID = 4          # 4 x 4 = 16 starting tiles of roughly 9 x 11 km
MAX_SPLIT_DEPTH = 3     # smallest tile after 3 splits: roughly 1 x 1.4 km
PAGE_SIZE = 20          # Google max per page
MAX_PAGES = 3           # Google max -> 60 results per search
CAP = PAGE_SIZE * MAX_PAGES

# Berlin postcodes run from 10115 to 14199
BERLIN_POSTCODE_MIN = 10115
BERLIN_POSTCODE_MAX = 14199

OUT_DIR = "data"


class BudgetReached(Exception):
    """Raised when the hard call limit is reached."""


class PlacesAPIError(Exception):
    """Raised when Google returns an error we should not retry (e.g. bad key)."""


# ----------------------------------------------------------------- API client
class PlacesClient:
    """Sends Text Search requests, counts every call and logs raw responses."""

    def __init__(self, api_key, max_calls, session=None, raw_log=None, sleep=time.sleep):
        self.api_key = api_key
        self.max_calls = max_calls
        self.calls = 0
        self.session = session or requests.Session()
        self.raw_log = raw_log      # open file handle (jsonl) or None
        self.sleep = sleep

    def _check_budget(self):
        if self.calls >= self.max_calls:
            raise BudgetReached()

    def search(self, query, tile, page_token=None):
        self._check_budget()

        south, west, north, east = tile
        body = {
            "textQuery": query,
            "languageCode": "de",
            "regionCode": "DE",
            "pageSize": PAGE_SIZE,
            "locationRestriction": {
                "rectangle": {
                    "low": {"latitude": south, "longitude": west},
                    "high": {"latitude": north, "longitude": east},
                }
            },
        }
        if page_token:
            body["pageToken"] = page_token

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }

        for attempt in range(4):
            self.calls += 1
            resp = self.session.post(URL, headers=headers, json=body, timeout=30)

            if resp.status_code == 200:
                data = resp.json()
                self._log(query, tile, page_token, resp.status_code, data)
                return data

            self._log(query, tile, page_token, resp.status_code, {"error": resp.text[:1000]})
            if resp.status_code in (429, 500, 503) and attempt < 3:
                wait = 2 ** (attempt + 1)
                print(f"  ! HTTP {resp.status_code}, retrying in {wait}s")
                self.sleep(wait)
                self._check_budget()
                continue
            raise PlacesAPIError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return {}

    def _log(self, query, tile, page_token, status, data):
        if self.raw_log is None:
            return
        record = {
            "call": self.calls,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query": query,
            "tile": tile,
            "page_token_used": bool(page_token),
            "status": status,
            "response": data,
        }
        self.raw_log.write(json.dumps(record, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------- grid
def make_grid(bbox, n):
    """Cut a box into n x n equal tiles."""
    south, west, north, east = bbox
    dlat = (north - south) / n
    dlng = (east - west) / n
    return [
        (south + i * dlat, west + j * dlng, south + (i + 1) * dlat, west + (j + 1) * dlng)
        for i in range(n)
        for j in range(n)
    ]


def split_tile(tile):
    """Cut one tile into 4 equal tiles (quadtree step)."""
    return make_grid(tile, 2)


# ----------------------------------------------------------------- parsing
def component(place, wanted_type):
    for comp in place.get("addressComponents", []):
        if wanted_type in comp.get("types", []):
            return comp.get("longText", "")
    return ""


def is_berlin(postal_code, locality):
    if locality != "Berlin":
        return False
    return postal_code.isdigit() and BERLIN_POSTCODE_MIN <= int(postal_code) <= BERLIN_POSTCODE_MAX


def to_row(place, query, collected_at):
    postal = component(place, "postal_code")
    locality = component(place, "locality")
    loc = place.get("location", {})
    return {
        "place_id": place.get("id", ""),
        "name": place.get("displayName", {}).get("text", ""),
        "address": place.get("formattedAddress", ""),
        "postal_code": postal,
        "district": component(place, "sublocality_level_1"),
        "locality": locality,
        "in_berlin": is_berlin(postal, locality),
        "lat": loc.get("latitude", ""),
        "lng": loc.get("longitude", ""),
        "primary_type": place.get("primaryType", ""),
        "types": "|".join(place.get("types", [])),
        "business_status": place.get("businessStatus", ""),
        "maps_url": place.get("googleMapsUri", ""),
        "matched_terms": query,
        "source": "google_places_text_search",
        "collected_at": collected_at,
    }


def add_place(places, place, query, collected_at):
    """Add a place once; if seen before, only record the extra search term."""
    pid = place.get("id")
    if not pid:
        return
    if pid in places:
        terms = places[pid]["matched_terms"].split("|")
        if query not in terms:
            places[pid]["matched_terms"] += "|" + query
    else:
        places[pid] = to_row(place, query, collected_at)


# ----------------------------------------------------------------- collect
def search_tile(client, query, tile, depth, places, stats, collected_at):
    found = 0
    found_in_berlin = 0
    token = None
    for _ in range(MAX_PAGES):
        data = client.search(query, tile, token)
        for place in data.get("places", []):
            found += 1
            add_place(places, place, query, collected_at)
            if places.get(place.get("id"), {}).get("in_berlin"):
                found_in_berlin += 1
        token = data.get("nextPageToken")
        if not token:
            break
        client.sleep(1)  # small pause before asking for the next page

    stats["tiles_searched"] += 1
    # Full tile but no Berlin company in it (e.g. Potsdam): do not spend more calls there
    if found >= CAP and found_in_berlin == 0:
        stats["tiles_skipped_outside_berlin"] += 1
        return
    # Hit Google's 60 cap -> there may be more; search 4 smaller tiles
    if found >= CAP and depth < MAX_SPLIT_DEPTH:
        stats["tiles_split"] += 1
        for child in split_tile(tile):
            search_tile(client, query, child, depth + 1, places, stats, collected_at)
    elif found >= CAP:
        stats["tiles_still_capped"] += 1


def collect(client, collected_at, terms=SEARCH_TERMS, bbox=BERLIN_BBOX, start_grid=START_GRID):
    """Run all searches. Returns (places, stats). Stops cleanly at the call limit."""
    places = {}
    stats = {"tiles_searched": 0, "tiles_split": 0, "tiles_still_capped": 0,
             "tiles_skipped_outside_berlin": 0,
             "stopped_by_limit": False, "stopped_by_error": ""}
    try:
        for query in terms:
            print(f"\nSearching: {query}")
            for tile in make_grid(bbox, start_grid):
                search_tile(client, query, tile, 0, places, stats, collected_at)
                print(f"  calls used: {client.calls:4d} | unique places: {len(places)}")
    except BudgetReached:
        stats["stopped_by_limit"] = True
        print(f"\nHard stop reached ({client.max_calls} calls). Saving what we have.")
    except PlacesAPIError as err:
        stats["stopped_by_error"] = str(err)
        print(f"\nStopped by API error: {err}\nSaving what we have.")
    return places, stats


def save(places, stats, client, started, run_id, out_dir=OUT_DIR):
    os.makedirs(out_dir, exist_ok=True)
    rows = sorted(places.values(), key=lambda r: (not r["in_berlin"], r["name"].lower()))
    if rows:
        with open(os.path.join(out_dir, "raw_places.csv"), "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    summary = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "search_terms": SEARCH_TERMS,
        "api_calls_used": client.calls,
        "max_calls": client.max_calls,
        "unique_places": len(rows),
        "in_berlin": sum(1 for r in rows if r["in_berlin"]),
        "permanently_closed": sum(1 for r in rows if r["business_status"] == "CLOSED_PERMANENTLY"),
        **stats,
    }
    with open(os.path.join(out_dir, "collect_log.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-calls", type=int, default=1000,
                        help="hard stop: maximum API requests for this run")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        sys.exit("Missing GOOGLE_PLACES_API_KEY. Put it in the .env file.")

    now = datetime.now(timezone.utc)
    started = now.isoformat(timespec="seconds")
    run_id = now.strftime("%Y%m%dT%H%M%SZ")

    raw_dir = os.path.join(OUT_DIR, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    raw_path = os.path.join(raw_dir, f"search_responses_{run_id}.jsonl")

    with open(raw_path, "w", encoding="utf-8") as raw_log:
        client = PlacesClient(api_key, args.max_calls, raw_log=raw_log)
        places, stats = collect(client, started)

    summary = save(places, stats, client, started, run_id)
    print("\nDone.")
    print(json.dumps(summary, indent=2))
    print(f"\nSaved: {OUT_DIR}/raw_places.csv and {raw_path}")
    if stats["stopped_by_error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()