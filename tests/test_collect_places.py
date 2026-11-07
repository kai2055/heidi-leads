"""Tests for collect_places.py. They use a fake Google API, so they cost nothing.

Run:  pytest -q
"""

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collect_places as cp  # noqa: E402


# ----------------------------------------------------------------- fakes
def fake_place(pid, postcode="10115", locality="Berlin"):
    return {
        "id": pid,
        "displayName": {"text": f"Hausverwaltung {pid}"},
        "formattedAddress": f"Teststr. 1, {postcode} {locality}",
        "addressComponents": [
            {"longText": postcode, "types": ["postal_code"]},
            {"longText": locality, "types": ["locality"]},
            {"longText": "Mitte", "types": ["sublocality_level_1"]},
        ],
        "location": {"latitude": 52.52, "longitude": 13.40},
        "types": ["real_estate_agency"],
        "businessStatus": "OPERATIONAL",
    }


class FakeResponse:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


class FakeSession:
    """Tiles wider than `dense_size` degrees are 'crowded' and return 60 results
    over 3 pages; smaller tiles return 5 results on one page."""

    def __init__(self, dense_size=0.03, status=200, postcode="10115", locality="Berlin"):
        self.dense_size = dense_size
        self.status = status
        self.postcode = postcode
        self.locality = locality
        self.requests = 0

    def post(self, url, headers, json, timeout):
        self.requests += 1
        if self.status != 200:
            return FakeResponse(self.status, text="error")
        rect = json["locationRestriction"]["rectangle"]
        size = rect["high"]["latitude"] - rect["low"]["latitude"]
        crowded = size > self.dense_size
        page = int(json.get("pageToken", 0))
        n = 20 if crowded else 5
        base = f"{rect['low']['latitude']:.4f}-{rect['low']['longitude']:.4f}-{page}"
        data = {"places": [fake_place(f"{base}-{i}", self.postcode, self.locality)
                           for i in range(n)]}
        if crowded and page < 2:
            data["nextPageToken"] = str(page + 1)
        return FakeResponse(200, data)


def make_client(session, max_calls=10_000, raw_log=None):
    return cp.PlacesClient("fake-key", max_calls, session=session,
                           raw_log=raw_log, sleep=lambda s: None)


# ----------------------------------------------------------------- grid
def test_grid_has_n_squared_tiles_and_covers_bbox():
    tiles = cp.make_grid(cp.BERLIN_BBOX, 4)
    assert len(tiles) == 16
    assert min(t[0] for t in tiles) == pytest.approx(cp.BERLIN_BBOX[0])
    assert min(t[1] for t in tiles) == pytest.approx(cp.BERLIN_BBOX[1])
    assert max(t[2] for t in tiles) == pytest.approx(cp.BERLIN_BBOX[2])
    assert max(t[3] for t in tiles) == pytest.approx(cp.BERLIN_BBOX[3])


def test_split_tile_gives_four_equal_children():
    tile = (52.0, 13.0, 52.4, 13.8)
    children = cp.split_tile(tile)
    assert len(children) == 4
    for s, w, n, e in children:
        assert n - s == pytest.approx(0.2)
        assert e - w == pytest.approx(0.4)


# ----------------------------------------------------------------- parsing
@pytest.mark.parametrize("postcode,locality,expected", [
    ("10115", "Berlin", True),
    ("14199", "Berlin", True),
    ("14467", "Potsdam", False),    # outside Berlin
    ("10115", "Potsdam", False),    # wrong city
    ("", "Berlin", False),          # missing postcode
])
def test_is_berlin(postcode, locality, expected):
    assert cp.is_berlin(postcode, locality) is expected


def test_to_row_extracts_fields():
    row = cp.to_row(fake_place("abc"), "Hausverwaltung", "2026-11-07")
    assert row["place_id"] == "abc"
    assert row["postal_code"] == "10115"
    assert row["district"] == "Mitte"
    assert row["in_berlin"] is True
    assert row["matched_terms"] == "Hausverwaltung"


def test_duplicate_place_is_kept_once_with_both_terms():
    places = {}
    cp.add_place(places, fake_place("abc"), "Hausverwaltung", "t")
    cp.add_place(places, fake_place("abc"), "Mietverwaltung", "t")
    cp.add_place(places, fake_place("abc"), "Mietverwaltung", "t")
    assert len(places) == 1
    assert places["abc"]["matched_terms"] == "Hausverwaltung|Mietverwaltung"


# ----------------------------------------------------------------- behaviour
def test_crowded_tile_is_split_until_small_enough():
    session = FakeSession(dense_size=0.03)
    client = make_client(session)
    places, stats = cp.collect(client, "t", terms=["Hausverwaltung"])
    assert stats["tiles_split"] > 0
    assert stats["tiles_still_capped"] == 0
    assert not stats["stopped_by_limit"]
    assert len(places) > 0


def test_tile_still_capped_is_reported_when_max_depth_reached():
    session = FakeSession(dense_size=0.0)   # every tile is crowded
    client = make_client(session)
    _, stats = cp.collect(client, "t", terms=["Hausverwaltung"], start_grid=1)
    assert stats["tiles_still_capped"] > 0


def test_full_tile_outside_berlin_is_not_split():
    session = FakeSession(dense_size=0.0, postcode="14467", locality="Potsdam")
    client = make_client(session)
    _, stats = cp.collect(client, "t", terms=["Hausverwaltung"])
    assert stats["tiles_split"] == 0
    assert stats["tiles_skipped_outside_berlin"] == 16
    assert client.calls == 16 * cp.MAX_PAGES


def test_hard_stop_never_exceeds_max_calls():
    session = FakeSession(dense_size=0.0)
    client = make_client(session, max_calls=25)
    _, stats = cp.collect(client, "t")
    assert stats["stopped_by_limit"] is True
    assert client.calls <= 25
    assert session.requests <= 25


def test_bad_key_stops_cleanly_and_reports_error():
    session = FakeSession(status=403)
    client = make_client(session)
    places, stats = cp.collect(client, "t")
    assert "HTTP 403" in stats["stopped_by_error"]
    assert session.requests == 1          # 403 is not retried
    assert places == {}


def test_every_call_is_written_to_raw_log():
    session = FakeSession(dense_size=0.03)
    raw = io.StringIO()
    client = make_client(session, max_calls=12, raw_log=raw)
    cp.collect(client, "t", terms=["Hausverwaltung"])
    lines = raw.getvalue().strip().splitlines()
    assert len(lines) == client.calls
    first = json.loads(lines[0])
    assert {"call", "query", "tile", "status", "response"} <= first.keys()


def test_save_writes_csv_and_summary(tmp_path):
    session = FakeSession(dense_size=0.03)
    client = make_client(session, max_calls=12)
    places, stats = cp.collect(client, "t", terms=["Hausverwaltung"])
    summary = cp.save(places, stats, client, "t", "run1", out_dir=str(tmp_path))
    assert (tmp_path / "raw_places.csv").exists()
    assert summary["unique_places"] == len(places)
    assert summary["api_calls_used"] == client.calls