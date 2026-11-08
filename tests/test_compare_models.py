"""Tests for compare_models.py."""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import compare_models as cm  # noqa: E402
import pytest  # noqa: E402
from extract_kpis import FIELDS, model_slug  # noqa: E402


def write_kpis(tmp_path, model, overrides):
    rows = []
    for domain in ("a.de", "b.de"):
        row = {"domain": domain}
        for field in FIELDS:
            row[field] = "unknown"
            row[f"{field}_quote"] = ""
            row[f"{field}_url"] = ""
        row.update(overrides.get(domain, {}))
        rows.append(row)
    with open(tmp_path / f"kpis_{model_slug(model)}.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_only_disagreements_are_listed_and_scored(tmp_path):
    write_kpis(tmp_path, "m/a", {"a.de": {"hiring": "yes", "units_managed": "300"}})
    write_kpis(tmp_path, "m/b", {"a.de": {"hiring": "unknown", "units_managed": "300"}})
    summary = cm.build("m/a", "m/b", data_dir=str(tmp_path))
    assert summary["disagreements_to_check"] == 1
    assert summary["fields_compared"] == 2 * len(FIELDS)

    path = tmp_path / "model_comparison.csv"
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    assert rows[0]["field"] == "hiring"
    rows[0]["correct"] = "a"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    scored = cm.score(data_dir=str(tmp_path))
    assert scored["a_accuracy_on_disagreements"] == 1.0
    assert scored["b_accuracy_on_disagreements"] == 0.0
    assert scored["unmarked_rows"] == 0


def mark_all(path, mark):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    for r in rows:
        r["correct"] = mark
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_golden_set_and_eval(tmp_path):
    write_kpis(tmp_path, "m/a", {"a.de": {"hiring": "yes", "legal_form": "GmbH"}})
    write_kpis(tmp_path, "m/b", {"a.de": {"hiring": "unknown", "legal_form": "UG"}})
    cm.build("m/a", "m/b", data_dir=str(tmp_path))
    mark_all(tmp_path / "model_comparison.csv", "a")
    assert cm.make_golden(data_dir=str(tmp_path))["golden_rows"] == 2

    # a new run that fixes one of the two answers
    write_kpis(tmp_path, "m/c", {"a.de": {"hiring": "yes", "legal_form": "UG"}})
    result = cm.evaluate(model_slug("m/c"), data_dir=str(tmp_path))
    assert result["right"] == 1 and result["accuracy"] == 0.5
    assert result["wrong"][0]["field"] == "legal_form"


def test_build_refuses_to_overwrite_marks(tmp_path):
    write_kpis(tmp_path, "m/a", {"a.de": {"hiring": "yes"}})
    write_kpis(tmp_path, "m/b", {"a.de": {"hiring": "unknown"}})
    cm.build("m/a", "m/b", data_dir=str(tmp_path))
    mark_all(tmp_path / "model_comparison.csv", "b")
    with pytest.raises(SystemExit):
        cm.build("m/a", "m/b", data_dir=str(tmp_path))
    cm.build("m/a", "m/b", data_dir=str(tmp_path), force=True)      # allowed on purpose


def test_norm_ignores_spacing_and_legal_form_spelling():
    assert cm.norm("e. Kfm.") == cm.norm("e.Kfm.") == cm.norm("e.K.")
    assert cm.norm("mbH") == cm.norm("GmbH")
    assert cm.norm("Peter Wudtke") != cm.norm("Max Müller")