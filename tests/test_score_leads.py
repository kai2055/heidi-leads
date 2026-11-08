"""Tests for score_leads.py."""

import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import score_leads as sl  # noqa: E402
from extract_kpis import FIELDS  # noqa: E402


def kpi(**values):
    row = {f: "" for f in FIELDS}
    row.update({f: "unknown" for f, (kind, _) in FIELDS.items() if isinstance(kind, list)})
    row.update({f"{f}_quote": "" for f in FIELDS})
    row.update({f"{f}_url": "" for f in FIELDS})
    row.update(values)
    return row


# ----------------------------------------------------------------- points
def test_size_points_bands_and_proxy():
    assert sl.size_points(50, 0)[0] == 5
    assert sl.size_points(300, 0)[0] == 12
    assert sl.size_points(1200, 0)[0] == 20
    assert sl.size_points(45000, 0)[0] == 25          # bigger is never worse on size
    assert sl.size_points(0, 56) == (10, "probably large (56 Google reviews, proxy)")
    assert sl.size_points(0, 12) == (0, "unknown")


def test_strong_lead_gets_A_with_explained_points():
    k = kpi(is_property_manager="yes", units_managed="1200", offers_weg="yes", offers_rental="yes",
            tenant_portal="yes", online_repair_form="yes", software_named="casavi",
            metering_provider="Techem", legal_form="e.K.", hiring="yes",
            association_member="Dachverband Deutscher Immobilienverwalter e.V.")
    fit, ease, parts = sl.score(k, reviews=5, likely_by_name=True, housing=False)
    assert (fit, ease) == (20 + 25 + 10 + 5, 10 + 10 + 5 + 10)
    assert sl.grade(fit + ease) == "A"
    assert "VDIV (Heidi partner)" in sl.explain(parts)


def test_unconfirmed_services_score_low():
    k = kpi(is_property_manager="yes")
    fit, ease, _ = sl.score(k, reviews=0, likely_by_name=False, housing=False)
    assert fit + ease == 0 and sl.grade(0) == "C"


def test_commercial_only_gets_no_residential_points():
    k = kpi(offers_weg="yes", commercial_only="yes")
    _, _, parts = sl.score(k, 0, True, False)
    assert dict((label, p) for label, p, _ in parts)["Residential"] == 0


def test_housing_company_counts_as_residential_and_no_vote():
    _, _, parts = sl.score(kpi(owns_housing_stock="yes"), 0, False, True)
    points = {label: p for label, p, _ in parts}
    assert points["Residential"] == 25 and points["No owners' vote"] == 10


def test_other_association_gets_half_points():
    _, ease, _ = sl.score(kpi(association_member="IVD"), 0, False, False)
    assert ease == 5


# ----------------------------------------------------------------- contacts
def test_email_matching_director_is_preferred():
    emails = ["info@hv-test.de", "m.mueller@hv-test.de", "a.schmidt@hv-test.de"]
    assert sl.pick_email(emails, "Anna Schmidt", "hv-test.de") == \
        ("a.schmidt@hv-test.de", "personal (matches managing director)")


def test_name_like_email_then_general_then_none():
    assert sl.pick_email(["info@x.de", "m.mueller@x.de"], "", "x.de")[1] == "personal (name pattern)"
    assert sl.pick_email(["info@x.de", "kontakt@x.de"], "", "x.de") == ("info@x.de", "general only")
    assert sl.pick_email([], "", "x.de") == ("", "none found")


def test_emails_from_other_domains_are_ignored_but_freemail_allowed():
    assert sl.pick_email(["peter.wudtke@freenet.de"], "Peter Wudtke", "wudtke.de")[0] == "peter.wudtke@freenet.de"
    assert sl.pick_email(["max@webagentur.de"], "", "hv.de") == ("", "none found")


# ----------------------------------------------------------------- build
def write(tmp_path, name, rows):
    with open(tmp_path / name, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def cand(pid, name, relevance="likely_pm", website="", group="all_likely", reviews=""):
    domain = website.split("//")[-1].strip("/").replace("www.", "") if website else ""
    return {"place_id": pid, "name": name, "address": "Teststr. 1, 10115 Berlin", "district": "Mitte",
            "relevance": relevance, "relevance_reason": "x", "website": website, "website_domain": domain,
            "phone": "030 1", "google_rating": "4.5", "google_rating_count": reviews,
            "selection_group": group, "maps_url": ""}


def test_build_assigns_status_type_and_grade(tmp_path):
    write(tmp_path, "enriched_candidates.csv", [
        cand("P1", "HV Gut", website="https://hv-gut.de/"),
        cand("P2", "Messdienst GmbH", website="https://mess.de/"),
        cand("P3", "Makler Nur", relevance="unclear", website="https://makler.de/", group="unclear_sample"),
        cand("P4", "HV Ohne Web", reviews="80"),
        cand("P5", "Klempner", relevance="excluded"),
        cand("P6", "Genossenschaft eG", relevance="unclear", website="https://wbg.de/", group="unclear_sample"),
    ])
    write(tmp_path, "kpis_run-x.csv", [
        kpi(domain="hv-gut.de", place_ids="P1", is_property_manager="yes", units_managed="900",
            offers_rental="yes", tenant_portal="yes", association_member="VDIV", hiring="yes"),
        kpi(domain="mess.de", place_ids="P2", is_property_manager="no", metering_provider="own"),
        kpi(domain="makler.de", place_ids="P3", is_property_manager="no"),
        kpi(domain="wbg.de", place_ids="P6", is_property_manager="no", owns_housing_stock="yes", units_managed="8027"),
    ])
    with open(tmp_path / "pages.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"domain": "hv-gut.de", "emails": ["info@hv-gut.de"]}) + "\n")

    summary = sl.build("run-x", data_dir=str(tmp_path))
    rows = {r["place_id"]: r for r in csv.DictReader(open(tmp_path / "leads_scored.csv", encoding="utf-8-sig"))}

    assert rows["P1"]["grade"] == "A" and rows["P1"]["lead_type"] == "property manager"
    assert rows["P1"]["contact_email"] == "info@hv-gut.de" and rows["P1"]["email_type"] == "general only"
    assert rows["P2"]["status"] == "competitor"
    assert rows["P3"]["status"] == "excluded"
    assert rows["P4"]["grade"] == "U" and rows["P4"]["reason"] == "no website on Google"
    assert rows["P5"]["status"] == "excluded"
    assert rows["P6"]["lead_type"] == "housing company" and rows["P6"]["grade"] == "A"   # 8,027 flats: size 25 + residential 25 + no vote 10
    assert summary["by_status"] == {"lead": 3, "competitor": 1, "excluded": 2}