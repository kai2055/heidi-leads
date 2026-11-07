"""Tests for filter_candidates.py. Cases are taken from the first real test run."""

import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import filter_candidates as fc  # noqa: E402


def row(name, types="point_of_interest|establishment", in_berlin="True",
        status="OPERATIONAL", address="Teststr. 1, 10115 Berlin"):
    return {"name": name, "types": types, "in_berlin": in_berlin,
            "business_status": status, "address": address}


@pytest.mark.parametrize("r,expected", [
    # property managers
    (row("FINDUS Hausverwaltung GmbH"), "likely_pm"),
    (row("Frank Behrend Wohnungsverwaltung GmbH"), "likely_pm"),
    (row("HYLLA immobilienmanagement"), "likely_pm"),
    (row("WIB · WEG & Immobilienverwaltung Breuing GmbH"), "likely_pm"),
    (row("sw61", address="Südwest 61 Property Management GmbH & Co. KG, Chausseestraße 9, 14109 Berlin"),
     "likely_pm"),
    # a property manager that also does caretaking is still a property manager
    (row("Dierig Hausverwaltung und Gebäudeservice UG", types="general_contractor|service"), "likely_pm"),
    # competitors
    (row("KALO Gebietsleitung Berlin-Süd calor systems gmbh"), "competitor"),
    (row("ista Deutschland GmbH"), "competitor"),
    (row("Techem Energy Services"), "competitor"),
    # wrong trades
    (row("Mike Pissarek", types="plumber|service"), "excluded"),
    (row("Rechtsanwältin Karen Kittner", types="lawyer|service"), "excluded"),
    (row("Semmer Architekten"), "excluded"),
    (row("PITNAS - Proton IT Network and Solution GmbH"), "excluded"),
    (row("HOWOGE Quartier Anne-Frank-Straße", types="apartment_complex|service"), "excluded"),
    # unclear: website decides
    (row("Biber Immobilien GmbH"), "unclear"),
    (row("K. F. Dähne Immobilienmakler", types="real_estate_agency"), "unclear"),
    (row("Görner Immobilien Dienstleistungsgesellschaft mbH",
         types="general_contractor|real_estate_agency"), "unclear"),
    (row("Wohnungsgenossenschaft Altglienicke eG", types="lodging|service"), "unclear"),
    # outside Berlin / closed
    (row("Hausverwaltung Potsdam", in_berlin="False"), "excluded"),
    (row("Hausverwaltung Alt", status="CLOSED_PERMANENTLY"), "excluded"),
])
def test_label(r, expected):
    assert fc.label(r)[0] == expected


def test_address_street_named_weg_does_not_count_as_pm():
    # "WEG" in a street name must not make a plumber a property manager
    r = row("Mike Pissarek", types="plumber", address="Neudecker Weg 43A, 12355 Berlin")
    assert fc.label(r)[0] == "excluded"


def test_run_labels_every_row_and_flags_shared_address(tmp_path):
    raw = tmp_path / "raw.csv"
    rows = [
        row("GV Daehne - Hausverwaltung", address="Prierosser Str. 71, 12355 Berlin"),
        row("K. F. Dähne Immobilienmakler", address="Prierosser Str. 71, 12355 Berlin"),
        row("Mike Pissarek", types="plumber"),
    ]
    with open(raw, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    out = tmp_path / "cand.csv"
    summary = fc.run(str(raw), str(out), str(tmp_path / "sum.json"))

    assert summary["input_rows"] == 3
    assert summary["shared_address_rows"] == 2
    labelled = list(csv.DictReader(open(out, encoding="utf-8")))
    assert all(r["relevance"] and r["relevance_reason"] for r in labelled)
    assert labelled[0]["relevance"] == "likely_pm"          # sorted: best first
    assert json.load(open(tmp_path / "sum.json"))["by_label"]["excluded"] == 1