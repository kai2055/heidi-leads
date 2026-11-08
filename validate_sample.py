"""
Step 7 - Hand validation of the final grades.

1. Draw a fixed random sample (seed 11) from the scored table:
     10 A-leads, 10 C-leads, 10 companies the AI excluded ("not a property manager")
   and write it to data/validation_sample.csv with empty `verdict` and `note` columns.
       python validate_sample.py

2. You check each row against the website: does the grade / exclusion make sense?
   Type agree or disagree in `verdict` (and a short note if you disagree).

3. Score it:
       python validate_sample.py --score
"""

import argparse
import csv
import json
import os
import random

DATA_DIR = "data"
SEED = 11
PER_GROUP = 10
AI_EXCLUDED = "website shows it is not a property manager"

GROUPS = {
    "A-lead": lambda r: r["status"] == "lead" and r["grade"] == "A",
    "C-lead": lambda r: r["status"] == "lead" and r["grade"] == "C",
    "AI-excluded": lambda r: r["status"] == "excluded" and r["reason"] == AI_EXCLUDED,
}

COLUMNS = ["group", "name", "website", "grade", "total_score", "lead_type", "reason", "score_reason",
           "units_managed", "units_quote", "managing_directors", "verdict", "note"]


def sample(data_dir=DATA_DIR, seed=SEED, per_group=PER_GROUP):
    path = os.path.join(data_dir, "validation_sample.csv")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            if any(r["verdict"].strip() for r in csv.DictReader(f)):
                raise SystemExit("validation_sample.csv already has your verdicts. Use --score.")

    with open(os.path.join(data_dir, "leads_scored.csv"), encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    rng = random.Random(seed)
    picked = []
    for group, rule in GROUPS.items():
        pool = sorted((r for r in rows if rule(r)), key=lambda r: r["place_id"])
        for r in rng.sample(pool, min(per_group, len(pool))):
            picked.append({"group": group, **{c: r.get(c, "") for c in COLUMNS if c not in ("group", "verdict", "note")},
                           "verdict": "", "note": ""})

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(picked)
    return {"rows": len(picked), "saved": path}


def score(data_dir=DATA_DIR):
    with open(os.path.join(data_dir, "validation_sample.csv"), encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    result = {"checked": 0, "agree": 0, "unmarked": 0, "by_group": {}}
    for r in rows:
        v = r["verdict"].strip().lower()
        g = result["by_group"].setdefault(r["group"], {"checked": 0, "agree": 0})
        if v not in ("agree", "disagree"):
            result["unmarked"] += 1
            continue
        result["checked"] += 1
        g["checked"] += 1
        if v == "agree":
            result["agree"] += 1
            g["agree"] += 1
    result["agreement_rate"] = round(result["agree"] / result["checked"], 3) if result["checked"] else None
    result["disagreements"] = [{"group": r["group"], "name": r["name"], "note": r["note"]}
                               for r in rows if r["verdict"].strip().lower() == "disagree"]
    with open(os.path.join(data_dir, "validation_result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--score", action="store_true", help="score your verdicts")
    args = parser.parse_args()
    result = score() if args.score else sample()
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()