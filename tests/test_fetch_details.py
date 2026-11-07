"""Tests for fetch_details.py with a fake Google API (no cost)."""

import csv
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fetch_details as fd  # noqa: E402


def cand(pid, relevance):
    return {"place_id": pid, "name": f"Firma {pid}", "relevance": relevance}


def candidates(n_likely, n_unclear, n_excluded=3):
    return ([cand(f"L{i}", "likely_pm") for i in range(n_likely)]
            + [cand(f"U{i}", "unclear") for i in range(n_unclear)]
            + [cand(f"X{i}", "excluded") for i in range(n_excluded)])


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, status_for=None):
        self.status_for = status_for or {}
        self.requests = 0

    def get(self, url, headers, timeout):
        self.requests += 1
        pid = url.rsplit("/", 1)[-1]
        status = self.status_for.get(pid, 200)
        if status != 200:
            return FakeResponse(status, {"error": "x"})
        return FakeResponse(200, {"id": pid, "websiteUri": f"https://www.{pid.lower()}.de/",
                                  "nationalPhoneNumber": "030 123", "rating": 4.5,
                                  "userRatingCount": 12})


def client(session, max_calls=10_000, raw_log=None):
    return fd.DetailsClient("fake", max_calls, session=session, raw_log=raw_log, sleep=lambda s: None)


def write_candidates(tmp_path, rows):
    with open(tmp_path / "candidates.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ----------------------------------------------------------------- selection
def test_selects_all_likely_and_fills_budget_with_unclear_sample():
    selected, groups = fd.select_places(candidates(5, 20), total_budget=10, already_done=set())
    assert len(selected) == 10
    assert sum(1 for g in groups.values() if g == "all_likely") == 5
    assert sum(1 for g in groups.values() if g == "unclear_sample") == 5
    assert not any(r["relevance"] == "excluded" for r in selected)


def test_sample_is_repeatable_with_same_seed():
    a, _ = fd.select_places(candidates(2, 50), 12, set(), seed=42)
    b, _ = fd.select_places(candidates(2, 50), 12, set(), seed=42)
    assert [r["place_id"] for r in a] == [r["place_id"] for r in b]


def test_sample_never_larger_than_unclear_group():
    selected, _ = fd.select_places(candidates(2, 3), total_budget=100, already_done=set())
    assert len(selected) == 5


def test_already_fetched_unclear_stay_in_sample():
    done = {"U7", "U9"}
    _, groups = fd.select_places(candidates(2, 50), 6, done)
    assert groups["U7"] == groups["U9"] == "unclear_sample"


# ----------------------------------------------------------------- parsing
@pytest.mark.parametrize("url,expected", [
    ("https://www.hausverwaltung-kugel.de/", "hausverwaltung-kugel.de"),
    ("http://findus-hv.de/kontakt", "findus-hv.de"),
    ("", ""),
])
def test_domain_of(url, expected):
    assert fd.domain_of(url) == expected


# ----------------------------------------------------------------- run
def test_run_fetches_writes_enriched_and_summary(tmp_path):
    write_candidates(tmp_path, candidates(3, 10))
    c = client(FakeSession())
    summary = fd.run(c, total_budget=6, data_dir=str(tmp_path))

    assert c.calls == 6
    assert summary["fetched_total"] == 6
    assert summary["with_website"] == 6
    enriched = list(csv.DictReader(open(tmp_path / "enriched_candidates.csv", encoding="utf-8")))
    assert len(enriched) == 16                      # every candidate kept
    groups = [r["selection_group"] for r in enriched]
    assert groups.count("all_likely") == 3
    assert groups.count("unclear_sample") == 3
    assert groups.count("not_selected") == 10
    likely_row = next(r for r in enriched if r["place_id"] == "L0")
    assert likely_row["website_domain"] == "l0.de"


def test_rerun_does_not_pay_twice(tmp_path):
    write_candidates(tmp_path, candidates(3, 10))
    fd.run(client(FakeSession()), total_budget=6, data_dir=str(tmp_path))
    second = client(FakeSession())
    fd.run(second, total_budget=6, data_dir=str(tmp_path))
    assert second.calls == 0


def test_hard_stop_then_resume(tmp_path):
    write_candidates(tmp_path, candidates(3, 10))
    first = client(FakeSession(), max_calls=4)
    s1 = fd.run(first, total_budget=8, data_dir=str(tmp_path))
    assert s1["stopped_by_limit"] is True
    assert first.calls == 4

    second = client(FakeSession())
    s2 = fd.run(second, total_budget=8, data_dir=str(tmp_path))
    assert second.calls == 4                        # only the missing ones
    assert s2["fetched_total"] == 8


def test_not_found_is_recorded_and_bad_key_stops(tmp_path):
    write_candidates(tmp_path, candidates(3, 0))
    s = fd.run(client(FakeSession({"L1": 404})), total_budget=3, data_dir=str(tmp_path))
    assert s["not_found"] == 1

    (tmp_path / "details.csv").unlink()
    session = FakeSession({"L0": 403})
    s = fd.run(client(session), total_budget=3, data_dir=str(tmp_path))
    assert "HTTP 403" in s["stopped_by_error"]
    assert session.requests == 1


def test_every_call_is_logged(tmp_path):
    write_candidates(tmp_path, candidates(3, 0))
    raw = io.StringIO()
    c = client(FakeSession(), raw_log=raw)
    fd.run(c, total_budget=3, data_dir=str(tmp_path))
    lines = raw.getvalue().strip().splitlines()
    assert len(lines) == c.calls == 3
    assert json.loads(lines[0])["place_id"] == "L0"