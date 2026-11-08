"""
Model and prompt comparison, with a hand-checked golden set.

1. Build the hand-check sheet: every field where two runs DISAGREE goes into
   data/model_comparison.csv with an empty `correct` column.
       python compare_models.py <run a> <run b>
   A run is a model id (google/gemini-3.1-flash-lite) or a file label
   (google-gemini-3-1-flash-lite-v2, i.e. data/kpis_<label>.csv).

2. You check each row against the website and type a / b / both / neither.

3. Score it:
       python compare_models.py --score

4. Turn the checked rows into a golden set (the known right answers):
       python compare_models.py --golden

   If a golden answer turns out too strict, add another accepted value:
       python compare_models.py --accept hausverwaltung-nesari.de legal_form ""

5. Score any later run against the golden set, with no new hand work:
       python compare_models.py --eval google/gemini-3.1-flash-lite
   (uses the current prompt version by default)
"""

import argparse
import csv
import json
import os
import sys

from extract_kpis import FIELDS, model_slug, run_label

DATA_DIR = "data"


def kpi_path(label, data_dir=DATA_DIR):
    """Find the kpis CSV for a model id or file label."""
    for candidate in (label, run_label(label), model_slug(label)):
        path = os.path.join(data_dir, f"kpis_{candidate}.csv")
        if os.path.exists(path):
            return path
    raise SystemExit(f"No kpis file found for '{label}' in {data_dir}.")


def read_rows(path):
    with open(path, encoding="utf-8") as f:
        return {r["domain"]: r for r in csv.DictReader(f)}


def norm(value):
    """Compare loosely: case, spaces and dots do not matter ('e. Kfm.' == 'e.Kfm.')."""
    text = str(value).strip().lower().replace(" ", "").replace(".", "")
    return {"mbh": "gmbh", "ekfm": "ek", "ekfr": "ek"}.get(text, text)


def same(a, b):
    return norm(a) == norm(b)


# ----------------------------------------------------------------- 1. build
def build(run_a, run_b, data_dir=DATA_DIR, force=False):
    sheet = os.path.join(data_dir, "model_comparison.csv")
    if os.path.exists(sheet) and not force and any(r["correct"].strip() for r in read_marked(data_dir)):
        raise SystemExit("model_comparison.csv already has your marks. Run --golden first to keep them, "
                         "then use --force to build a new sheet.")
    a_rows = read_rows(kpi_path(run_a, data_dir))
    b_rows = read_rows(kpi_path(run_b, data_dir))
    domains = sorted(set(a_rows) & set(b_rows))
    fields = [f for f in FIELDS if all(f in r for r in (*a_rows.values(), *b_rows.values()))]

    out, agree, total = [], 0, 0
    for domain in domains:
        a, b = a_rows[domain], b_rows[domain]
        for field in fields:
            total += 1
            if same(a[field], b[field]):
                agree += 1
                continue
            out.append({
                "domain": domain, "field": field,
                "a_value": a[field], "a_quote": a[f"{field}_quote"], "a_url": a[f"{field}_url"],
                "b_value": b[field], "b_quote": b[f"{field}_quote"], "b_url": b[f"{field}_url"],
                "correct": "",
            })

    with open(os.path.join(data_dir, "model_comparison.csv"), "w", newline="", encoding="utf-8-sig") as f:
        cols = ["domain", "field", "a_value", "a_quote", "a_url", "b_value", "b_quote", "b_url", "correct"]
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(out)

    summary = {"model_a": run_a, "model_b": run_b, "websites": len(domains),
               "fields_compared": total, "agree": agree,
               "agreement_rate": round(agree / total, 3) if total else 0,
               "disagreements_to_check": len(out)}
    with open(os.path.join(data_dir, "model_comparison_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return summary


# ----------------------------------------------------------------- 3. score
def read_marked(data_dir):
    with open(os.path.join(data_dir, "model_comparison.csv"), encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def score(data_dir=DATA_DIR):
    rows = read_marked(data_dir)
    with open(os.path.join(data_dir, "model_comparison_summary.json"), encoding="utf-8") as f:
        summary = json.load(f)

    marks = [r["correct"].strip().lower() for r in rows]
    unmarked = sum(1 for m in marks if m not in ("a", "b", "both", "neither"))
    checked = len(marks) - unmarked
    a_right = sum(1 for m in marks if m in ("a", "both"))
    b_right = sum(1 for m in marks if m in ("b", "both"))
    summary.update({
        "disagreements_checked": checked,
        "unmarked_rows": unmarked,
        "a_right": a_right, "b_right": b_right,
        "neither_right": sum(1 for m in marks if m == "neither"),
        "a_accuracy_on_disagreements": round(a_right / checked, 3) if checked else None,
        "b_accuracy_on_disagreements": round(b_right / checked, 3) if checked else None,
    })
    with open(os.path.join(data_dir, "model_comparison_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return summary


# ----------------------------------------------------------------- 4. golden set
def make_golden(data_dir=DATA_DIR):
    """Hand-checked rows -> data/golden_set.csv with every accepted answer per (domain, field)."""
    golden = []
    for r in read_marked(data_dir):
        mark = r["correct"].strip().lower()
        if mark == "a":
            accepted = [r["a_value"]]
        elif mark == "b":
            accepted = [r["b_value"]]
        elif mark == "both":
            accepted = [r["a_value"], r["b_value"]]
        else:
            continue                     # 'neither' or unmarked: no known right answer
        golden.append({"domain": r["domain"], "field": r["field"],
                       "accepted": json.dumps(sorted(set(accepted)), ensure_ascii=False)})
    path = os.path.join(data_dir, "golden_set.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["domain", "field", "accepted"])
        writer.writeheader()
        writer.writerows(golden)
    return {"golden_rows": len(golden), "saved": path}


def accept(domain, field, value, data_dir=DATA_DIR):
    """Add one more accepted answer to the golden set (for a label that was too strict)."""
    path = os.path.join(data_dir, "golden_set.csv")
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if r["domain"] == domain and r["field"] == field:
            values = set(json.loads(r["accepted"])) | {value}
            r["accepted"] = json.dumps(sorted(values), ensure_ascii=False)
            break
    else:
        raise SystemExit(f"No golden row for {domain} / {field}.")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["domain", "field", "accepted"])
        writer.writeheader()
        writer.writerows(rows)
    return {"domain": domain, "field": field, "accepted": sorted(values)}


# ----------------------------------------------------------------- 5. eval
def evaluate(run, data_dir=DATA_DIR):
    with open(os.path.join(data_dir, "golden_set.csv"), encoding="utf-8") as f:
        golden = list(csv.DictReader(f))
    rows = read_rows(kpi_path(run, data_dir))

    results, right, missing = [], 0, 0
    for g in golden:
        row = rows.get(g["domain"])
        if row is None or g["field"] not in row:
            missing += 1
            continue
        accepted = [norm(v) for v in json.loads(g["accepted"])]
        got = row[g["field"]]
        ok = norm(got) in accepted
        right += ok
        if not ok:
            results.append({"domain": g["domain"], "field": g["field"], "expected": accepted, "got": got})
    scored = len(golden) - missing
    result = {"run": run, "golden_rows": len(golden), "scored": scored, "right": right,
              "accuracy": round(right / scored, 3) if scored else None, "wrong": results}
    with open(os.path.join(data_dir, "golden_eval.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="*", help="two model ids or run labels")
    parser.add_argument("--score", action="store_true", help="score the hand-checked comparison sheet")
    parser.add_argument("--golden", action="store_true", help="turn the hand-checked sheet into a golden set")
    parser.add_argument("--eval", metavar="RUN", help="score a run against the golden set")
    parser.add_argument("--force", action="store_true", help="overwrite a hand-marked comparison sheet")
    parser.add_argument("--accept", nargs=3, metavar=("DOMAIN", "FIELD", "VALUE"),
                        help="add an accepted answer to the golden set (use \"\" for empty)")
    args = parser.parse_args()

    if args.score:
        result = score()
    elif args.golden:
        result = make_golden()
    elif args.accept:
        result = accept(*args.accept)
    elif args.eval:
        result = evaluate(args.eval)
    elif len(args.runs) == 2:
        result = build(*args.runs, force=args.force)
        print("Open data/model_comparison.csv, check each row, and type a / b / both / neither in 'correct'.")
    else:
        sys.exit("Give two runs, or one of --score, --golden, --eval RUN.")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()