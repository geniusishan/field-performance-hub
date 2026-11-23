"""Weighted YTD scorecards at rep, territory and market level.

Score = sum(weight_k x min(attainment_k, cap)). Weights, goals, cap and tier
thresholds come from src/fph/settings.yaml. Each row also names its biggest gap
(the KPI costing it the most weighted points), so a manager knows exactly where to coach.
"""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from fph.config import settings


def _ytd_actuals_and_targets(con, plan_year: int, as_of_month: str) -> pd.DataFrame:
    return con.execute(f"""
        WITH a AS (
            SELECT rep_id, ANY_VALUE(rep_name) rep_name, ANY_VALUE(role) role,
                   ANY_VALUE(territory_id) territory_id, ANY_VALUE(territory_name) territory_name,
                   ANY_VALUE(market_id) market_id, ANY_VALUE(market_name) market_name, ANY_VALUE(region) region,
                   SUM(signings) signings, SUM(mid_large_signings) mid_large_signings,
                   SUM(new_merchant_volume_usd) new_volume, SUM(activated_90d) activated_90d,
                   SUM(activation_eligible) activation_eligible
            FROM marts.rep_monthly
            WHERE EXTRACT(YEAR FROM month) = {plan_year} AND month <= DATE '{as_of_month}'
            GROUP BY rep_id
        ),
        t AS (
            SELECT rep_id, SUM(target_signings) target_signings, SUM(target_new_volume_usd) target_new_volume
            FROM targets.rep_monthly
            WHERE plan_year = {plan_year} AND month <= DATE '{as_of_month}'
            GROUP BY rep_id
        )
        SELECT a.*, t.target_signings, t.target_new_volume
        FROM a JOIN t USING (rep_id)
    """).df()


def score(df: pd.DataFrame) -> pd.DataFrame:
    sc = settings()["scorecard"]
    k, cap = sc["kpis"], sc["attainment_cap"]
    d = df.copy()
    d["activation_rate"] = d["activated_90d"] / d["activation_eligible"].replace(0, np.nan)
    d["mid_large_mix"] = d["mid_large_signings"] / d["signings"].replace(0, np.nan)
    att = pd.DataFrame({
        "signings": d["signings"] / d["target_signings"].replace(0, np.nan),
        "new_volume": d["new_volume"] / d["target_new_volume"].replace(0, np.nan),
        "activation_90d": d["activation_rate"] / k["activation_90d"]["goal"],
        "quality_mix": d["mid_large_mix"] / k["quality_mix"]["goal"],
    }).fillna(0).clip(upper=cap)
    for c in att:
        d[f"att_{c}"] = att[c]
    weighted = sum(att[c] * k[c]["weight"] for c in att)
    d["score"] = weighted
    gaps = pd.DataFrame({c: (1 - att[c]).clip(lower=0) * k[c]["weight"] for c in att})
    d["biggest_gap"] = np.where(gaps.max(axis=1) > 0, gaps.idxmax(axis=1).map(lambda c: k[c]["label"]), "—")
    tiers = sorted(sc["tiers"], key=lambda t: -t["min_score"])
    d["tier"] = [next(t["tier"] for t in tiers if s >= t["min_score"]) for s in d["score"]]
    return d


def build(con: duckdb.DuckDBPyConnection, plan_year: int, as_of_month: str) -> dict[str, pd.DataFrame]:
    base = _ytd_actuals_and_targets(con, plan_year, as_of_month)
    rep = score(base)
    rep["rank_in_market"] = rep.groupby("market_id")["score"].rank(ascending=False, method="min").astype(int)
    rep["rank_global"] = rep["score"].rank(ascending=False, method="min").astype(int)

    sum_cols = ["signings", "mid_large_signings", "new_volume", "activated_90d", "activation_eligible",
                "target_signings", "target_new_volume"]
    terr = score(base.groupby(["territory_id", "territory_name", "market_id", "market_name", "region"],
                              as_index=False)[sum_cols].sum())
    terr["rank_in_market"] = terr.groupby("market_id")["score"].rank(ascending=False, method="min").astype(int)
    market = score(base.groupby(["market_id", "market_name", "region"], as_index=False)[sum_cols].sum())
    market["rank_global"] = market["score"].rank(ascending=False, method="min").astype(int)

    out = {"rep": rep, "territory": terr, "market": market}
    con.execute("CREATE SCHEMA IF NOT EXISTS scorecard")
    for name, df in out.items():
        df = df.assign(plan_year=plan_year, as_of_month=pd.Timestamp(as_of_month).date())
        con.execute(f"CREATE OR REPLACE TABLE scorecard.{name} AS SELECT * FROM df")
        out[name] = df
    return out
