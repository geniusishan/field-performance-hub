"""Why are we behind (or ahead of) target? A three-driver bridge for signings.

Signings can be written as

    signings = rep-months  x  avg capacity per rep-month  x  signings per unit of capacity
             =     H       x            R                 x              P

where capacity is each rep's ramp factor (new hires count 0.5, rising to 1.0 after a
year) times a role weight (Account Development reps carry a smaller new-signings share).
Comparing an actual period (a) with a comparison (c) - the plan, the same period last
year, or the previous period - the gap splits exactly into:

    headcount     = (H_a - H_c) x R_c x P_c     more or fewer reps than the comparison
    ramp & mix    =  H_a x (R_a - R_c) x P_c    more new hires still ramping, or role mix
    productivity  =  H_a x R_a x (P_a - P_c)    what each productive rep actually signed

headcount + ramp & mix + productivity = actual - comparison, to the unit. For the plan,
H and R come from targets.territory_monthly: the team employed when the plan was set,
ramping forward, plus planned net hiring at the pace of the previous 12 months.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import duckdb
import numpy as np
import pandas as pd

from fph.config import settings
from fph.targets import _ramp_factor

COMPARISONS = {
    "target": "Target",
    "last_year": "Same period last year",
    "previous": "Previous period",
}
DRIVER_LABELS = {
    "headcount": "Headcount",
    "ramp": "Ramp & role mix",
    "productivity": "Productivity",
}


@dataclass
class Bridge:
    actual: float
    comparison: float
    headcount: float
    ramp: float
    productivity: float
    actual_rep_months: float
    comparison_rep_months: float
    actual_capacity: float
    comparison_capacity: float
    months: int

    @property
    def gap(self) -> float:
        return self.actual - self.comparison

    @property
    def gap_pct(self) -> float:
        return self.gap / self.comparison if self.comparison else float("nan")

    @property
    def main_driver(self) -> str:
        parts = {"headcount": self.headcount, "ramp": self.ramp, "productivity": self.productivity}
        # the driver that moved the result most in the direction of the gap
        sign = np.sign(self.gap) or -1
        key = max(parts, key=lambda k: parts[k] * sign)
        return DRIVER_LABELS[key]

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(gap=self.gap, gap_pct=self.gap_pct, main_driver=self.main_driver)
        return d


# --- data frames -------------------------------------------------------------------
def actual_frame(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Rep-month actuals with capacity (ramp x role weight)."""
    df = con.execute("""
        SELECT month, rep_id, rep_name, role, territory_id, territory_name, market_id, market_name, region,
               tenure_months, signings, mid_large_signings, new_merchant_volume_usd,
               activated_90d, activation_eligible
        FROM marts.rep_monthly
    """).df()
    df["month"] = pd.to_datetime(df["month"])
    tcfg = settings()["targets"]
    df["capacity"] = (_ramp_factor(df["tenure_months"].astype(float).to_numpy(), tcfg["rep_ramp"])
                      * df["role"].map(tcfg["role_weight"]).fillna(1.0).to_numpy())
    return df


def plan_frame(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Territory-month plan: signings target, planned rep-months and capacity, volume target."""
    df = con.execute("""
        SELECT t.plan_year, t.month, t.territory_id, t.market_id, t.territory_target AS target_signings,
               t.planned_rep_months, t.planned_capacity, COALESCE(v.target_new_volume_usd, 0) AS target_new_volume_usd
        FROM targets.territory_monthly t
        LEFT JOIN (SELECT plan_year, month, territory_id, SUM(target_new_volume_usd) AS target_new_volume_usd
                   FROM targets.rep_monthly GROUP BY 1, 2, 3) v USING (plan_year, month, territory_id)
    """).df()
    df["month"] = pd.to_datetime(df["month"])
    names = con.execute("SELECT DISTINCT territory_id, territory_name, market_name, region FROM marts.dim_rep").df()
    return df.merge(names, on="territory_id", how="left")


# --- periods ---------------------------------------------------------------------------
def comparison_months(start: pd.Timestamp, end: pd.Timestamp, mode: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if mode == "last_year":
        return start - pd.DateOffset(years=1), end - pd.DateOffset(years=1)
    if mode == "previous":
        n = (end.to_period("M") - start.to_period("M")).n + 1
        return start - pd.DateOffset(months=n), start - pd.DateOffset(months=1)
    return start, end


def target_available(plan: pd.DataFrame, start, end) -> bool:
    months = pd.date_range(start, end, freq="MS")
    return set(months).issubset(set(plan["month"]))


# --- the bridge ----------------------------------------------------------------------------
def _bridge(a_sign: float, a_h: float, a_c: float, c_sign: float, c_h: float, c_c: float, months: int) -> Bridge:
    r_a = a_c / a_h if a_h else 0.0
    r_c = c_c / c_h if c_h else 0.0
    p_a = a_sign / a_c if a_c else 0.0
    p_c = c_sign / c_c if c_c else 0.0
    headcount = (a_h - c_h) * r_c * p_c
    ramp = a_h * (r_a - r_c) * p_c
    productivity = a_h * r_a * (p_a - p_c)
    # guard against degenerate inputs (e.g. no plan capacity): book any residual to productivity
    residual = (a_sign - c_sign) - (headcount + ramp + productivity)
    return Bridge(a_sign, c_sign, headcount, ramp, productivity + residual, a_h, c_h, a_c, c_c, months)


def compute(actual: pd.DataFrame, plan: pd.DataFrame, start, end, mode: str,
            markets: list[str] | None = None, territories: list[str] | None = None) -> Bridge:
    """Bridge for any scope (all / markets / territories) and month range."""
    def scope(df: pd.DataFrame) -> pd.DataFrame:
        if markets:
            df = df[df["market_name"].isin(markets)]
        if territories:
            df = df[df["territory_name"].isin(territories)]
        return df

    start, end = pd.Timestamp(start), pd.Timestamp(end)
    months = (end.to_period("M") - start.to_period("M")).n + 1
    a = scope(actual[(actual["month"] >= start) & (actual["month"] <= end)])
    if mode == "target":
        p = scope(plan[(plan["month"] >= start) & (plan["month"] <= end)])
        return _bridge(a["signings"].sum(), len(a), a["capacity"].sum(),
                       p["target_signings"].sum(), p["planned_rep_months"].sum(), p["planned_capacity"].sum(), months)
    cs, ce = comparison_months(start, end, mode)
    c = scope(actual[(actual["month"] >= cs) & (actual["month"] <= ce)])
    return _bridge(a["signings"].sum(), len(a), a["capacity"].sum(),
                   c["signings"].sum(), len(c), c["capacity"].sum(), months)


def by_group(actual: pd.DataFrame, plan: pd.DataFrame, start, end, mode: str, level: str,
             markets: list[str] | None = None) -> pd.DataFrame:
    """One bridge per market or territory, as a table."""
    col = "market_name" if level == "market" else "territory_name"
    pool = actual if not markets else actual[actual["market_name"].isin(markets)]
    rows = []
    for name in sorted(pool[col].dropna().unique()):
        kw = {"markets": [name]} if level == "market" else {"territories": [name]}
        b = compute(actual, plan, start, end, mode, **kw)
        row = {col: name, **b.as_dict()}
        if level == "territory":
            row["market_name"] = pool.loc[pool[col] == name, "market_name"].iloc[0]
        rows.append(row)
    return pd.DataFrame(rows)


# --- plain-English explanation ---------------------------------------------------------------
def _n(x: float) -> str:
    return f"{x:+,.0f}"


def explain(b: Bridge, comparison_label: str) -> list[str]:
    """Deterministic sentences: every number comes straight from the bridge."""
    if not b.comparison:
        return [f"Signings were {b.actual:,.0f}; there is no {comparison_label.lower()} to compare with."]
    direction = "ahead" if b.gap >= 0 else "behind"
    lines = [f"Signings were {b.actual:,.0f} vs {b.comparison:,.0f} ({comparison_label.lower()}): "
             f"{abs(b.gap):,.0f} ({abs(b.gap_pct):.1%}) {direction}."]
    avg_a = b.actual_rep_months / b.months if b.months else 0
    avg_c = b.comparison_rep_months / b.months if b.months else 0
    parts = [
        ("headcount", b.headcount, f"average team of {avg_a:,.1f} reps vs {avg_c:,.1f}"),
        ("ramp", b.ramp, "share of capacity lost to reps still ramping up, and role mix"),
        ("productivity", b.productivity, "signings per fully productive rep"),
    ]
    parts.sort(key=lambda p: -abs(p[1]))
    for key, val, why in parts:
        lines.append(f"{DRIVER_LABELS[key]}: {_n(val)} signings ({why}).")
    lines.append(f"Main driver: {b.main_driver}.")
    return lines


# --- published table for Excel, the dashboard and the AI agent ---------------------------------
def publish(con: duckdb.DuckDBPyConnection, plan_year: int, as_of_month: str) -> pd.DataFrame:
    actual, plan = actual_frame(con), plan_frame(con)
    start, end = pd.Timestamp(f"{plan_year}-01-01"), pd.Timestamp(as_of_month)
    m = by_group(actual, plan, start, end, "target", "market")
    m.insert(0, "level", "market")
    t = by_group(actual, plan, start, end, "target", "territory")
    t.insert(0, "level", "territory")
    out = pd.concat([m, t], ignore_index=True)
    out["avg_reps_actual"] = out["actual_rep_months"] / out["months"]
    out["avg_reps_plan"] = out["comparison_rep_months"] / out["months"]
    out = out.rename(columns={"comparison": "target", "headcount": "headcount_effect",
                              "ramp": "ramp_effect", "productivity": "productivity_effect"})
    out = out[["level", "market_name", "territory_name", "actual", "target", "gap", "gap_pct",
               "headcount_effect", "ramp_effect", "productivity_effect", "main_driver",
               "avg_reps_actual", "avg_reps_plan"]]
    out.insert(0, "plan_year", plan_year)
    out.insert(1, "as_of_month", pd.Timestamp(as_of_month).date())
    con.execute("CREATE SCHEMA IF NOT EXISTS insights")
    con.execute("CREATE OR REPLACE TABLE insights.gap_drivers AS SELECT * FROM out")
    return out
