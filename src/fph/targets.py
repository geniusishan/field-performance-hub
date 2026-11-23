"""Explainable target setting: market forecast -> territory -> rep -> month.

1. Market plan  = back-tested forecast for the plan year x (1 + stretch).
2. Territory split = weighted blend of
       historical share (what it delivered in the last 12 months),
       white-space share (merchants in its universe not yet signed),
       capacity share (ramp-adjusted rep headcount),
   with a guard-rail: a territory's share of the market plan moves at most +/-max_change
   (relative) vs its share of last year's actuals, so no territory gets a shock re-allocation.
3. Monthly phasing follows the market's forecast seasonality.
4. Rep targets split each territory-month by ramp-adjusted capacity, so a new hire
   carries 50% of a tenured rep's target, rising to 100% after a year.
5. New-merchant volume target = historical "volume per YTD signing" curve x cumulative
   signings target, so volume and signings targets are consistent with each other.

Every component is stored in targets.territory_plan, so any target can be explained
line by line to a sales leader.
"""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from fph import forecast as fc_mod
from fph.config import settings


def _ramp_factor(tenure_months: np.ndarray, ramp_cfg: list[dict]) -> np.ndarray:
    out = np.zeros_like(tenure_months, dtype=float)
    lower = -np.inf
    for band in ramp_cfg:
        mask = (tenure_months >= lower) & (tenure_months < band["max_tenure_months"]) & (tenure_months >= 0)
        out[mask] = band["factor"]
        lower = band["max_tenure_months"]
    return out


def _cap_and_renormalise(w: pd.Series, prior_share: pd.Series, max_change: float, iters: int = 50) -> pd.Series:
    """Clip each territory's share to +/-max_change of last year's share, keep the total at 1."""
    lo, hi = prior_share * (1 - max_change), prior_share * (1 + max_change)
    w = w.copy()
    for _ in range(iters):
        clipped = w.clip(lower=lo, upper=hi)
        free = (clipped > lo + 1e-12) & (clipped < hi - 1e-12)
        gap = 1 - clipped.sum()
        if abs(gap) < 1e-10 or not free.any():
            return clipped
        clipped[free] += gap * clipped[free] / clipped[free].sum()
        w = clipped
    return w / w.sum()


def set_targets(con: duckdb.DuckDBPyConnection, plan_year: int, as_of_month: str) -> pd.DataFrame:
    cfg = settings()
    tcfg, fcfg = cfg["targets"], dict(cfg["forecast"])
    as_of = pd.Timestamp(as_of_month).to_period("M").to_timestamp()
    plan_months = pd.date_range(f"{plan_year}-01-01", f"{plan_year}-12-01", freq="MS")
    fcfg["horizon_months"] = (plan_months[-1].to_period("M") - as_of.to_period("M")).n

    mm = con.execute("SELECT market_id, month, signings FROM marts.market_monthly ORDER BY 1, 2").df()
    mm["month"] = pd.to_datetime(mm["month"])
    hist = mm[mm["month"] <= as_of]

    # 1. market plan with monthly phasing ---------------------------------------
    market_plan = []
    for mid, g in hist.groupby("market_id"):
        y = g.set_index("month")["signings"].astype(float).asfreq("MS").fillna(0)
        f, _ = fc_mod.forecast_market(y, fcfg)
        f = f[f["month"].isin(plan_months)]
        market_plan.append(pd.DataFrame({
            "market_id": mid, "month": f["month"],
            "market_target": f["forecast"].to_numpy() * (1 + tcfg["stretch_factor"]),
            "forecast_model": f["model"].to_numpy(),
        }))
    market_plan = pd.concat(market_plan, ignore_index=True)

    # 2. territory weights ---------------------------------------------------------
    rm = con.execute("""
        SELECT r.month, r.rep_id, r.territory_id, r.market_id, r.signings
        FROM marts.rep_monthly r
    """).df()
    rm["month"] = pd.to_datetime(rm["month"])
    last12 = rm[(rm["month"] > as_of - pd.DateOffset(months=12)) & (rm["month"] <= as_of)]
    terr = con.execute("""
        SELECT DISTINCT territory_id, market_id, territory_name, merchant_universe FROM marts.dim_rep
    """).df()
    signed_to_date = rm[rm["month"] <= as_of].groupby("territory_id")["signings"].sum()
    hist12 = last12.groupby("territory_id")["signings"].sum()
    terr["prior_12m_signings"] = terr["territory_id"].map(hist12).fillna(0)
    terr["whitespace"] = (terr["merchant_universe"] - terr["territory_id"].map(signed_to_date).fillna(0)).clip(lower=0)

    reps = con.execute("SELECT rep_id, territory_id, hire_date FROM marts.dim_rep").df()
    reps["hire_date"] = pd.to_datetime(reps["hire_date"])
    reps_now = reps[reps["hire_date"] <= as_of].copy()
    reps_now["ramp"] = _ramp_factor(((plan_months[0] - reps_now["hire_date"]).dt.days / 30.44).to_numpy(),
                                    tcfg["rep_ramp"])
    terr["capacity"] = terr["territory_id"].map(reps_now.groupby("territory_id")["ramp"].sum()).fillna(0)

    aw = tcfg["allocation_weights"]
    parts = []
    for mid, g in terr.groupby("market_id"):
        g = g.copy()
        for col, key in (("prior_12m_signings", "historical_share"), ("whitespace", "whitespace_share"),
                         ("capacity", "capacity_share")):
            g[key] = g[col] / g[col].sum() if g[col].sum() else 1 / len(g)
        g["blended_share"] = sum(aw[k] * g[k] for k in aw)
        g["final_share"] = _cap_and_renormalise(g["blended_share"], g["historical_share"],
                                                tcfg["max_change_vs_prior"]).to_numpy()
        parts.append(g)
    terr = pd.concat(parts, ignore_index=True)

    # 3. territory-month targets ---------------------------------------------------
    tm = terr.merge(market_plan, on="market_id")
    tm["territory_target"] = tm["market_target"] * tm["final_share"]

    # 4. rep-month split by ramp-adjusted capacity --------------------------------
    rep_month = reps.merge(pd.DataFrame({"month": plan_months}), how="cross")
    rep_month = rep_month[rep_month["hire_date"] <= rep_month["month"] + pd.offsets.MonthEnd(0)]
    rep_month["ramp"] = _ramp_factor(((rep_month["month"] - rep_month["hire_date"]).dt.days / 30.44).to_numpy(),
                                     tcfg["rep_ramp"])
    rep_month["cap_share"] = rep_month["ramp"] / rep_month.groupby(["territory_id", "month"])["ramp"].transform("sum")
    rt = rep_month.merge(tm[["territory_id", "market_id", "month", "territory_target"]], on=["territory_id", "month"])
    rt["target_signings"] = rt["territory_target"] * rt["cap_share"]

    # 5. consistent new-merchant volume target ------------------------------------
    ref_year = as_of.year - 1 if as_of.month < 12 else as_of.year
    curve = con.execute(f"""
        SELECT market_id, month, SUM(signings) AS s, SUM(new_merchant_volume_usd) AS v
        FROM marts.rep_monthly WHERE EXTRACT(YEAR FROM month) = {ref_year}
        GROUP BY 1, 2 ORDER BY 1, 2
    """).df()
    curve["cum_s"] = curve.groupby("market_id")["s"].cumsum()
    curve["vol_per_ytd_signing"] = curve["v"] / curve["cum_s"].replace(0, np.nan)
    curve["moy"] = pd.to_datetime(curve["month"]).dt.month
    rt["moy"] = rt["month"].dt.month
    rt = rt.sort_values(["rep_id", "month"])
    rt["cum_target"] = rt.groupby("rep_id")["target_signings"].cumsum()
    rt = rt.merge(curve[["market_id", "moy", "vol_per_ytd_signing"]], on=["market_id", "moy"], how="left")
    rt["target_new_volume_usd"] = rt["cum_target"] * rt["vol_per_ytd_signing"].fillna(0)

    out = rt[["month", "rep_id", "territory_id", "market_id", "ramp", "target_signings", "target_new_volume_usd"]].copy()
    out.insert(0, "plan_year", plan_year)
    out["month"] = out["month"].dt.date
    terr_plan = terr.assign(plan_year=plan_year, as_of_month=as_of.date(),
                            annual_target=lambda d: d["market_id"].map(market_plan.groupby("market_id")["market_target"].sum()) * d["final_share"])
    terr_plan["change_vs_prior_pct"] = terr_plan["annual_target"] / terr_plan["prior_12m_signings"].replace(0, np.nan) - 1

    con.execute("CREATE SCHEMA IF NOT EXISTS targets")
    for name, df in (("rep_monthly", out), ("territory_plan", terr_plan)):
        exists = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='targets' AND table_name=?", [name]
        ).fetchone()[0]
        if exists:
            con.execute(f"DELETE FROM targets.{name} WHERE plan_year = ?", [plan_year])
            con.execute(f"INSERT INTO targets.{name} BY NAME SELECT * FROM df")
        else:
            con.execute(f"CREATE TABLE targets.{name} AS SELECT * FROM df")
    return terr_plan
