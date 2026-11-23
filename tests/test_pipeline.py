import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fph import generate, scorecard, targets
from fph.config import DATA_DIR, settings


def test_pipeline_succeeds(manifest):
    assert manifest["status"] == "SUCCESS"
    assert manifest["dq"]["critical_failures"] == []
    assert Path(manifest["outputs"]["excel"]).exists()


def test_generator_is_deterministic():
    a, b = generate.generate(seed=7), generate.generate(seed=7)
    pd.testing.assert_frame_equal(a.merchants.drop(columns="_loaded_at"), b.merchants.drop(columns="_loaded_at"))


# --- data quality: every injected defect is caught, exactly ---------------------
@pytest.mark.parametrize("issue,key", [
    ("DUPLICATE_RECORD", "merchants_duplicate_rows"),
    ("MISSING_TERRITORY", "merchants_null_territory"),
    ("ORPHAN_REP", "merchants_orphan_rep"),
    ("FUTURE_SIGNED_DATE", "merchants_future_signed_date"),
])
def test_merchant_defects_caught_exactly(con, issue, key):
    injected = json.loads((DATA_DIR / "raw" / "_injected_defects.json").read_text())
    n = con.execute("SELECT COUNT(*) FROM dq.quarantine_merchants WHERE dq_issue = ?", [issue]).fetchone()[0]
    assert n == injected[key]


@pytest.mark.parametrize("issue,key", [
    ("NEGATIVE_AMOUNT", "volume_negative_amount"),
    ("DUPLICATE_RECORD", "volume_duplicate_rows"),
    ("ORPHAN_MERCHANT", "volume_orphan_merchant"),
])
def test_volume_defects_caught_exactly(con, issue, key):
    injected = json.loads((DATA_DIR / "raw" / "_injected_defects.json").read_text())
    n = con.execute("SELECT COUNT(*) FROM dq.quarantine_volume WHERE dq_issue = ?", [issue]).fetchone()[0]
    assert n == injected[key]


def test_no_defects_leak_into_marts(con):
    assert con.execute("SELECT COUNT(*) FROM staging.merchant_volume WHERE charge_volume_usd < 0").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) - COUNT(DISTINCT merchant_id) FROM marts.fct_signings").fetchone()[0] == 0


def test_reconciliation_checks_all_pass(con):
    failed = con.execute("SELECT check_id FROM dq.results WHERE category = 'Reconciliation' AND NOT passed").fetchall()
    assert failed == []


# --- forecasting ------------------------------------------------------------
def test_one_model_selected_per_market_and_accuracy_reasonable(con):
    sel = con.execute("SELECT market_id, COUNT(*) FILTER (WHERE selected) n, MIN(mean_wape) w "
                      "FROM forecast.model_selection GROUP BY 1").df()
    assert (sel["n"] == 1).all()
    assert sel["w"].max() < 0.25


def test_forecast_intervals_bracket_point(con):
    bad = con.execute("SELECT COUNT(*) FROM forecast.signings WHERE NOT (lower <= forecast AND forecast <= upper)").fetchone()[0]
    assert bad == 0


# --- targets ----------------------------------------------------------------
def test_territory_shares_sum_to_one_per_market(con):
    s = con.execute("SELECT plan_year, market_id, SUM(final_share) s FROM targets.territory_plan GROUP BY 1, 2").df()
    assert np.allclose(s["s"], 1.0)


def test_rep_targets_roll_up_to_market_plan(con):
    d = con.execute("""
        SELECT p.plan_year, p.market_id, p.plan, r.reps FROM
          (SELECT plan_year, market_id, SUM(annual_target) plan FROM targets.territory_plan GROUP BY 1,2) p
        JOIN (SELECT plan_year, market_id, SUM(target_signings) reps FROM targets.rep_monthly GROUP BY 1,2) r
        USING (plan_year, market_id)""").df()
    assert np.allclose(d["plan"], d["reps"], rtol=1e-6)


def test_share_cap_respected(con):
    cap = settings()["targets"]["max_change_vs_prior"]
    d = con.execute("SELECT final_share, historical_share FROM targets.territory_plan").df()
    ratio = d["final_share"] / d["historical_share"]
    assert ratio.between(1 - cap - 1e-6, 1 + cap + 1e-6).all()


def test_ramp_factor_bands():
    cfg = settings()["targets"]["rep_ramp"]
    f = targets._ramp_factor(np.array([-1, 0, 5.9, 6, 11.9, 12, 60]), cfg)
    assert f.tolist() == [0, 0.5, 0.5, 0.75, 0.75, 1.0, 1.0]


# --- scorecard --------------------------------------------------------------
def test_score_is_weighted_capped_attainment():
    df = pd.DataFrame({"signings": [30], "target_signings": [10], "mid_large_signings": [0], "new_volume": [0],
                       "target_new_volume": [100], "activated_90d": [0], "activation_eligible": [10]})
    out = scorecard.score(df)
    cap = settings()["scorecard"]["attainment_cap"]
    w = settings()["scorecard"]["kpis"]["signings"]["weight"]
    assert out["att_signings"].iloc[0] == cap
    assert out["score"].iloc[0] == pytest.approx(w * cap)
    assert out["tier"].iloc[0] == "Needs Attention"


def test_every_rep_has_tier_and_rank(con):
    d = con.execute("SELECT tier, rank_in_market FROM scorecard.rep").df()
    assert d["tier"].notna().all() and (d["rank_in_market"] >= 1).all()
