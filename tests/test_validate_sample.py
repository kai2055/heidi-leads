"""Tests for validate_sample.py."""

import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import validate_sample as vs  # noqa: E402


def write_scored(tmp_path):
    rows = []
    for i in range(15):
        rows.append({"place_id": f"A{i}", "status": "lead", "grade": "A", "reason": "", "name": f"A {i}"})
        rows.append({"place_id": f"C{i}", "status": "lead", "grade": "C", "reason": "", "name": f"C {i}"})
        rows.append({"place_id": f"X{i}", "status": "excluded", "grade": "", "reason": vs.AI_EXCLUDED, "name": f"X {i}"})
        rows.append({"place_id": f"O{i}", "status": "excluded", "grade": "", "reason": "outside Berlin", "name": f"O {i}"})
    with open(tmp_path / "leads_scored.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_sample_takes_ten_per_group_and_is_repeatable(tmp_path):
    write_scored(tmp_path)
    vs.sample(data_dir=str(tmp_path))
    first = list(csv.DictReader(open(tmp_path / "validation_sample.csv", encoding="utf-8-sig")))
    assert [r["group"] for r in first].count("A-lead") == 10
    assert [r["group"] for r in first].count("AI-excluded") == 10
    assert not any(r["name"].startswith("O") for r in first)       # rule exclusions are not sampled

    os.remove(tmp_path / "validation_sample.csv")
    vs.sample(data_dir=str(tmp_path))
    second = list(csv.DictReader(open(tmp_path / "validation_sample.csv", encoding="utf-8-sig")))
    assert [r["name"] for r in first] == [r["name"] for r in second]


def test_score_and_protect_verdicts(tmp_path):
    write_scored(tmp_path)
    vs.sample(data_dir=str(tmp_path))
    path = tmp_path / "validation_sample.csv"
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    for i, r in enumerate(rows):
        r["verdict"] = "disagree" if i == 0 else "agree"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    result = vs.score(data_dir=str(tmp_path))
    assert result["checked"] == 30 and result["agree"] == 29
    assert len(result["disagreements"]) == 1
    with pytest.raises(SystemExit):
        vs.sample(data_dir=str(tmp_path))                          # verdicts are not overwritten