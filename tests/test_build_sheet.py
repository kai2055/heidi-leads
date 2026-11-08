"""Tests for build_sheet.py."""

import csv
import json
import os
import sys

from openpyxl import load_workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_sheet as bs  # noqa: E402


def row(**v):
    base = {"status": "lead", "grade": "A", "total_score": "72", "lead_type": "property manager",
            "confidence": "high", "name": "HV Test GmbH", "district": "Mitte", "address": "X 1, 10115 Berlin",
            "website": "https://hv-test.de/", "phone": "030 1", "contact_name": "Max Müller",
            "contact_role": "Managing director / owner", "contact_email": "m.mueller@hv-test.de",
            "email_type": "personal (matches managing director)", "contact_source": "https://hv-test.de/impressum",
            "units_managed": "1200", "reason": "", "maps_url": "", "score_reason": "Size 20 (1,200 units)"}
    base.update(v)
    return base


def test_workbook_has_all_tabs_and_rows(tmp_path):
    rows = [row(), row(grade="C", name="HV Klein", total_score="20"),
            row(grade="U", name="HV Ohne Web", total_score="", reason="no website on Google"),
            row(status="competitor", grade="", name="Messdienst", reason="metering service provider"),
            row(status="excluded", grade="", name="Klempner", reason="wrong trade")]
    with open(tmp_path / "leads_scored.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    (tmp_path / "score_summary.json").write_text(json.dumps({"leads_by_grade": {"A": 1}}), encoding="utf-8")

    result = bs.build(data_dir=str(tmp_path))
    assert (result["leads"], result["unverified"], result["excluded"]) == (2, 1, 2)

    wb = load_workbook(tmp_path / "heidi_leads_berlin.xlsx")
    assert wb.sheetnames == ["Leads", "Unverified", "Excluded", "KPI definitions", "Method & numbers"]
    leads = wb["Leads"]
    headers = [c.value for c in leads[1]]
    assert headers[:5] == ["Grade", "Score", "Lead type", "Confidence", "Company"]
    assert leads["B2"].value == 72                                   # numbers stay numbers
    search = leads.cell(row=2, column=headers.index("LinkedIn search") + 1).value
    assert "linkedin.com" in search and "M%C3%BCller" in search
    assert wb["Excluded"]["A2"].value == "competitor"                 # competitors listed first


def test_no_linkedin_search_without_contact_name():
    assert bs.linkedin_search({"contact_name": "", "name": "X"}) == ""