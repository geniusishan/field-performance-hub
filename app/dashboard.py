"""Field Performance Hub - interactive dashboard.

    streamlit run app/dashboard.py

Reads only from the published warehouse tables (read-only connection).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fph import warehouse  # noqa: E402
from fph.config import EXPORT_DIR  # noqa: E402

st.set_page_config(page_title="Field Performance Hub", page_icon="📊", layout="wide")
TIER_COLORS = {"Exceeds": "#2E7D32", "Meets": "#81C784", "Below": "#FFB300", "Needs Attention": "#E53935"}


@st.cache_data(ttl=600)
def q(sql: str) -> pd.DataFrame:
    con = warehouse.connect(read_only=True)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


@st.cache_resource(show_spinner="First launch: building the warehouse (about 10 seconds)…")
def ensure_warehouse() -> bool:
    """On a fresh deploy (e.g. Streamlit Community Cloud) build the data on first load."""
    from fph import pipeline
    from fph.config import warehouse_path
    if not warehouse_path().exists():
        pipeline.run(verbose=False)
    return True


try:
    ensure_warehouse()
    as_of = pd.Timestamp(q("SELECT as_of_month FROM staging.run_params").iloc[0, 0])
except Exception as e:  # noqa: BLE001
    st.error(f"Warehouse unavailable: {e}. Run `python -m fph.pipeline run`.")
    st.stop()

plan_year = as_of.year
st.title("📊 Field Performance Hub")
st.caption(f"{plan_year} YTD through {as_of:%B %Y} · synthetic data · "
           f"run {q('SELECT run_id FROM ops.run_log ORDER BY logged_at DESC LIMIT 1').iloc[0, 0]}")

markets = q("SELECT DISTINCT market_name FROM scorecard.market ORDER BY 1")["market_name"].tolist()
sel = st.sidebar.multiselect("Markets", markets, default=markets)
mfilter = "(" + ",".join(f"'{m}'" for m in sel) + ")" if sel else "('')"

tab_overview, tab_score, tab_fc, tab_targets, tab_dq = st.tabs(
    ["Overview", "Scorecards", "Forecast", f"{plan_year + 1} Targets", "Data Quality"])

with tab_overview:
    m = q(f"SELECT * FROM scorecard.market WHERE market_name IN {mfilter} ORDER BY score DESC")
    c = st.columns(4)
    c[0].metric("YTD signings", f"{m['signings'].sum():,.0f}", f"{m['signings'].sum() / m['target_signings'].sum() - 1:+.1%} vs target")
    c[1].metric("New-merchant volume", f"${m['new_volume'].sum() / 1e6:,.1f}M",
                f"{m['new_volume'].sum() / m['target_new_volume'].sum() - 1:+.1%} vs target")
    c[2].metric("90-day activation", f"{m['activated_90d'].sum() / m['activation_eligible'].sum():.1%}")
    c[3].metric("Mid/Large mix", f"{m['mid_large_signings'].sum() / m['signings'].sum():.1%}")
    left, right = st.columns([3, 2])
    fig = px.bar(m, x="market_name", y="score", color="tier", color_discrete_map=TIER_COLORS,
                 text=m["score"].map("{:.0%}".format), title="Weighted scorecard by market")
    fig.add_hline(y=1.0, line_dash="dash", line_color="grey")
    fig.update_layout(yaxis_tickformat=".0%", xaxis_title=None, yaxis_title=None)
    left.plotly_chart(fig, use_container_width=True)
    mm = q(f"SELECT month, SUM(signings) AS signings FROM marts.market_monthly "
           f"WHERE market_name IN {mfilter} GROUP BY 1 ORDER BY 1")
    right.plotly_chart(px.line(mm, x="month", y="signings", title="Monthly signings"), use_container_width=True)
    st.subheader("Headline insights")
    st.markdown(Path(EXPORT_DIR / "insights.md").read_text().split("\n", 2)[-1])

with tab_score:
    level = st.radio("Level", ["Territory", "Rep"], horizontal=True)
    tbl = "territory" if level == "Territory" else "rep"
    df = q(f"SELECT * FROM scorecard.{tbl} WHERE market_name IN {mfilter} ORDER BY score DESC")
    tiers = st.multiselect("Tier", list(TIER_COLORS), default=list(TIER_COLORS))
    df = df[df["tier"].isin(tiers)]
    name_cols = ["market_name", "territory_name"] + (["rep_name", "role"] if tbl == "rep" else [])
    show = df[name_cols + ["signings", "target_signings", "att_signings", "att_new_volume",
                           "activation_rate", "mid_large_mix", "score", "tier", "biggest_gap"]]
    st.dataframe(show, use_container_width=True, hide_index=True, column_config={
        c: st.column_config.ProgressColumn(c, format="percent", min_value=0, max_value=1.5)
        for c in ["att_signings", "att_new_volume", "score"]
    } | {"target_signings": st.column_config.NumberColumn(format="%.0f"),
         "activation_rate": st.column_config.NumberColumn(format="percent"),
         "mid_large_mix": st.column_config.NumberColumn(format="percent")})
    st.plotly_chart(px.histogram(df, x="score", color="tier", color_discrete_map=TIER_COLORS, nbins=30,
                                 title=f"{level} score distribution"), use_container_width=True)

with tab_fc:
    mk = st.selectbox("Market", sel or markets)
    act = q(f"SELECT month, signings FROM marts.market_monthly WHERE market_name = '{mk}' ORDER BY month")
    fc = q(f"SELECT month, forecast, lower, upper, model FROM forecast.signings WHERE market_name = '{mk}' ORDER BY month")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=pd.concat([fc["month"], fc["month"][::-1]]),
                             y=pd.concat([fc["upper"], fc["lower"][::-1]]), fill="toself",
                             fillcolor="rgba(31,58,95,0.15)", line=dict(width=0), name="80% interval"))
    fig.add_trace(go.Scatter(x=act["month"], y=act["signings"], name="Actual", line=dict(color="#1F3A5F")))
    fig.add_trace(go.Scatter(x=fc["month"], y=fc["forecast"], name=f"Forecast ({fc['model'].iloc[0]})",
                             line=dict(color="#1F3A5F", dash="dash")))
    fig.update_layout(title=f"{mk}: monthly signings, actual and forecast")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Model chosen per market by rolling-origin back-test (lowest WAPE). Interval from the model's own back-test errors.")
    st.dataframe(q(f"SELECT model, mean_wape, selected FROM forecast.model_selection WHERE market_name = '{mk}' ORDER BY mean_wape"),
                 hide_index=True, column_config={"mean_wape": st.column_config.NumberColumn(format="percent")})

with tab_targets:
    tp = q(f"""SELECT t.*, d.market_name FROM targets.territory_plan t
               JOIN (SELECT DISTINCT market_id, market_name FROM marts.dim_rep) d USING (market_id)
               WHERE plan_year = {plan_year + 1} AND d.market_name IN {mfilter}""")
    st.markdown("Each territory's share of its market plan blends **history**, **white space** and "
                "**ramp-adjusted capacity**, then is capped against last year's share.")
    long = tp.melt(id_vars=["territory_name"], value_vars=["historical_share", "whitespace_share", "capacity_share", "final_share"],
                   var_name="component", value_name="share")
    st.plotly_chart(px.bar(long, x="territory_name", y="share", color="component", barmode="group",
                           title="How each territory's share was built").update_layout(yaxis_tickformat=".0%"),
                    use_container_width=True)
    st.dataframe(tp[["market_name", "territory_name", "prior_12m_signings", "annual_target", "change_vs_prior_pct"]],
                 hide_index=True, use_container_width=True,
                 column_config={"annual_target": st.column_config.NumberColumn(format="%.0f"),
                                "change_vs_prior_pct": st.column_config.NumberColumn(format="percent")})

with tab_dq:
    dqr = q("SELECT category, check_id, severity, passed, failing_rows, detail FROM dq.results ORDER BY category, check_id")
    c = st.columns(3)
    c[0].metric("Checks", len(dqr))
    c[1].metric("Critical failures", int(((dqr["severity"] == "CRITICAL") & ~dqr["passed"]).sum()))
    c[2].metric("Rows quarantined", f"{int(dqr.loc[(dqr['category']=='Validity') & (dqr['severity']=='WARNING'), 'failing_rows'].sum()):,}")
    st.dataframe(dqr, hide_index=True, use_container_width=True)
    qm = q("SELECT 'merchants' AS source, dq_issue, COUNT(*) AS n FROM dq.quarantine_merchants GROUP BY 1,2 "
           "UNION ALL SELECT 'merchant_volume', dq_issue, COUNT(*) FROM dq.quarantine_volume GROUP BY 1,2")
    st.plotly_chart(px.bar(qm, x="n", y="dq_issue", color="source", orientation="h", title="Quarantined rows by reason code"),
                    use_container_width=True)
    st.caption("Every quarantined row is kept with its reason code in dq.quarantine_* for remediation by the source owner.")
