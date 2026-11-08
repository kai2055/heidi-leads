"""
Test: can the Google review count stand in for company size when units are not stated?

Takes every company where the website states units_managed AND Google gives a review count,
and measures how strongly the two move together (Spearman rank correlation: 1 = perfectly
together, 0 = no relation). Rank correlation is used because both numbers are very skewed
(a few companies with thousands of units or hundreds of reviews).

Decision rule (set before looking at the result):
- rho >= 0.4  -> use review count as a size proxy (with fewer points than stated units)
- rho <  0.4  -> do not use it; size stays "unknown" when units are not stated

Input : data/kpis_<run>.csv, data/enriched_candidates.csv
Output: printed summary and data/size_proxy_test.json

Run
    python analyze_size_proxy.py google/gemini-3.1-flash-lite
"""

import json
import os
import sys

import pandas as pd

from compare_models import kpi_path

DATA_DIR = "data"
THRESHOLD = 0.4


def load(run, data_dir=DATA_DIR):
    kpis = pd.read_csv(kpi_path(run, data_dir), dtype=str).fillna("")
    cands = pd.read_csv(os.path.join(data_dir, "enriched_candidates.csv"), dtype=str).fillna("")

    # one website can belong to several Google places: use the highest review count among them
    reviews = pd.to_numeric(cands.set_index("place_id")["google_rating_count"], errors="coerce")
    rows = []
    for _, k in kpis.iterrows():
        counts = [reviews.get(pid) for pid in k["place_ids"].split("|") if pid in reviews.index]
        counts = [c for c in counts if pd.notna(c)]
        rows.append({
            "domain": k["domain"],
            "units": pd.to_numeric(k["units_managed"], errors="coerce"),
            "reviews": max(counts) if counts else float("nan"),
        })
    return pd.DataFrame(rows)


def test(df, threshold=THRESHOLD):
    both = df.dropna(subset=["units", "reviews"])
    both = both[both["units"] > 0]
    n = len(both)
    # Spearman = ordinary correlation of the ranks (no scipy needed)
    rho = both["units"].rank().corr(both["reviews"].rank()) if n >= 3 else float("nan")

    # simple table: median reviews per size band
    bands = pd.cut(both["units"], [0, 99, 499, 2999, float("inf")],
                   labels=["<100", "100-499", "500-2,999", "3,000+"])
    table = both.groupby(bands, observed=False)["reviews"].agg(["count", "median"]).reset_index()

    return {
        "companies_with_units_and_reviews": n,
        "spearman_rho": None if pd.isna(rho) else round(float(rho), 3),
        "threshold": threshold,
        "use_reviews_as_size_proxy": bool(n >= 30 and not pd.isna(rho) and rho >= threshold),
        "median_reviews_by_units_band": [
            {"units": str(r["units"]), "companies": int(r["count"]),
             "median_reviews": None if pd.isna(r["median"]) else float(r["median"])}
            for _, r in table.iterrows()
        ],
    }


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python analyze_size_proxy.py <model id or run label>")
    result = test(load(sys.argv[1]))
    with open(os.path.join(DATA_DIR, "size_proxy_test.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()