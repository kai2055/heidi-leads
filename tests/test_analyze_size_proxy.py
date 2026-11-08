"""Tests for analyze_size_proxy.py."""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analyze_size_proxy as ap  # noqa: E402


def frame(pairs):
    return pd.DataFrame([{"domain": f"d{i}", "units": u, "reviews": r} for i, (u, r) in enumerate(pairs)])


def test_strong_relation_is_accepted():
    pairs = [(u, u // 10) for u in range(50, 5000, 100)]          # 50 companies, reviews grow with units
    result = ap.test(frame(pairs))
    assert result["spearman_rho"] > 0.9
    assert result["use_reviews_as_size_proxy"] is True


def test_no_relation_is_rejected():
    pairs = [(u, (u * 7919) % 97) for u in range(50, 5000, 100)]  # reviews unrelated to units
    result = ap.test(frame(pairs))
    assert result["use_reviews_as_size_proxy"] is False


def test_too_few_companies_is_rejected_even_if_correlated():
    result = ap.test(frame([(100, 1), (500, 5), (1000, 10), (2000, 20)]))
    assert result["use_reviews_as_size_proxy"] is False


def test_missing_values_are_ignored():
    df = frame([(100, 1), (500, 5), (1000, 10)])
    df.loc[3] = {"domain": "x", "units": float("nan"), "reviews": 99}
    assert ap.test(df)["companies_with_units_and_reviews"] == 3