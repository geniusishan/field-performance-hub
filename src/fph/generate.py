"""Synthetic merchant-acquisition data for a global card-network sales field.

Models a realistic field organisation: markets -> territories -> sales reps ->
merchant signings -> monthly charge volume. Rep skill, tenure ramp, seasonality,
segment mix and activation lag all vary, so scorecards and forecasts have real
signal to find. Optionally injects the defects real source systems produce
(duplicates, orphans, negative volumes, future dates) so the data-quality layer
has something genuine to catch.

No real company data is used; everything here is generated.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from fph.config import DATA_DIR, settings

MARKETS = [
    # id, name, region, currency, size multiplier, yoy growth
    ("IN", "India", "JAPA", "INR", 1.25, 0.14),
    ("JP", "Japan", "JAPA", "JPY", 1.10, 0.05),
    ("AU", "Australia", "JAPA", "AUD", 0.90, 0.06),
    ("UK", "United Kingdom", "EMEA", "GBP", 1.00, 0.04),
    ("DE", "Germany", "EMEA", "EUR", 0.85, 0.07),
    ("US", "United States", "NA", "USD", 1.40, 0.03),
    ("CA", "Canada", "NA", "CAD", 0.80, 0.05),
    ("MX", "Mexico", "LACC", "MXN", 0.95, 0.11),
]
TERRITORIES_PER_MARKET = 5
INDUSTRIES = ["Restaurants", "Retail", "Travel", "Healthcare", "Education",
              "Professional Services", "Online & Digital", "Fuel & Auto"]
SEGMENTS = np.array(["Small", "Mid", "Large"])
SEGMENT_P = np.array([0.72, 0.22, 0.06])
SEGMENT_VOL = {"Small": 60_000, "Mid": 450_000, "Large": 3_500_000}  # median annual USD
FIRST = ["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Ananya", "Vikram", "Isha", "Arjun",
         "Saanvi", "Hiro", "Yuki", "Emma", "Oliver", "Lena", "Noah", "Sofia", "Mateo",
         "Liam", "Ava", "Chloe", "Lucas", "Mia", "Ethan", "Zara", "Kenji", "Priya", "Omar"]
LAST = ["Sharma", "Iyer", "Kapoor", "Reddy", "Nair", "Mehta", "Tanaka", "Sato", "Smith",
        "Brown", "Muller", "Schmidt", "Garcia", "Lopez", "Wilson", "Taylor", "Martin",
        "Khan", "Singh", "Gupta", "Ito", "Davies", "Fischer", "Hernandez", "Clarke"]
# monthly seasonality index (Jan..Dec): Q4 push, Dec holiday dip, Q1 restart
SEASONALITY = np.array([0.85, 0.92, 1.08, 0.98, 1.00, 1.04, 0.95, 0.97, 1.06, 1.10, 1.12, 0.93])


@dataclass
class Dataset:
    markets: pd.DataFrame
    territories: pd.DataFrame
    reps: pd.DataFrame
    merchants: pd.DataFrame
    merchant_volume: pd.DataFrame
    injected: dict


def generate(seed: int | None = None, inject_issues: bool | None = None) -> Dataset:
    cfg = settings()["data"]
    seed = cfg["seed"] if seed is None else seed
    inject_issues = cfg["inject_dq_issues"] if inject_issues is None else inject_issues
    rng = np.random.default_rng(seed)
    months = pd.date_range(cfg["start_month"], cfg["end_month"], freq="MS")
    end_month = months[-1]

    markets = pd.DataFrame(MARKETS, columns=["market_id", "market_name", "region",
                                             "currency", "size_mult", "yoy_growth"])

    # --- territories --------------------------------------------------------
    terr_rows = []
    for m in markets.itertuples():
        for t in range(1, TERRITORIES_PER_MARKET + 1):
            terr_rows.append({
                "territory_id": f"{m.market_id}-T{t:02d}",
                "market_id": m.market_id,
                "territory_name": f"{m.market_name} Territory {t}",
                "merchant_universe": int(rng.integers(6_000, 16_000) * m.size_mult),
                "potential_mult": float(rng.uniform(0.75, 1.3)),
            })
    territories = pd.DataFrame(terr_rows)

    # --- reps ---------------------------------------------------------------
    rep_rows, rid = [], 1
    for t in territories.itertuples():
        for _ in range(int(rng.integers(4, 9))):
            hire = pd.Timestamp("2017-01-01") + pd.Timedelta(days=int(rng.integers(0, 3500)))
            rep_rows.append({
                "rep_id": f"R{rid:04d}",
                "rep_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                "territory_id": t.territory_id,
                "role": rng.choice(["Acquisition", "Account Development"], p=[0.75, 0.25]),
                "hire_date": hire.normalize(),
                "skill": float(np.clip(rng.lognormal(0, 0.2), 0.55, 1.8)),
            })
            rid += 1
    reps = pd.DataFrame(rep_rows)
    reps["manager_id"] = reps.groupby("territory_id")["rep_id"].transform("first")

    # --- attrition and backfills: real field teams lose people and replace them --
    # Exits are drawn inside the reporting window (~12% a year); each leaver is backfilled
    # 1-4 months later by a new hire who then has to ramp up. This is what makes
    # "why are we behind target?" a real question: vacancies and ramping reps cost signings.
    window_start, window_end = months[0], months[-1] + pd.offsets.MonthEnd(0)
    base = reps["hire_date"].where(reps["hire_date"] > window_start, window_start)
    exit_after = rng.exponential(scale=8 * 365, size=len(reps)).astype(int)
    term = base + pd.to_timedelta(exit_after, unit="D")
    reps["termination_date"] = term.where(term <= window_end)
    backfills = []
    for r in reps[reps["termination_date"].notna()].itertuples():
        hire = (r.termination_date + pd.Timedelta(days=int(rng.integers(30, 120)))).normalize()
        if hire > window_end:
            continue  # vacancy still open at the end of the window
        backfills.append({
            "rep_id": f"R{rid:04d}", "rep_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "territory_id": r.territory_id, "role": r.role, "hire_date": hire,
            "skill": float(np.clip(rng.lognormal(0, 0.2), 0.55, 1.8)), "manager_id": r.manager_id,
            "termination_date": pd.NaT,
        })
        rid += 1
    if backfills:
        reps = pd.concat([reps, pd.DataFrame(backfills)], ignore_index=True)

    # --- signings (one row = one merchant signed) -----------------------------
    rt = reps.merge(territories, on="territory_id").merge(markets, on="market_id")
    grid = rt.assign(key=1).merge(pd.DataFrame({"month": months, "key": 1}), on="key").drop(columns="key")
    grid = grid[(grid["hire_date"] <= grid["month"] + pd.offsets.MonthEnd(0))
                & (grid["termination_date"].isna() | (grid["termination_date"] >= grid["month"]))]
    tenure = ((grid["month"] - grid["hire_date"]).dt.days / 30.44).clip(lower=0)
    ramp = np.where(tenure < 6, 0.45, np.where(tenure < 12, 0.75, 1.0))
    years = (grid["month"] - months[0]).dt.days / 365.25
    lam = (4.2 * grid["size_mult"] * grid["potential_mult"] * grid["skill"] * ramp
           * SEASONALITY[grid["month"].dt.month - 1] * (1 + grid["yoy_growth"]) ** years)
    # Account Development reps sign fewer brand-new merchants
    lam = lam * np.where(grid["role"] == "Account Development", 0.45, 1.0)
    counts = rng.poisson(lam.to_numpy())
    sig = grid.loc[grid.index.repeat(counts)].reset_index(drop=True)
    n = len(sig)
    day_offset = rng.integers(0, 28, n)
    sig["signed_date"] = sig["month"] + pd.to_timedelta(day_offset, unit="D")
    seg = rng.choice(SEGMENTS, size=n, p=SEGMENT_P)
    med = np.vectorize(SEGMENT_VOL.get)(seg)
    merchants = pd.DataFrame({
        "merchant_id": [f"M{i:07d}" for i in range(1, n + 1)],
        "merchant_name": [f"Merchant {i:07d}" for i in range(1, n + 1)],
        "territory_id": sig["territory_id"].to_numpy(),
        "signed_by_rep_id": sig["rep_id"].to_numpy(),
        "signed_date": sig["signed_date"].dt.date.to_numpy(),
        "industry": rng.choice(INDUSTRIES, size=n),
        "segment": seg,
        "expected_annual_volume_usd": np.round(med * rng.lognormal(0, 0.55, n), 2),
        "source_system": rng.choice(["CRM", "Partner Portal", "Digital Self-Serve"], size=n,
                                     p=[0.7, 0.18, 0.12]),
    })

    # --- monthly charge volume ----------------------------------------------
    # activation lag depends on rep skill: better reps get merchants transacting sooner
    skill = sig["skill"].to_numpy()
    p_fast = np.clip(0.80 + 0.22 * (skill - 1), 0.45, 0.97)
    lag = np.where(rng.random(n) < p_fast, rng.integers(0, 3, n), rng.integers(3, 9, n))
    never = rng.random(n) < 0.06
    signed_m = sig["month"].to_numpy().astype("datetime64[M]")
    act_m = signed_m + lag.astype("timedelta64[M]")
    end_m = np.datetime64(end_month, "M")
    churn_after = rng.geometric(1 / 40, n)  # months active before churn
    last_m = np.minimum(act_m + churn_after.astype("timedelta64[M]"), end_m)
    n_months = ((last_m - act_m).astype(int) + 1)
    n_months = np.where(never | (act_m > end_m), 0, np.clip(n_months, 0, None))
    idx = np.repeat(np.arange(n), n_months)
    offs = np.concatenate([np.arange(k) for k in n_months]) if n_months.sum() else np.array([], int)
    vol_month = act_m[idx] + offs.astype("timedelta64[M]")
    base = merchants["expected_annual_volume_usd"].to_numpy()[idx] / 12
    ramp_up = np.minimum(1.0, 0.35 + 0.22 * offs)
    seas = SEASONALITY[vol_month.astype("datetime64[M]").astype(int) % 12]
    volume = np.round(base * ramp_up * seas * rng.lognormal(0, 0.25, len(idx)), 2)
    merchant_volume = pd.DataFrame({
        "merchant_id": merchants["merchant_id"].to_numpy()[idx],
        "month": pd.to_datetime(vol_month).date,
        "charge_volume_usd": volume,
        "transactions": np.maximum(1, (volume / rng.uniform(35, 140, len(idx))).astype(int)),
    })

    reps_out = reps.drop(columns=["skill"]).assign(
        hire_date=lambda d: d["hire_date"].dt.date,
        termination_date=lambda d: d["termination_date"].dt.date.astype(object).where(d["termination_date"].notna(), None))
    territories_out = territories.drop(columns=["potential_mult"])
    markets_out = markets.drop(columns=["size_mult", "yoy_growth"])

    injected: dict = {}
    if inject_issues:
        merchants, merchant_volume, injected = _inject_defects(merchants, merchant_volume, rng, end_month)

    load_ts = pd.Timestamp.now().floor("s")
    for df in (markets_out, territories_out, reps_out, merchants, merchant_volume):
        df["_loaded_at"] = load_ts
    return Dataset(markets_out, territories_out, reps_out, merchants, merchant_volume, injected)


def _inject_defects(merchants: pd.DataFrame, vol: pd.DataFrame, rng, end_month):
    """Introduce realistic source-system defects and record exactly what was injected."""
    m = merchants.copy()
    v = vol.copy()
    n, nv = len(m), len(v)

    dup_idx = rng.choice(n, size=int(n * 0.004), replace=False)
    null_terr = rng.choice(n, size=int(n * 0.0015), replace=False)
    orphan_rep = rng.choice(n, size=int(n * 0.002), replace=False)
    future = rng.choice(n, size=max(5, int(n * 0.0004)), replace=False)
    # keep defect sets disjoint so ground truth is unambiguous
    null_terr = np.setdiff1d(null_terr, dup_idx)
    orphan_rep = np.setdiff1d(orphan_rep, np.concatenate([dup_idx, null_terr]))
    future = np.setdiff1d(future, np.concatenate([dup_idx, null_terr, orphan_rep]))

    m.loc[null_terr, "territory_id"] = None
    m.loc[orphan_rep, "signed_by_rep_id"] = [f"R9{rng.integers(100, 999)}" for _ in orphan_rep]
    m.loc[future, "signed_date"] = (end_month + pd.Timedelta(days=75)).date()
    m = pd.concat([m, m.iloc[dup_idx]], ignore_index=True)

    neg = rng.choice(nv, size=int(nv * 0.001), replace=False)
    v.loc[neg, "charge_volume_usd"] = -v.loc[neg, "charge_volume_usd"].abs()
    vdup = np.setdiff1d(rng.choice(nv, size=int(nv * 0.002), replace=False), neg)
    orphan_m = pd.DataFrame({
        "merchant_id": [f"M9{rng.integers(100000, 999999)}" for _ in range(int(nv * 0.0008))],
        "month": rng.choice(v["month"].unique(), size=int(nv * 0.0008)),
        "charge_volume_usd": np.round(rng.uniform(100, 5000, int(nv * 0.0008)), 2),
        "transactions": rng.integers(1, 50, int(nv * 0.0008)),
    })
    v = pd.concat([v, v.iloc[vdup], orphan_m], ignore_index=True)

    injected = {
        "merchants_duplicate_rows": int(len(dup_idx)),
        "merchants_null_territory": int(len(null_terr)),
        "merchants_orphan_rep": int(len(orphan_rep)),
        "merchants_future_signed_date": int(len(future)),
        "volume_negative_amount": int(len(neg)),
        "volume_duplicate_rows": int(len(vdup)),
        "volume_orphan_merchant": int(len(orphan_m)),
    }
    return m, v, injected


def write_raw(ds: Dataset) -> None:
    raw = DATA_DIR / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for name in ("markets", "territories", "reps", "merchants", "merchant_volume"):
        getattr(ds, name).to_parquet(raw / f"{name}.parquet", index=False)
    (raw / "_injected_defects.json").write_text(json.dumps(ds.injected, indent=2))


if __name__ == "__main__":
    d = generate()
    write_raw(d)
    print({k: len(getattr(d, k)) for k in ("markets", "territories", "reps", "merchants", "merchant_volume")})
    print(d.injected)
