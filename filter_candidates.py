"""
Step 2a - Rule-based filter: is this place really a property management company?

Google Text Search matches loosely, so raw results include plumbers, lawyers,
builders, estate agents and metering companies. This step labels every row.
It never deletes anything: each row gets a label and a reason, so the decision
can be checked.

Labels
- likely_pm   name clearly says property management (Hausverwaltung, ...)
- unclear     could be a property manager; the website check (later step) decides
- competitor  metering service provider (ista, Techem, KALO, ...): not a lead
- excluded    outside Berlin, permanently closed, building listing, or wrong trade

Input : data/raw_places.csv
Output: data/candidates.csv, data/filter_summary.json

Run
    python filter_candidates.py
"""

import csv
import json
import os
import re
from collections import Counter

IN_CSV = os.path.join("data", "raw_places.csv")
OUT_CSV = os.path.join("data", "candidates.csv")
OUT_SUMMARY = os.path.join("data", "filter_summary.json")

# Name says property management -> likely a lead
PM_PATTERN = re.compile(
    r"verwaltung|verwalter|property\s*management|immobilienmanagement|"
    r"hausverwalt|\bweg\b",
    re.IGNORECASE,
)

# Metering service providers = Heidi's competitors
COMPETITOR_PATTERN = re.compile(
    r"\bista\b|techem|\bkalo\b|calor\s*systems|brunata|metrona|minol|"
    r"noventic|qundis|messdienst|heidi\s*systems|\bmetr\b|metiundo",
    re.IGNORECASE,
)

# Wrong trade by name (only used when the name has no property-management word)
WRONG_TRADE_NAME = re.compile(
    r"rechtsanw|anwalt|kanzlei|notar|steuerberat|architekt|ingenieurb|"
    r"bauservice|innenausbau|ausbauprofis|sanitär|heizungsbau|elektro|"
    r"maler|dachdeck|reinigung|umzug|\bit\b|software",
    re.IGNORECASE,
)

# Wrong trade by Google type
WRONG_TRADE_TYPES = {
    "plumber", "lawyer", "electrician", "painter", "roofing_contractor",
    "general_contractor", "health", "hospital", "doctor", "dentist",
    "restaurant", "store", "lodging", "car_repair", "moving_company",
    "locksmith", "accounting",
}

# A building, not a company
BUILDING_TYPES = {"apartment_complex", "apartment_building", "housing_complex"}

# Estate agents: some also manage property, so they stay "unclear"
AGENT_PATTERN = re.compile(r"makler", re.IGNORECASE)

# Housing companies and cooperatives own and manage flats: possible leads
HOUSING_PATTERN = re.compile(
    r"genossenschaft|wohnungsbau|wohnbau|wohnungsunternehmen|\beg\b|e\.g\.",
    re.IGNORECASE,
)

# Property-management words that are safe to look for in the address line too
# (Google sometimes puts the legal company name there, e.g. "... Property Management GmbH, Street 9")
PM_IN_ADDRESS = re.compile(r"verwaltung|property\s*management", re.IGNORECASE)

# If Google also tags it as a real estate business, a trade type alone is not enough to exclude
REAL_ESTATE_TYPES = {"real_estate_agency"}


def label(row):
    """Return (label, reason) for one raw place row."""
    name = row.get("name", "")
    types = set(filter(None, row.get("types", "").split("|")))

    if str(row.get("in_berlin", "")).lower() != "true":
        return "excluded", "outside Berlin"
    if row.get("business_status") == "CLOSED_PERMANENTLY":
        return "excluded", "permanently closed"
    if COMPETITOR_PATTERN.search(name):
        return "competitor", "metering service provider"
    if types & BUILDING_TYPES:
        return "excluded", "building listing, not a company"

    is_pm_name = bool(PM_PATTERN.search(name))
    is_pm_address = bool(PM_IN_ADDRESS.search(row.get("address", "")))
    is_housing = bool(HOUSING_PATTERN.search(name))

    if not (is_pm_name or is_pm_address or is_housing):
        bad_types = types & WRONG_TRADE_TYPES
        if bad_types and not (types & REAL_ESTATE_TYPES):
            return "excluded", f"wrong trade (Google type: {', '.join(sorted(bad_types))})"
        if WRONG_TRADE_NAME.search(name):
            return "excluded", "wrong trade (name)"

    if is_pm_name:
        return "likely_pm", "name mentions property management"
    if is_pm_address:
        return "likely_pm", "company name in address mentions property management"
    if is_housing:
        return "unclear", "housing company or cooperative; check website"
    if AGENT_PATTERN.search(name):
        return "unclear", "estate agent; may also manage property"
    return "unclear", "name does not say; check website"


def normalise_address(address):
    return re.sub(r"[^a-z0-9]", "", address.lower())


def run(in_csv=IN_CSV, out_csv=OUT_CSV, out_summary=OUT_SUMMARY):
    with open(in_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Same address used by several listings (e.g. agent + property manager of one office)
    address_counts = Counter(normalise_address(r["address"]) for r in rows)

    for row in rows:
        row["relevance"], row["relevance_reason"] = label(row)
        row["shared_address"] = address_counts[normalise_address(row["address"])] > 1

    order = {"likely_pm": 0, "unclear": 1, "competitor": 2, "excluded": 3}
    rows.sort(key=lambda r: (order[r["relevance"]], r["name"].lower()))

    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "input_rows": len(rows),
        "by_label": dict(Counter(r["relevance"] for r in rows)),
        "by_reason": dict(Counter(r["relevance_reason"] for r in rows)),
        "shared_address_rows": sum(1 for r in rows if r["shared_address"]),
    }
    with open(out_summary, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nSaved: {OUT_CSV}")