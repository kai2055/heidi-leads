"""
Step 6 - Score every company and build the final lead table.

Combines everything collected so far:
  enriched_candidates.csv  (Google data + rule filter)    one row per Google place
  kpis_<run>.csv           (AI facts with checked quotes)  one row per website
  crawl_status.csv         (why a website could not be read)
  pages.jsonl              (e-mail addresses shown on the websites)

1. Status: lead, competitor or excluded (with the reason).
2. Lead type: property manager, housing company, or unverified (no readable website).
3. Score out of 100 = FIT (65) + EASE (35), every point explained in `score_reason`.
   Version 2 after hand validation: size no longer penalises very large firms, hiring halved.
4. Grade: A >= 60, B 35-59, C < 35; U = unverified (not graded, sorted by Google reviews).
5. Contact: managing director and the best published e-mail (personal before general).

Output: data/leads_scored.csv, data/score_summary.json

Run
    python score_leads.py google/gemini-3.1-flash-lite
"""

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

from compare_models import kpi_path

DATA_DIR = "data"

GRADE_A, GRADE_B = 60, 35
REVIEWS_PROXY_MIN = 30          # from analyze_size_proxy.py: only separates very large firms

KNOWN_METERING = ("ista", "techem", "brunata", "minol", "kalo", "metrona", "noventic", "qundis", "calor")
VDIV_NAMES = ("vdiv", "dachverband deutscher immobilienverwalter", "verband der immobilienverwalter")
OWNER_RUN_FORMS = ("ek", "ekfm", "ekfr", "einzelunternehmen", "gbr", "inhaber")

GENERIC_MAILBOXES = {
    "info", "kontakt", "contact", "office", "mail", "post", "service", "verwaltung", "hausverwaltung",
    "buchhaltung", "vermietung", "bewerbung", "jobs", "karriere", "datenschutz", "impressum", "team",
    "zentrale", "hello", "hallo", "anfrage", "anfragen", "schaden", "schadensmeldung", "technik",
    "notdienst", "empfang", "weg", "mieter", "eigentuemer", "sekretariat", "rechnung", "rechnungen",
    "noreply", "no-reply", "webmaster", "admin", "support", "immobilien", "personal", "hv",
}
FREEMAIL = ("freenet.de", "gmx.de", "gmx.net", "web.de", "t-online.de", "gmail.com", "outlook.de", "yahoo.de")


# ----------------------------------------------------------------- helpers
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def flat(text):
    return re.sub(r"[\s.]", "", str(text).lower())


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def yes(value):
    return str(value).strip().lower() == "yes"


# ----------------------------------------------------------------- scoring
def size_points(units, reviews):
    """Size measures value only (more units = more meters). Slower decisions of big firms are
    already covered under EASE, so they are not punished here a second time (validation finding)."""
    if units > 0:
        if units < 100:
            return 5, f"{units:,} units"
        if units < 500:
            return 12, f"{units:,} units"
        if units < 3000:
            return 20, f"{units:,} units"
        return 25, f"{units:,} units"
    if reviews >= REVIEWS_PROXY_MIN:
        return 10, f"probably large ({reviews} Google reviews, proxy)"
    return 0, "unknown"


def score(k, reviews, likely_by_name, housing):
    """Return (fit, ease, parts) for one website's KPI row. parts = [(label, points, note)]."""
    parts = []

    pts, note = size_points(to_int(k.get("units_managed")), reviews)
    parts.append(("Size", pts, note))

    if yes(k.get("commercial_only")):
        pts, note = 0, "commercial only"
    elif housing or any(yes(k.get(f)) for f in ("offers_weg", "offers_rental", "offers_sev")):
        pts, note = 25, "residential services confirmed"
    elif likely_by_name:
        pts, note = 12, "name says property management"
    else:
        pts, note = 0, "not confirmed"
    parts.append(("Residential", pts, note))

    digital, signals = 0, []
    if yes(k.get("tenant_portal")):
        digital, signals = digital + 4, signals + ["portal"]
    if yes(k.get("online_repair_form")):
        digital, signals = digital + 3, signals + ["online repairs"]
    if str(k.get("software_named", "")).strip():
        digital, signals = digital + 3, signals + [f"software {k['software_named']}"]
    parts.append(("Digital", digital, ", ".join(signals)))

    provider = str(k.get("metering_provider", "")).lower()
    hook = 5 if any(m in provider for m in KNOWN_METERING) else 0
    parts.append(("Metering hook", hook, k.get("metering_provider", "") if hook else ""))

    fit = sum(p for _, p, _ in parts)

    no_vote = 10 if (yes(k.get("offers_rental")) or housing) else 0
    parts.append(("No owners' vote", no_vote, "rental management" if yes(k.get("offers_rental")) else ("owns flats" if housing else "")))

    form = flat(k.get("legal_form", ""))
    director_quote = str(k.get("managing_directors_quote", "")).lower()
    owner_run = 10 if (any(f == form for f in OWNER_RUN_FORMS) or "inhaber" in director_quote) else 0
    parts.append(("Owner-run", owner_run, k.get("legal_form", "") if owner_run else ""))

    # weak signal: 2 of 10 hand-checked A-leads showed no visible open jobs
    hiring = 5 if yes(k.get("hiring")) else 0
    parts.append(("Hiring", hiring, "open jobs" if hiring else ""))

    assoc = str(k.get("association_member", "")).lower()
    if any(v in assoc for v in VDIV_NAMES):
        a_pts, a_note = 10, "VDIV (Heidi partner)"
    elif assoc.strip():
        a_pts, a_note = 5, k["association_member"]
    else:
        a_pts, a_note = 0, ""
    parts.append(("Association", a_pts, a_note))

    ease = no_vote + owner_run + hiring + a_pts
    return fit, ease, parts


def grade(total):
    return "A" if total >= GRADE_A else ("B" if total >= GRADE_B else "C")


def explain(parts):
    return " · ".join(f"{label} {pts}" + (f" ({note})" if note else "") for label, pts, note in parts)


# ----------------------------------------------------------------- contacts
def pick_email(emails, directors, domain):
    """Best published address: one matching a director's surname, then a name-like one, then general."""
    surnames = [n.split()[-1].lower() for n in directors.split(",") if n.strip()]
    own = [e for e in emails if e.split("@")[-1].endswith(domain) or e.split("@")[-1] in FREEMAIL]
    for e in own:
        local = e.split("@")[0].lower()
        if any(s and s in local for s in surnames):
            return e, "personal (matches managing director)"
    for e in own:
        local = e.split("@")[0].lower()
        if local not in GENERIC_MAILBOXES and re.fullmatch(r"[a-zäöüß]+[._-][a-zäöüß]+|[a-z]\.?[a-zäöüß]{2,}", local):
            if not any(g in local for g in ("info", "kontakt", "service", "verwaltung")):
                return e, "personal (staff)"
    if own:
        return own[0], "general only"
    return "", "none found"


# ----------------------------------------------------------------- run
def build(run, data_dir=DATA_DIR):
    candidates = read_csv(os.path.join(data_dir, "enriched_candidates.csv"))
    kpis = read_csv(kpi_path(run, data_dir))
    crawl = {r["domain"]: r for r in read_csv(os.path.join(data_dir, "crawl_status.csv"))}

    kpi_by_place = {}
    for k in kpis:
        for pid in k["place_ids"].split("|"):
            kpi_by_place[pid] = k

    emails_by_domain = defaultdict(set)
    pages_path = os.path.join(data_dir, "pages.jsonl")
    if os.path.exists(pages_path):
        with open(pages_path, encoding="utf-8") as f:
            for line in f:
                page = json.loads(line)
                emails_by_domain[page["domain"]].update(page.get("emails", []))

    rows = []
    for c in candidates:
        k = kpi_by_place.get(c["place_id"])
        reviews = to_int(c.get("google_rating_count"))
        domain = c.get("website_domain", "")
        row = {
            "status": "lead", "grade": "", "total_score": "", "lead_type": "", "confidence": "",
            "name": c["name"], "district": c.get("district", ""), "address": c["address"],
            "website": c.get("website", ""), "phone": c.get("phone", ""),
            "google_rating": c.get("google_rating", ""), "google_reviews": reviews or "",
            "fit_score": "", "ease_score": "", "score_reason": "",
            "reason": "", "maps_url": c.get("maps_url", ""), "place_id": c["place_id"],
        }
        for field in ("units_managed", "offers_weg", "offers_rental", "offers_sev", "owns_housing_stock",
                      "tenant_portal", "online_repair_form", "software_named", "metering_provider", "hiring",
                      "association_member", "legal_form", "managing_directors"):
            row[field] = k.get(field, "") if k else ""
        row["units_quote"] = k.get("units_managed_quote", "") if k else ""
        row["units_source"] = k.get("units_managed_url", "") if k else ""
        row["contact_email"], row["email_type"] = "", ""
        directors = [n.strip() for n in row["managing_directors"].split(",") if n.strip()]
        row["contact_name"] = directors[0] if directors else ""
        row["contact_role"] = "Managing director / owner" if directors else ""
        row["contact_source"] = k.get("managing_directors_url", "") if (k and directors) else ""

        # 1. status
        if c["relevance"] == "excluded":
            row.update(status="excluded", reason=c["relevance_reason"])
        elif c["relevance"] == "competitor":
            row.update(status="competitor", reason=c["relevance_reason"])
        elif k and str(k.get("metering_provider", "")).strip().lower() == "own":
            row.update(status="competitor", reason="offers metering itself (website, quoted)")
        elif k and k.get("is_property_manager") == "no" and not yes(k.get("owns_housing_stock")):
            row.update(status="excluded", reason="website shows it is not a property manager")

        if row["status"] != "lead":
            rows.append(row)
            continue

        # 2. lead type + 3. score
        if k and (k.get("is_property_manager") in ("yes", "unclear") or yes(k.get("owns_housing_stock"))):
            housing = yes(k.get("owns_housing_stock"))
            fit, ease, parts = score(k, reviews, c["relevance"] == "likely_pm", housing)
            total = fit + ease
            row.update(
                lead_type="housing company" if housing else "property manager",
                confidence="high" if (k.get("is_property_manager") == "yes" or housing) else "medium",
                fit_score=fit, ease_score=ease, total_score=total, grade=grade(total),
                score_reason=explain(parts),
            )
        else:
            if not c.get("website"):
                why = "no website on Google" if c.get("selection_group") != "not_selected" else "not in details sample"
            elif k is None:
                status = crawl.get(domain, {}).get("status", "not crawled")
                why = f"website not readable ({status})"
            else:
                why = "website does not say what the company does"
            row.update(lead_type="unverified", confidence="low", grade="U", reason=why)

        # 5. contact
        email, kind = pick_email(sorted(emails_by_domain.get(domain, [])), row["managing_directors"], domain)
        row.update(contact_email=email, email_type=kind)
        rows.append(row)

    order = {"A": 0, "B": 1, "C": 2, "U": 3, "": 4}
    status_order = {"lead": 0, "competitor": 1, "excluded": 2}
    rows.sort(key=lambda r: (status_order[r["status"]], order[r["grade"]],
                             -to_int(r["total_score"]), -to_int(r["google_reviews"]), r["name"].lower()))

    out = os.path.join(data_dir, "leads_scored.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    leads = [r for r in rows if r["status"] == "lead"]
    summary = {
        "rows": len(rows),
        "by_status": dict(Counter(r["status"] for r in rows)),
        "leads_by_grade": dict(Counter(r["grade"] for r in leads)),
        "leads_by_type": dict(Counter(r["lead_type"] for r in leads)),
        "graded_leads": sum(1 for r in leads if r["grade"] in ("A", "B", "C")),
        "share_A_of_graded": None,
        "contacts": dict(Counter(r["email_type"] for r in leads if r["grade"] in ("A", "B"))),
    }
    graded = summary["graded_leads"]
    if graded:
        summary["share_A_of_graded"] = round(summary["leads_by_grade"].get("A", 0) / graded, 3)
    with open(os.path.join(data_dir, "score_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python score_leads.py <model id or run label>")
    summary = build(sys.argv[1])
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("\nSaved: data/leads_scored.csv")


if __name__ == "__main__":
    main()