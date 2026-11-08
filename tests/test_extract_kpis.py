"""Tests for extract_kpis.py with a fake OpenRouter API (no cost)."""

import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import extract_kpis as ek  # noqa: E402

PAGES = [
    {"page_type": "home", "url": "https://hv-test.de/",
     "text": "Hausverwaltung Test GmbH. Wir verwalten über 1.200 Wohneinheiten in Berlin. "
             "WEG-Verwaltung und Mietverwaltung aus einer Hand. Unser Mieterportal ist rund um die Uhr erreichbar."},
    {"page_type": "impressum", "url": "https://hv-test.de/impressum",
     "text": "Impressum Hausverwaltung Test GmbH Geschäftsführer: Max Müller Mitglied im VDIV Berlin-Brandenburg"},
]


def answer(**overrides):
    """A full, correct model answer; override single fields to test failures."""
    base = {name: {"value": ("" if kind == "text" else 0 if kind == "int" else "unknown"),
                   "quote": "", "source_url": ""}
            for name, (kind, _) in ek.FIELDS.items()}
    base.update({
        "is_property_manager": {"value": "yes", "quote": "WEG-Verwaltung und Mietverwaltung aus einer Hand",
                                "source_url": "https://hv-test.de/"},
        "offers_weg": {"value": "yes", "quote": "WEG-Verwaltung", "source_url": "https://hv-test.de/"},
        "units_managed": {"value": 1200, "quote": "Wir verwalten über 1.200 Wohneinheiten",
                          "source_url": "https://hv-test.de/"},
        "tenant_portal": {"value": "yes", "quote": "Unser Mieterportal ist rund um die Uhr erreichbar",
                          "source_url": "https://hv-test.de/"},
        "managing_directors": {"value": "Max Müller", "quote": "Geschäftsführer: Max Müller",
                               "source_url": "https://hv-test.de/impressum"},
        "association_member": {"value": "VDIV", "quote": "Mitglied im VDIV Berlin-Brandenburg",
                               "source_url": "https://hv-test.de/impressum"},
    })
    base.update(overrides)
    return base


# ----------------------------------------------------------------- quote check
def test_correct_answers_pass_the_check():
    checked, failed = ek.check_answers(answer(), PAGES)
    assert failed == []
    assert checked["units_managed"]["value"] == 1200
    assert checked["managing_directors"]["value"] == "Max Müller"


def test_invented_quote_is_rejected():
    bad = answer(software_named={"value": "casavi", "quote": "Wir nutzen casavi",
                                 "source_url": "https://hv-test.de/"})
    checked, failed = ek.check_answers(bad, PAGES)
    assert failed == ["software_named"]
    assert checked["software_named"]["value"] == ""


def test_number_must_appear_in_quote():
    bad = answer(units_managed={"value": 5000, "quote": "Wir verwalten über 1.200 Wohneinheiten",
                                "source_url": "https://hv-test.de/"})
    checked, failed = ek.check_answers(bad, PAGES)
    assert failed == ["units_managed"]
    assert checked["units_managed"]["value"] == 0


def test_quote_check_ignores_case_and_spacing():
    ok = answer(offers_weg={"value": "yes", "quote": "weg-verwaltung   UND mietverwaltung",
                            "source_url": "https://hv-test.de/"})
    _, failed = ek.check_answers(ok, PAGES)
    assert failed == []


def test_quote_on_another_page_of_same_site_is_accepted():
    ok = answer(managing_directors={"value": "Max Müller", "quote": "Geschäftsführer: Max Müller",
                                    "source_url": "https://hv-test.de/"})     # wrong page given
    _, failed = ek.check_answers(ok, PAGES)
    assert failed == []


def test_value_outside_allowed_list_becomes_unknown():
    checked, _ = ek.check_answers(answer(hiring={"value": "maybe", "quote": "", "source_url": ""}), PAGES)
    assert checked["hiring"]["value"] == "unknown"


def test_unit_number_given_as_text_is_parsed():
    ok = answer(units_managed={"value": "1.200", "quote": "Wir verwalten über 1.200 Wohneinheiten",
                               "source_url": "https://hv-test.de/"})
    checked, failed = ek.check_answers(ok, PAGES)
    assert failed == [] and checked["units_managed"]["value"] == 1200


# ----------------------------------------------------------------- prompt
def test_user_message_respects_total_limit():
    pages = [{"page_type": "home", "url": "u1", "text": "a" * 20_000},
             {"page_type": "about", "url": "u2", "text": "b" * 20_000},
             {"page_type": "careers", "url": "u3", "text": "c" * 20_000}]
    msg = ek.build_user_message(["X"], pages)
    assert msg.count("a") >= ek.MAX_PAGE_CHARS
    assert len(msg) < ek.MAX_TOTAL_CHARS + 1_000


def test_schema_lists_every_field():
    schema = ek.json_schema()
    assert set(schema["required"]) == set(ek.FIELDS)


def test_parse_json_strips_code_fences():
    assert ek.parse_json('```json\n{"a": 1}\n```') == {"a": 1}


# ----------------------------------------------------------------- fake API
class FakeResponse:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text or json.dumps(data or {})

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, cost=0.001, status=200, reject_schema=False):
        self.cost = cost
        self.status = status
        self.reject_schema = reject_schema
        self.bodies = []

    def post(self, url, headers, json, timeout):
        self.bodies.append(json)
        if self.status != 200:
            return FakeResponse(self.status, text="error")
        if self.reject_schema and json["response_format"]["type"] == "json_schema":
            return FakeResponse(400, text="response_format json_schema not supported")
        content = __import__("json").dumps(answer(), ensure_ascii=False)
        return FakeResponse(200, {"choices": [{"message": {"content": content}}],
                                  "usage": {"prompt_tokens": 3000, "completion_tokens": 400, "cost": self.cost}})


def write_data(tmp_path, n_sites=5):
    with open(tmp_path / "pages.jsonl", "w", encoding="utf-8") as f:
        for i in range(n_sites):
            for page in PAGES:
                rec = {**page, "domain": f"site{i}.de", "place_ids": [f"P{i}"]}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(tmp_path / "enriched_candidates.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["place_id", "name"])
        writer.writeheader()
        writer.writerows({"place_id": f"P{i}", "name": f"Hausverwaltung {i}"} for i in range(n_sites))


def client(session, max_cost=10.0, model="test/model-a"):
    return ek.OpenRouterClient("fake", model, max_cost, session=session, sleep=lambda s: None)


def test_run_writes_csv_and_summary(tmp_path):
    write_data(tmp_path)
    summary = ek.run(client(FakeSession()), data_dir=str(tmp_path), workers=1)
    assert summary["websites_extracted"] == 5
    assert summary["failed_quote_checks"] == 0
    assert summary["cost_this_run_usd"] == pytest.approx(0.005)
    rows = list(csv.DictReader(open(tmp_path / f"kpis_{ek.run_label('test/model-a')}.csv", encoding="utf-8")))
    assert rows[0]["units_managed"] == "1200"
    assert rows[0]["managing_directors_quote"] == "Geschäftsführer: Max Müller"


def test_same_sample_for_every_model(tmp_path):
    write_data(tmp_path, n_sites=30)
    ek.run(client(FakeSession(), model="test/a"), data_dir=str(tmp_path), sample=10, workers=1)
    ek.run(client(FakeSession(), model="test/b"), data_dir=str(tmp_path), sample=10, workers=1)
    a = {r["domain"] for r in csv.DictReader(open(tmp_path / f"kpis_{ek.run_label('test/a')}.csv", encoding="utf-8"))}
    b = {r["domain"] for r in csv.DictReader(open(tmp_path / f"kpis_{ek.run_label('test/b')}.csv", encoding="utf-8"))}
    assert a == b and len(a) == 10


def test_resume_skips_done_websites(tmp_path):
    write_data(tmp_path)
    ek.run(client(FakeSession()), data_dir=str(tmp_path), workers=1)
    session = FakeSession()
    ek.run(client(session), data_dir=str(tmp_path), workers=1)
    assert session.bodies == []


def test_cost_hard_stop(tmp_path):
    write_data(tmp_path, n_sites=10)
    c = client(FakeSession(cost=0.01), max_cost=0.03)
    summary = ek.run(c, data_dir=str(tmp_path), workers=1)
    assert summary["stopped_by_limit"] is True
    assert summary["websites_extracted"] == 3
    assert c.cost <= 0.03 + 1e-9


def test_no_credits_stops_the_run(tmp_path):
    write_data(tmp_path)
    summary = ek.run(client(FakeSession(status=402)), data_dir=str(tmp_path), workers=1)
    assert "402" in summary["stopped_by_error"]
    assert summary["websites_extracted"] == 0


def test_falls_back_to_json_mode_when_schema_not_supported(tmp_path):
    write_data(tmp_path, n_sites=1)
    session = FakeSession(reject_schema=True)
    summary = ek.run(client(session), data_dir=str(tmp_path), workers=1)
    assert summary["websites_extracted"] == 1
    assert session.bodies[-1]["response_format"] == {"type": "json_object"}


def test_website_text_is_sent_as_user_data_not_system(tmp_path):
    write_data(tmp_path, n_sites=1)
    session = FakeSession()
    ek.run(client(session), data_dir=str(tmp_path), workers=1)
    messages = session.bodies[0]["messages"]
    assert "rund um die Uhr" not in messages[0]["content"]
    assert "rund um die Uhr" in messages[1]["content"]


def test_pages_from_directories_are_not_sent(tmp_path):
    write_data(tmp_path, n_sites=2)
    with open(tmp_path / "pages.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"domain": "firmenfreund.info", "place_ids": ["P9"], "page_type": "home",
                            "url": "https://makler-finden.com/123-firma", "text": "Vertreten durch Fremder Name"}) + "\n")
    session = FakeSession()
    summary = ek.run(client(session), data_dir=str(tmp_path), workers=1)
    assert summary["websites_skipped_directory_only"] == 1
    assert summary["websites_extracted"] == 2
    assert all("Fremder Name" not in b["messages"][1]["content"] for b in session.bodies)


def test_run_label_includes_prompt_version():
    assert ek.run_label("google/gemini-3.1-flash-lite", "v2") == "google-gemini-3-1-flash-lite-v2"


# ----------------------------------------------------------------- value checks (v2)
def test_legal_form_must_be_written_in_quote():
    pages = PAGES + [{"page_type": "impressum", "url": "https://hv-test.de/rechtliches",
                      "text": "Registergericht: AG Charlottenburg HRB 17126. Versicherung: Funk Versicherungsmakler GmbH"}]
    guessed = answer(legal_form={"value": "GmbH", "quote": "Registergericht: AG Charlottenburg HRB 17126",
                                 "source_url": "https://hv-test.de/rechtliches"})
    checked, failed = ek.check_answers(guessed, pages)
    assert "legal_form" in failed and checked["legal_form"]["value"] == ""

    stated = answer(legal_form={"value": "GmbH", "quote": "Hausverwaltung Test GmbH",
                                "source_url": "https://hv-test.de/"})
    checked, failed = ek.check_answers(stated, pages)
    assert failed == [] and checked["legal_form"]["value"] == "GmbH"


def test_association_not_on_website_is_trimmed():
    extra = answer(association_member={"value": "VDIV, Verwalterverband Berlin-Brandenburg",
                                       "quote": "Mitglied im VDIV Berlin-Brandenburg",
                                       "source_url": "https://hv-test.de/impressum"})
    checked, failed = ek.check_answers(extra, PAGES)
    assert checked["association_member"]["value"] == "VDIV"
    assert failed == ["association_member:trimmed"]


def test_director_not_on_website_fails():
    invented = answer(managing_directors={"value": "Erika Muster", "quote": "Geschäftsführer: Max Müller",
                                          "source_url": "https://hv-test.de/impressum"})
    checked, failed = ek.check_answers(invented, PAGES)
    assert "managing_directors" in failed and checked["managing_directors"]["value"] == ""


def test_recheck_applies_new_rules_without_api_calls(tmp_path):
    write_data(tmp_path, n_sites=1)
    ek.run(client(FakeSession()), data_dir=str(tmp_path), workers=1)
    raw_path = tmp_path / f"extractions_{ek.run_label('test/model-a')}.jsonl"
    rec = json.loads(raw_path.read_text(encoding="utf-8").strip())
    rec["raw_answer"]["legal_form"] = {"value": "GmbH", "quote": "Geschäftsführer: Max Müller",
                                       "source_url": "https://hv-test.de/impressum"}
    rec["checked"]["legal_form"] = rec["raw_answer"]["legal_form"]      # as if an old rule had accepted it
    raw_path.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")

    result = ek.recheck("test/model-a", data_dir=str(tmp_path))
    assert result["failed_or_trimmed_after"] == 1
    rows = list(csv.DictReader(open(tmp_path / f"kpis_{ek.run_label('test/model-a')}.csv", encoding="utf-8")))
    assert rows[0]["legal_form"] == ""