"""Field Performance Hub - executive performance dashboard.

    streamlit run app/dashboard.py

Pick a period, what to compare it with (target, same period last year, previous period)
and a scope (region, market, territory). Every view answers the question it shows:
KPI tiles with deltas, a plain-English "What's happening" summary, the trend, a gap
bridge that explains *why* (headcount, ramp & role mix, productivity), and a ranked
leaderboard. Reads only published warehouse tables, on a read-only connection.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fph import drivers, warehouse  # noqa: E402
from fph.config import EXPORT_DIR, settings  # noqa: E402

st.set_page_config(page_title="Field Performance Hub", page_icon="📊", layout="wide")

NAVY, GREY, GREEN, RED = "#1F3A5F", "#9AA5B1", "#2E7D32", "#C62828"
TIER_COLORS = {"Exceeds": "#2E7D32", "Meets": "#81C784", "Below": "#FFB300", "Needs Attention": "#E53935"}
GOALS = {k: v["goal"] for k, v in settings()["scorecard"]["kpis"].items() if "goal" in v}


# ---------------------------------------------------------------- data
@st.cache_resource(show_spinner="First launch: building the warehouse (about 10 seconds)…")
def ensure_warehouse() -> bool:
    """On a fresh deploy (e.g. Streamlit Community Cloud) build the data on first load."""
    from fph import pipeline
    from fph.config import warehouse_path
    if not warehouse_path().exists() or not _has_table("insights", "gap_drivers"):
        pipeline.run(verbose=False)   # first launch, or data built by an older version of the app
    return True


def _has_table(schema: str, table: str) -> bool:
    try:
        con = warehouse.connect(read_only=True)
    except Exception:  # noqa: BLE001
        return False
    try:
        return bool(con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
                                [schema, table]).fetchone()[0])
    finally:
        con.close()


@st.cache_data(ttl=600)
def q(sql: str) -> pd.DataFrame:
    con = warehouse.connect(read_only=True)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


@st.cache_data(ttl=600)
def frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    con = warehouse.connect(read_only=True)
    try:
        return drivers.actual_frame(con), drivers.plan_frame(con)
    finally:
        con.close()


@st.cache_data(ttl=600)
def bridge_table(start, end, mode, level, markets: tuple, territories: tuple) -> pd.DataFrame:
    a, p = frames()
    df = drivers.by_group(a, p, start, end, mode, level, list(markets))
    if territories and level == "territory":
        df = df[df["territory_name"].isin(territories)]
    return df


try:
    ensure_warehouse()
    as_of = pd.Timestamp(q("SELECT as_of_month FROM staging.run_params").iloc[0, 0])
    A, P = frames()
except Exception as e:  # noqa: BLE001
    st.error(f"Warehouse unavailable: {e}. Run `python -m fph.pipeline run`.")
    st.stop()

plan_year = as_of.year
months = [pd.Timestamp(m) for m in sorted(A["month"].unique())]

# ---------------------------------------------------------------- filters
with st.sidebar:
    st.header("Filters")
    start, end = st.select_slider(
        "Period", options=months, value=(pd.Timestamp(f"{plan_year}-01-01"), as_of),
        format_func=lambda m: f"{m:%b %Y}")
    mode_label = st.radio("Compare with", list(drivers.COMPARISONS.values()))
    mode = {v: k for k, v in drivers.COMPARISONS.items()}[mode_label]

    regions = sorted(A["region"].unique())
    sel_regions = st.multiselect("Region", regions, default=regions)
    market_opts = sorted(A.loc[A["region"].isin(sel_regions), "market_name"].unique())
    sel_markets = st.multiselect("Market", market_opts, default=market_opts)
    terr_opts = sorted(A.loc[A["market_name"].isin(sel_markets), "territory_name"].unique())
    sel_terrs = st.multiselect("Territory (optional)", terr_opts, default=[],
                               placeholder="All territories")
    st.caption("The period and comparison apply to the Performance tab. Scorecards are "
               f"{plan_year} year-to-date through {as_of:%b %Y}.")

if not sel_markets:
    st.warning("Select at least one market.")
    st.stop()

# comparison availability: fall back gracefully instead of showing a misleading number
notice = None
first_month = months[0]
if mode == "target" and not drivers.target_available(P, start, end):
    notice = f"Targets exist only for {plan_year}; comparing with the same period last year instead."
    mode = "last_year"
if mode != "target":
    cs, ce = drivers.comparison_months(start, end, mode)
    if cs < first_month:
        alt = "previous" if mode == "last_year" else "last_year"
        acs, _ = drivers.comparison_months(start, end, alt)
        if acs >= first_month:
            notice = f"Not enough history for '{drivers.COMPARISONS[mode]}'; showing '{drivers.COMPARISONS[alt]}'."
            mode = alt
        else:
            notice = "Not enough history before this period for a comparison; pick a later start month."
            mode = None
cmp_label = drivers.COMPARISONS.get(mode, "—")
short = {"target": "target", "last_year": "LY", "previous": "prev. period"}.get(mode, "")


def scope(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df["market_name"].isin(sel_markets)]
    return df[df["territory_name"].isin(sel_terrs)] if sel_terrs else df


def in_range(df: pd.DataFrame, s, e) -> pd.DataFrame:
    return df[(df["month"] >= s) & (df["month"] <= e)]


n_months = (end.to_period("M") - start.to_period("M")).n + 1
cur = scope(in_range(A, start, end))
if mode == "target":
    cmp_df = scope(in_range(P, start, end))
elif mode:
    cs, ce = drivers.comparison_months(start, end, mode)
    cmp_df = scope(in_range(A, cs, ce))
else:
    cmp_df = None

scope_label = (", ".join(sel_terrs) if sel_terrs and len(sel_terrs) <= 2 else
               f"{len(sel_terrs)} territories" if sel_terrs else
               sel_markets[0] if len(sel_markets) == 1 else
               "All markets" if len(sel_markets) == len(A["market_name"].unique()) else f"{len(sel_markets)} markets")

st.title("📊 Field Performance Hub")
st.caption(f"Synthetic data · as of {as_of:%B %Y} · run "
           f"{q('SELECT run_id FROM ops.run_log ORDER BY logged_at DESC LIMIT 1').iloc[0, 0]}")

tab_perf, tab_score, tab_fc, tab_targets, tab_dq = st.tabs(
    ["📈 Performance", "🏆 Scorecards", "🔮 Forecast", f"🎯 {plan_year + 1} Targets", "🛡️ Data Quality"])

# ================================================================ PERFORMANCE
with tab_perf:
    st.subheader(f"{scope_label} · {start:%b %Y} – {end:%b %Y}" + (f" vs {cmp_label.lower()}" if mode else ""))
    if notice:
        st.info(notice)

    # ---- KPI tiles
    def ratio(n, d):
        return n / d if d else float("nan")

    a_sign = cur["signings"].sum()
    a_vol = cur["new_merchant_volume_usd"].sum()
    a_act = ratio(cur["activated_90d"].sum(), cur["activation_eligible"].sum())
    a_mix = ratio(cur["mid_large_signings"].sum(), a_sign)
    a_reps = len(cur) / n_months
    if mode == "target":
        c_sign, c_vol = cmp_df["target_signings"].sum(), cmp_df["target_new_volume_usd"].sum()
        c_act, c_mix = GOALS["activation_90d"], GOALS["quality_mix"]
        c_reps = cmp_df["planned_rep_months"].sum() / n_months
        act_lbl, mix_lbl, reps_lbl = "vs goal", "vs goal", "vs plan"
    elif mode:
        c_sign, c_vol = cmp_df["signings"].sum(), cmp_df["new_merchant_volume_usd"].sum()
        c_act = ratio(cmp_df["activated_90d"].sum(), cmp_df["activation_eligible"].sum())
        c_mix = ratio(cmp_df["mid_large_signings"].sum(), c_sign)
        c_reps = len(cmp_df) / n_months
        act_lbl = mix_lbl = reps_lbl = f"vs {short}"
    k = st.columns(5)
    if mode:
        k[0].metric("Signings", f"{a_sign:,.0f}", f"{ratio(a_sign, c_sign) - 1:+.1%} vs {short}")
        k[1].metric("New-merchant volume", f"${a_vol / 1e6:,.1f}M", f"{ratio(a_vol, c_vol) - 1:+.1%} vs {short}",
                    help="Charge volume from merchants signed in the same calendar year, so it builds through the "
                         "year. Compare within a year (target or same period last year) for like-for-like.")
        k[2].metric("90-day activation", f"{a_act:.1%}", f"{(a_act - c_act) * 100:+.1f} pts {act_lbl}")
        k[3].metric("Mid/Large mix", f"{a_mix:.1%}", f"{(a_mix - c_mix) * 100:+.1f} pts {mix_lbl}")
        k[4].metric("Avg active reps", f"{a_reps:,.1f}", f"{a_reps - c_reps:+.1f} {reps_lbl}")
    else:
        for col, (lbl, val) in zip(k, [("Signings", f"{a_sign:,.0f}"), ("New-merchant volume", f"${a_vol / 1e6:,.1f}M"),
                                       ("90-day activation", f"{a_act:.1%}"), ("Mid/Large mix", f"{a_mix:.1%}"),
                                       ("Avg active reps", f"{a_reps:,.1f}")]):
            col.metric(lbl, val)

    if mode:
        b = drivers.compute(A, P, start, end, mode, markets=sel_markets, territories=sel_terrs or None)
        level = "territory" if (len(sel_markets) == 1 or sel_terrs) else "market"
        groups = bridge_table(start, end, mode, level, tuple(sel_markets), tuple(sel_terrs))
        name_col = "market_name" if level == "market" else "territory_name"

        # ---- What's happening (every number computed, none written by hand)
        with st.container(border=True):
            st.markdown("**What's happening**")
            lines = drivers.explain(b, cmp_label)
            top = {"Headcount": b.headcount, "Ramp & role mix": b.ramp, "Productivity": b.productivity}[b.main_driver]
            why = next(l for l in lines[1:-1] if l.startswith(b.main_driver)).split("(", 1)[1].rstrip(").")
            bullets = [lines[0], f"Main driver: **{b.main_driver.lower()}**, {top:+,.0f} signings ({why}). "
                                 f"See the bridge below for all three effects."]
            g = groups.dropna(subset=["gap_pct"]).sort_values("gap_pct")
            if len(g) >= 2:
                best, worst = g.iloc[-1], g.iloc[0]
                bullets.append(f"Best {level}: **{best[name_col]}** ({best['gap_pct']:+.1%}, mainly {best['main_driver'].lower()}). "
                               f"Furthest behind: **{worst[name_col]}** ({worst['gap_pct']:+.1%}, mainly {worst['main_driver'].lower()}).")
            monthly = cur.groupby("month")["signings"].sum().sort_index()
            if len(monthly) >= 6:
                last3, prev3 = monthly.iloc[-3:].sum(), monthly.iloc[-6:-3].sum()
                trend = "up" if last3 >= prev3 else "down"
                bullets.append(f"Momentum: the last 3 months are {trend} {abs(ratio(last3, prev3) - 1):.1%} on the 3 months before.")
            if a_act - c_act <= -0.02:
                bullets.append(f"Watch: 90-day activation is {a_act:.1%}, {abs(a_act - c_act) * 100:.1f} pts below {act_lbl.replace('vs ', '')}.")
            st.markdown("\n".join(f"- {x}" for x in bullets))

        # ---- Trend + bridge
        left, right = st.columns([3, 2])
        monthly_cur = cur.groupby("month")["signings"].sum()
        if mode == "target":
            monthly_cmp = cmp_df.groupby("month")["target_signings"].sum()
        else:
            offset = pd.DateOffset(years=1) if mode == "last_year" else pd.DateOffset(months=n_months)
            monthly_cmp = cmp_df.groupby("month")["signings"].sum()
            monthly_cmp.index = monthly_cmp.index + offset          # align to the current months
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=monthly_cmp.index, y=monthly_cmp.values, name=cmp_label,
                                 line=dict(color=GREY, dash="dash", width=2)))
        fig.add_trace(go.Scatter(x=monthly_cur.index, y=monthly_cur.values, name="Actual", mode="lines+markers",
                                 line=dict(color=NAVY, width=3)))
        fig.update_layout(title=f"Monthly signings vs {cmp_label.lower()}", height=380, hovermode="x unified",
                          legend=dict(orientation="h", y=1.12), margin=dict(l=10, r=10, t=60, b=10))
        left.plotly_chart(fig, width="stretch")

        wf = go.Figure(go.Waterfall(
            x=[{"target": "Target", "last_year": "Last year", "previous": "Prev. period"}[mode],
               "Headcount", "Ramp & mix", "Productivity", "Actual"],
            measure=["absolute", "relative", "relative", "relative", "total"],
            y=[b.comparison, b.headcount, b.ramp, b.productivity, b.actual],
            text=[f"{b.comparison:,.0f}", f"{b.headcount:+,.0f}", f"{b.ramp:+,.0f}", f"{b.productivity:+,.0f}", f"{b.actual:,.0f}"],
            textposition="outside", connector=dict(line=dict(color=GREY)),
            increasing=dict(marker=dict(color=GREEN)), decreasing=dict(marker=dict(color=RED)),
            totals=dict(marker=dict(color=NAVY))))
        lo = min(b.comparison, b.actual) - max(abs(b.headcount), abs(b.ramp), abs(b.productivity)) * 2.2
        wf.update_layout(title="Why the gap? Signings bridge", height=380, showlegend=False,
                         xaxis=dict(tickangle=0),
                         yaxis=dict(range=[max(0, lo), max(b.comparison, b.actual) * 1.04]),
                         margin=dict(l=10, r=10, t=60, b=10))
        right.plotly_chart(wf, width="stretch")
        with st.expander("How to read the bridge"):
            st.markdown(
                "Signings = **reps** × **capacity per rep** (new hires count 50%, rising to 100% after a year; "
                "Account Development reps carry a smaller new-signings share) × **signings per unit of capacity**.\n\n"
                "- **Headcount:** more or fewer reps than the comparison (vacancies, attrition, hiring).\n"
                "- **Ramp & role mix:** capacity lost to reps still ramping up, or a different role mix.\n"
                "- **Productivity:** what each fully productive rep actually signed.\n\n"
                "The three effects add up exactly to the gap. For targets, the comparison team is the plan: "
                "the reps employed when the plan was set, plus planned hiring.")

        # ---- Comparison by market / territory
        g = groups.sort_values("gap_pct")
        bar = go.Figure(go.Bar(
            x=g["gap_pct"], y=g[name_col], orientation="h",
            marker_color=[GREEN if v >= 0 else RED for v in g["gap_pct"]],
            text=[f"{v:+.1%}  ({a:,.0f} vs {c:,.0f})" for v, a, c in zip(g["gap_pct"], g["actual"], g["comparison"])],
            textposition="outside", customdata=g["main_driver"],
            hovertemplate="%{y}: %{x:+.1%}<br>Main driver: %{customdata}<extra></extra>"))
        lo_x, hi_x = min(0.0, g["gap_pct"].min()), max(0.0, g["gap_pct"].max())
        pad = (hi_x - lo_x) * 0.55 or 0.05                       # room for the outside labels
        bar.update_traces(cliponaxis=False)
        bar.update_layout(title=f"Signings vs {cmp_label.lower()} by {level}", height=max(320, 28 * len(g) + 120),
                          xaxis=dict(tickformat="+.0%", zeroline=True, zerolinecolor=GREY,
                                     range=[lo_x - (pad if lo_x < 0 else 0.01), hi_x + (pad if hi_x > 0 else 0.01)]),
                          margin=dict(l=10, r=10, t=60, b=10))
        st.plotly_chart(bar, width="stretch")

        # ---- Leaderboard
        st.markdown(f"**{level.title()} leaderboard** (sorted by gap; click a column to re-sort)")
        board = groups.assign(avg_reps=groups["actual_rep_months"] / groups["months"],
                              cmp_reps=groups["comparison_rep_months"] / groups["months"])
        cols = ([name_col] + (["market_name"] if level == "territory" else [])
                + ["actual", "comparison", "gap", "gap_pct", "main_driver", "headcount", "ramp", "productivity", "avg_reps", "cmp_reps"])
        board = board[cols].sort_values("gap_pct").assign(gap_pct=lambda d: d["gap_pct"] * 100).rename(columns={
            "market_name": "Market", "territory_name": "Territory", "actual": "Actual", "comparison": cmp_label,
            "gap": "Gap", "gap_pct": "Gap %", "main_driver": "Main driver", "headcount": "Headcount effect",
            "ramp": "Ramp & mix effect", "productivity": "Productivity effect", "avg_reps": "Avg reps",
            "cmp_reps": f"Avg reps ({short})"})
        num = {c: st.column_config.NumberColumn(format="%.0f") for c in
               ["Actual", cmp_label, "Gap", "Headcount effect", "Ramp & mix effect", "Productivity effect"]}
        st.dataframe(board, hide_index=True, width="stretch", column_config=num | {
            "Gap %": st.column_config.NumberColumn(format="%+.1f%%"),
            "Avg reps": st.column_config.NumberColumn(format="%.1f"),
            f"Avg reps ({short})": st.column_config.NumberColumn(format="%.1f")})
        st.download_button("Download this view (CSV)", board.to_csv(index=False),
                           file_name=f"performance_{start:%Y%m}_{end:%Y%m}_{mode}.csv")
    else:
        st.line_chart(cur.groupby("month")["signings"].sum())

# ================================================================ SCORECARDS
mfilter = "(" + ",".join("'" + m.replace("'", "''") + "'" for m in sel_markets) + ")"
with tab_score:
    st.caption(f"Official weighted scorecards, {plan_year} year-to-date through {as_of:%B %Y}.")
    level_s = st.radio("Level", ["Territory", "Rep"], horizontal=True)
    tbl = "territory" if level_s == "Territory" else "rep"
    df = q(f"SELECT * FROM scorecard.{tbl} WHERE market_name IN {mfilter} ORDER BY score DESC")
    if tbl == "rep":
        df = df.merge(q("SELECT rep_id, status FROM marts.dim_rep"), on="rep_id", how="left")
    tiers = st.multiselect("Tier", list(TIER_COLORS), default=list(TIER_COLORS))
    df = df[df["tier"].isin(tiers)]
    name_cols = ["market_name", "territory_name"] + (["rep_name", "role", "status"] if tbl == "rep" else [])
    show = df[name_cols + ["signings", "target_signings", "att_signings", "att_new_volume",
                           "activation_rate", "mid_large_mix", "score", "tier", "biggest_gap"]]
    st.dataframe(show, width="stretch", hide_index=True, column_config={
        c: st.column_config.ProgressColumn(c, format="percent", min_value=0, max_value=1.5)
        for c in ["att_signings", "att_new_volume", "score"]
    } | {"target_signings": st.column_config.NumberColumn(format="%.0f"),
         "activation_rate": st.column_config.NumberColumn(format="percent"),
         "mid_large_mix": st.column_config.NumberColumn(format="percent")})
    st.plotly_chart(px.histogram(df, x="score", color="tier", color_discrete_map=TIER_COLORS, nbins=30,
                                 title=f"{level_s} score distribution"), width="stretch")

# ================================================================ FORECAST
with tab_fc:
    mk = st.selectbox("Market", sel_markets)
    act = q(f"SELECT month, signings FROM marts.market_monthly WHERE market_name = '{mk}' ORDER BY month")
    fc = q(f"SELECT month, forecast, lower, upper, model FROM forecast.signings WHERE market_name = '{mk}' ORDER BY month")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=pd.concat([fc["month"], fc["month"][::-1]]),
                             y=pd.concat([fc["upper"], fc["lower"][::-1]]), fill="toself",
                             fillcolor="rgba(31,58,95,0.15)", line=dict(width=0), name="80% interval"))
    fig.add_trace(go.Scatter(x=act["month"], y=act["signings"], name="Actual", line=dict(color=NAVY)))
    fig.add_trace(go.Scatter(x=fc["month"], y=fc["forecast"], name=f"Forecast ({fc['model'].iloc[0]})",
                             line=dict(color=NAVY, dash="dash")))
    fig.update_layout(title=f"{mk}: monthly signings, actual and forecast")
    st.plotly_chart(fig, width="stretch")
    st.caption("Model chosen per market by rolling-origin back-test (lowest WAPE). Interval from the model's own back-test errors.")
    st.dataframe(q(f"SELECT model, mean_wape, selected FROM forecast.model_selection WHERE market_name = '{mk}' ORDER BY mean_wape"),
                 hide_index=True, column_config={"mean_wape": st.column_config.NumberColumn(format="percent")})

# ================================================================ TARGETS
with tab_targets:
    tp = q(f"""SELECT t.*, d.market_name FROM targets.territory_plan t
               JOIN (SELECT DISTINCT market_id, market_name FROM marts.dim_rep) d USING (market_id)
               WHERE plan_year = {plan_year + 1} AND d.market_name IN {mfilter}""")
    st.markdown("Each territory's share of its market plan blends **history**, **white space** and "
                "**ramp- and role-adjusted capacity**, then is capped against last year's share.")
    long = tp.melt(id_vars=["territory_name"], value_vars=["historical_share", "whitespace_share", "capacity_share", "final_share"],
                   var_name="component", value_name="share")
    st.plotly_chart(px.bar(long, x="territory_name", y="share", color="component", barmode="group",
                           title="How each territory's share was built").update_layout(yaxis_tickformat=".0%"),
                    width="stretch")
    st.dataframe(tp[["market_name", "territory_name", "prior_12m_signings", "annual_target", "change_vs_prior_pct"]],
                 hide_index=True, width="stretch",
                 column_config={"annual_target": st.column_config.NumberColumn(format="%.0f"),
                                "change_vs_prior_pct": st.column_config.NumberColumn(format="percent")})

# ================================================================ DATA QUALITY
with tab_dq:
    dqr = q("SELECT category, check_id, severity, passed, failing_rows, detail FROM dq.results ORDER BY category, check_id")
    c = st.columns(3)
    c[0].metric("Checks", len(dqr))
    c[1].metric("Critical failures", int(((dqr["severity"] == "CRITICAL") & ~dqr["passed"]).sum()))
    c[2].metric("Rows quarantined", f"{int(dqr.loc[(dqr['category']=='Validity') & (dqr['severity']=='WARNING'), 'failing_rows'].sum()):,}")
    st.dataframe(dqr, hide_index=True, width="stretch")
    qm = q("SELECT 'merchants' AS source, dq_issue, COUNT(*) AS n FROM dq.quarantine_merchants GROUP BY 1,2 "
           "UNION ALL SELECT 'merchant_volume', dq_issue, COUNT(*) FROM dq.quarantine_volume GROUP BY 1,2")
    st.plotly_chart(px.bar(qm, x="n", y="dq_issue", color="source", orientation="h", title="Quarantined rows by reason code"),
                    width="stretch")
    st.caption("Every quarantined row is kept with its reason code in dq.quarantine_* for remediation by the source owner.")
    st.markdown("**Headline insights (published with the Excel pack)**")
    st.markdown(Path(EXPORT_DIR / "insights.md").read_text().split("\n", 2)[-1])
