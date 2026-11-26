import numpy as np
import pandas as pd
import pytest
from pathlib import Path
from streamlit.testing.v1 import AppTest

from fph import drivers


@pytest.fixture(scope="module")
def frames(con):
    return drivers.actual_frame(con), drivers.plan_frame(con)


@pytest.mark.parametrize("mode", ["target", "last_year", "previous"])
@pytest.mark.parametrize("scope", [{}, {"markets": ["India"]}, {"territories": ["Japan Territory 2"]}])
def test_bridge_adds_up_exactly(frames, mode, scope):
    a, p = frames
    b = drivers.compute(a, p, "2026-01-01", "2026-10-01", mode, **scope)
    assert b.headcount + b.ramp + b.productivity == pytest.approx(b.gap, abs=1e-6)


def test_target_bridge_matches_published_scorecard(con, frames):
    a, p = frames
    b = drivers.compute(a, p, "2026-01-01", "2026-10-01", "target")
    sc = con.execute("SELECT SUM(signings), SUM(target_signings) FROM scorecard.market").fetchone()
    assert b.actual == sc[0]
    assert b.comparison == pytest.approx(sc[1], rel=1e-9)


def test_comparison_periods():
    s, e = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-10-01")
    assert drivers.comparison_months(s, e, "last_year") == (pd.Timestamp("2025-01-01"), pd.Timestamp("2025-10-01"))
    assert drivers.comparison_months(s, e, "previous") == (pd.Timestamp("2025-03-01"), pd.Timestamp("2025-12-01"))


def test_explanation_numbers_come_from_bridge():
    b = drivers._bridge(900, 100, 80, 1000, 100, 90, 10)
    text = " ".join(drivers.explain(b, "Target"))
    assert "900 vs 1,000" in text and "100 (10.0%) behind" in text
    assert b.main_driver in text


def test_published_gap_drivers_table(con):
    d = con.execute("SELECT * FROM insights.gap_drivers").df()
    assert set(d["level"]) == {"market", "territory"}
    total = d.loc[d["level"] == "market", ["headcount_effect", "ramp_effect", "productivity_effect"]].to_numpy().sum()
    assert total == pytest.approx(d.loc[d["level"] == "market", "gap"].sum(), abs=1e-6)


def test_attrition_and_backfills_present(con):
    left = con.execute("SELECT COUNT(*) FROM marts.dim_rep WHERE termination_date IS NOT NULL").fetchone()[0]
    assert left > 0
    # no rep has activity after leaving
    after = con.execute("""SELECT COUNT(*) FROM marts.rep_monthly r JOIN marts.dim_rep d USING (rep_id)
                           WHERE d.termination_date IS NOT NULL AND r.month > d.termination_date""").fetchone()[0]
    assert after == 0


def test_role_weighting_keeps_targets_fair(con):
    d = con.execute("SELECT role, SUM(signings) / SUM(target_signings) AS att FROM scorecard.rep GROUP BY role").df()
    assert d["att"].max() - d["att"].min() < 0.15   # roles measured on comparable footing


APP = str(Path(__file__).resolve().parents[1] / "app" / "dashboard.py")


def test_performance_tab_all_modes_and_fallbacks(manifest):
    at = AppTest.from_file(APP, default_timeout=120).run()
    assert not at.exception
    assert at.metric[0].label == "Signings" and "vs target" in at.metric[0].delta
    for label in ["Same period last year", "Previous period", "Target"]:
        at.sidebar.radio[0].set_value(label).run()
        assert not at.exception, label
    at.sidebar.multiselect[1].set_value(["Japan"]).run()          # single market -> territory view
    assert not at.exception
    at.sidebar.select_slider[0].set_value((pd.Timestamp("2025-04-01"), pd.Timestamp("2026-03-01"))).run()
    assert not at.exception and any("Targets exist only" in i.value for i in at.info)
