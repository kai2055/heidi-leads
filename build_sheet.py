"""
Step 8 - Build the final workbook for Heidi Systems (import into Google Sheets).

Tabs
1. Leads            graded leads (A, B, C): contact, KPIs, score and the reason for every point
2. Unverified       leads without a readable website (U), sorted by Google reviews
3. Excluded         competitors and excluded places, each with the reason
4. KPI definitions  what each KPI measures, why it matters for Heidi, source and points
5. Method & numbers pipeline funnel, validation results and costs, read from the run logs

Input : data/leads_scored.csv and the JSON logs in data/
Output: data/heidi_leads_berlin.xlsx

Run
    python build_sheet.py
"""

import csv
import json
import os
from urllib.parse import quote_plus

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

DATA_DIR = "data"
OUT = os.path.join(DATA_DIR, "heidi_leads_berlin.xlsx")

HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
HEADER_FONT = Font(bold=True, color="FFFFFF")
GRADE_FILL = {"A": "C6EFCE", "B": "FFEB9C", "C": "F2F2F2", "U": "DDEBF7"}

# (column title, source field or function, width)
LEAD_COLUMNS = [
    ("Grade", "grade", 7), ("Score", "total_score", 7), ("Lead type", "lead_type", 16),
    ("Confidence", "confidence", 11), ("Company", "name", 38), ("District", "district", 16),
    ("Address", "address", 38), ("Website", "website", 30), ("Phone", "phone", 18),
    ("Contact person", "contact_name", 24), ("Role", "contact_role", 22),
    ("Email", "contact_email", 30), ("Email type", "email_type", 30),
    ("Contact source", "contact_source", 30),
    ("LinkedIn profile (checked by hand)", None, 30), ("LinkedIn search", "linkedin_search", 18),
    ("Units managed", "units_managed", 11), ("WEG", "offers_weg", 8), ("Rental mgmt", "offers_rental", 10),
    ("SEV", "offers_sev", 8), ("Owns flats", "owns_housing_stock", 10), ("Tenant portal", "tenant_portal", 10),
    ("Online repairs", "online_repair_form", 10), ("Software", "software_named", 16),
    ("Metering provider", "metering_provider", 16), ("Hiring", "hiring", 8),
    ("Association", "association_member", 28), ("Legal form", "legal_form", 12),
    ("Google rating", "google_rating", 9), ("Google reviews", "google_reviews", 9),
    ("Fit (of 65)", "fit_score", 8), ("Ease (of 35)", "ease_score", 8),
    ("Why this score", "score_reason", 80), ("Units quote (evidence)", "units_quote", 50),
    ("Units source", "units_source", 30), ("Google Maps", "maps_url", 20),
]

UNVERIFIED_COLUMNS = [
    ("Company", "name", 40), ("District", "district", 16), ("Address", "address", 40),
    ("Website", "website", 30), ("Phone", "phone", 18), ("Google rating", "google_rating", 9),
    ("Google reviews", "google_reviews", 9), ("Why unverified", "reason", 34), ("Google Maps", "maps_url", 20),
]

EXCLUDED_COLUMNS = [
    ("Status", "status", 12), ("Company", "name", 40), ("Reason", "reason", 50),
    ("Address", "address", 40), ("Website", "website", 30), ("Google Maps", "maps_url", 20),
]

KPI_DEFINITIONS = [
    ("KPI", "What it measures", "Why it matters for Heidi", "Source", "Points"),
    ("Size", "Units managed or owned, as stated on the website; otherwise 30+ Google reviews = 'probably large'",
     "More units = more meters to install and bill", "Website (quote checked); Google reviews",
     "<100: 5 · 100-499: 12 · 500-2,999: 20 · 3,000+: 25 · proxy: 10"),
    ("Residential focus", "Offers WEG-Verwaltung, Mietverwaltung or SEV, or owns flats",
     "Heating-cost billing is a residential product", "Website (quote checked); company name",
     "confirmed: 25 · name only: 12 · commercial only: 0"),
    ("Digital maturity", "Tenant portal, online repair form, named property-management software",
     "Digital firms adopt a digital metering platform faster", "Website (quote checked)",
     "portal 4 · repairs 3 · software 3"),
    ("Metering hook", "Names its current metering provider (ista, Techem, ...)",
     "Clear 'switch to Heidi' story", "Website (quote checked)", "5"),
    ("No owners' vote", "Offers rental management, or owns the flats",
     "The owner decides alone; WEGs need an owners' meeting", "Website (quote checked)", "10"),
    ("Owner-run", "Legal form e.K., GbR, Einzelunternehmen, or 'Inhaber'",
     "One person decides: shorter sales cycle", "Website Impressum (legal form must be written)", "10"),
    ("Hiring", "Advertises open jobs", "Growth and workload: more open to tools (weak signal)",
     "Website careers page", "5"),
    ("Association", "Member of VDIV (also 'Dachverband Deutscher Immobilienverwalter') or other association",
     "Heidi is a VDIV partner: warm introduction", "Website (each name must appear)", "VDIV 10 · other 5"),
    ("", "", "", "", ""),
    ("Grade", "A >= 60 · B 35-59 · C < 35 · U = no readable website (not graded)", "", "", "Max 100 = Fit 65 + Ease 35"),
    ("Lead type", "Property manager · Housing company (owns its flats) · Unverified", "", "", ""),
    ("Confidence", "high = website confirms it manages property · medium = unclear · low = no website data", "", "", ""),
]


def read_json(name, data_dir=DATA_DIR):
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def linkedin_search(row):
    if not row.get("contact_name"):
        return ""
    company = row["name"].split(",")[0][:60]
    q = f'site:linkedin.com/in "{row["contact_name"]}" "{company}"'
    return "https://www.google.com/search?q=" + quote_plus(q)


def write_table(ws, columns, rows, grade_colour=False):
    for c, (title, _, width) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=c, value=title)
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[get_column_letter(c)].width = width
    for r, row in enumerate(rows, start=2):
        for c, (_, field, _) in enumerate(columns, start=1):
            value = row.get(field, "") if field else ""
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            cell = ws.cell(row=r, column=c, value=value)
            if isinstance(value, str) and value.startswith("http"):
                cell.hyperlink = value
                cell.font = Font(color="0563C1", underline="single")
        if grade_colour and row.get("grade") in GRADE_FILL:
            ws.cell(row=r, column=1).fill = PatternFill("solid", fgColor=GRADE_FILL[row["grade"]])
    ws.freeze_panes = "B2" if grade_colour else "A2"
    ws.auto_filter.ref = ws.dimensions


def method_rows(data_dir=DATA_DIR):
    collect = read_json("collect_log.json", data_dir)
    filt = read_json("filter_summary.json", data_dir)
    details = read_json("details_log.json", data_dir)
    crawl = read_json("crawl_log.json", data_dir)
    size = read_json("size_proxy_test.json", data_dir)
    golden = read_json("model_comparison_summary.json", data_dir)
    valid = read_json("validation_result.json", data_dir)
    final_eval = read_json("golden_eval.json", data_dir)
    score = read_json("score_summary.json", data_dir)
    labels = filt.get("by_label", {})
    grades = score.get("leads_by_grade", {})

    return [
        ("PIPELINE", ""),
        ("Places found on Google Maps (4 search terms, adaptive grid over Berlin)", collect.get("unique_places")),
        ("... of which in Berlin", collect.get("in_berlin")),
        ("Google search calls used", collect.get("api_calls_used")),
        ("Rule filter: likely property managers / unclear / competitors / excluded",
         f'{labels.get("likely_pm")} / {labels.get("unclear")} / {labels.get("competitor")} / {labels.get("excluded")}'),
        ("Companies looked up (website, phone)", details.get("fetched_total")),
        ("... with a website", details.get("with_website")),
        ("Websites crawled / read OK", f'{crawl.get("websites_crawled")} / {crawl.get("by_status", {}).get("ok")}'),
        ("Pages saved (home, Impressum, services, about, careers)", crawl.get("pages_saved")),
        ("", ""),
        ("RESULT", ""),
        ("Leads / competitors / excluded",
         f'{score.get("by_status", {}).get("lead")} / {score.get("by_status", {}).get("competitor")} / {score.get("by_status", {}).get("excluded")}'),
        ("Graded leads: A / B / C", f'{grades.get("A")} / {grades.get("B")} / {grades.get("C")}'),
        ("Unverified leads (no readable website)", grades.get("U")),
        ("Lead types", json.dumps(score.get("leads_by_type", {}), ensure_ascii=False)),
        ("Contacts for A and B leads (email type)", json.dumps(score.get("contacts", {}), ensure_ascii=False)),
        ("", ""),
        ("VALIDATION", ""),
        ("Model choice: answers compared on the same 20 websites", f'{golden.get("fields_compared")} fields, '
         f'{golden.get("agreement_rate")} agreement'),
        ("Model choice: hand-checked disagreements (golden set)",
         f'Gemini 3.1 Flash-Lite {golden.get("b_accuracy_on_disagreements")} vs Gemini 2.5 Flash-Lite '
         f'{golden.get("a_accuracy_on_disagreements")}'),
        ("Final prompt (v2 + Python value checks) on the golden set",
         f'{final_eval.get("right")} of {final_eval.get("scored")} ({final_eval.get("accuracy")})'),
        ("Quote check", "every AI answer must quote the website; quotes not found on the page are rejected"),
        ("Size proxy test (units vs Google reviews)",
         f'Spearman {size.get("spearman_rho")}, n = {size.get("companies_with_units_and_reviews")}'),
        ("Random hand check of final grades (10 A, 10 C, 10 excluded)",
         f'{valid.get("agree")} of {valid.get("checked")} confirmed ({valid.get("agreement_rate")})'),
    ]


def build(data_dir=DATA_DIR, out=None):
    out = out or os.path.join(data_dir, "heidi_leads_berlin.xlsx")
    with open(os.path.join(data_dir, "leads_scored.csv"), encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["linkedin_search"] = linkedin_search(r) if r["grade"] in ("A", "B") else ""

    graded = [r for r in rows if r["status"] == "lead" and r["grade"] in ("A", "B", "C")]
    unverified = [r for r in rows if r["status"] == "lead" and r["grade"] == "U"]
    excluded = [r for r in rows if r["status"] in ("competitor", "excluded")]
    excluded.sort(key=lambda r: (r["status"] != "competitor", r["reason"], r["name"].lower()))

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    write_table(ws, LEAD_COLUMNS, graded, grade_colour=True)
    write_table(wb.create_sheet("Unverified"), UNVERIFIED_COLUMNS, unverified)
    write_table(wb.create_sheet("Excluded"), EXCLUDED_COLUMNS, excluded)

    kpi = wb.create_sheet("KPI definitions")
    for r, line in enumerate(KPI_DEFINITIONS, start=1):
        for c, value in enumerate(line, start=1):
            cell = kpi.cell(row=r, column=c, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if r == 1:
                cell.fill, cell.font = HEADER_FILL, HEADER_FONT
    for c, width in enumerate((18, 50, 45, 34, 36), start=1):
        kpi.column_dimensions[get_column_letter(c)].width = width

    method = wb.create_sheet("Method & numbers")
    method.column_dimensions["A"].width = 70
    method.column_dimensions["B"].width = 70
    for r, (label, value) in enumerate(method_rows(data_dir), start=1):
        method.cell(row=r, column=1, value=label)
        method.cell(row=r, column=2, value=value)
        if label in ("PIPELINE", "RESULT", "VALIDATION"):
            method.cell(row=r, column=1).font = Font(bold=True)
    method.cell(row=len(method_rows(data_dir)) + 2, column=1,
                value="Code: https://github.com/kai2055/heidi-leads").font = Font(italic=True)

    wb.save(out)
    return {"saved": out, "leads": len(graded), "unverified": len(unverified), "excluded": len(excluded)}


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))