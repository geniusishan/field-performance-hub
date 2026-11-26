"""Publish: formatted Excel scorecard pack, Tableau/Power BI extracts, and a rule-based insights brief."""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from fph.config import EXPORT_DIR

NAVY = "1F3A5F"
TIER_FILL = {"Exceeds": "C6EFCE", "Meets": "E2EFDA", "Below": "FFEB9C", "Needs Attention": "FFC7CE"}

SCORE_COLS = {
    "signings": ("Signings", "#,##0"), "target_signings": ("Signings Target", "#,##0"),
    "att_signings": ("Signings Att.", "0%"), "new_volume": ("New Volume (USD)", "$#,##0"),
    "target_new_volume": ("Volume Target (USD)", "$#,##0"), "att_new_volume": ("Volume Att.", "0%"),
    "activation_rate": ("90d Activation", "0%"), "mid_large_mix": ("Mid/Large Mix", "0%"),
    "score": ("Weighted Score", "0%"), "tier": ("Tier", "@"), "biggest_gap": ("Biggest Gap", "@"),
}


def _write_sheet(wb: Workbook, title: str, df: pd.DataFrame, id_cols: dict[str, str], note: str) -> None:
    ws = wb.create_sheet(title)
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)
    ws["A2"] = note
    ws["A2"].font = Font(italic=True, color="666666")
    cols = list(id_cols.items()) + [(c, SCORE_COLS[c][0]) for c in SCORE_COLS if c in df.columns]
    fmts = {c: SCORE_COLS[c][1] for c in SCORE_COLS}
    hdr_row = 4
    for j, (_, label) in enumerate(cols, 1):
        cell = ws.cell(hdr_row, j, label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, row in enumerate(df[[c for c, _ in cols]].itertuples(index=False), hdr_row + 1):
        for j, (c, _) in enumerate(cols, 1):
            v = row[j - 1]
            cell = ws.cell(i, j, None if pd.isna(v) else (v.item() if hasattr(v, "item") else v))
            if c in fmts:
                cell.number_format = fmts[c]
    last = hdr_row + len(df)
    for j, (c, label) in enumerate(cols, 1):
        letter = get_column_letter(j)
        ws.column_dimensions[letter].width = max(12, min(42, len(label) + 4)) if c != "biggest_gap" else 40
        if c.startswith("att_") or c == "score":
            ws.conditional_formatting.add(f"{letter}{hdr_row+1}:{letter}{last}", ColorScaleRule(
                start_type="num", start_value=0.7, start_color="F8696B",
                mid_type="num", mid_value=1.0, mid_color="FFEB84",
                end_type="num", end_value=1.2, end_color="63BE7B"))
        if c == "tier":
            for tier, color in TIER_FILL.items():
                ws.conditional_formatting.add(f"{letter}{hdr_row+1}:{letter}{last}", CellIsRule(
                    operator="equal", formula=[f'"{tier}"'], fill=PatternFill("solid", fgColor=color)))
    ws.freeze_panes = ws.cell(hdr_row + 1, len(id_cols) + 1)
    ws.auto_filter.ref = f"A{hdr_row}:{get_column_letter(len(cols))}{last}"


def _write_plain(wb: Workbook, title: str, df: pd.DataFrame, fmts: dict[str, str] | None = None) -> None:
    ws = wb.create_sheet(title)
    df = df.copy()
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].dt.date
    ws.append(list(df.columns))
    for row in df.itertuples(index=False):
        ws.append([None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in row])
    for j, c in enumerate(df.columns, 1):
        ws.column_dimensions[get_column_letter(j)].width = max(12, min(60, len(str(c)) + 6))
        if fmts and c in fmts:
            for r in range(2, len(df) + 2):
                ws.cell(r, j).number_format = fmts[c]
    if len(df):
        tab = Table(displayName=title.replace(" ", "_").replace("-", "_"),
                    ref=f"A1:{get_column_letter(len(df.columns))}{len(df)+1}")
        tab.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(tab)
    ws.freeze_panes = "A2"


def insights(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Deterministic, fully traceable headline findings (no LLM involved)."""
    m = con.execute("SELECT * FROM scorecard.market ORDER BY score DESC").df()
    r = con.execute("SELECT * FROM scorecard.rep").df()
    t = con.execute("SELECT * FROM scorecard.territory ORDER BY score").df()
    as_of = pd.Timestamp(m["as_of_month"].iloc[0])
    total_att = m["signings"].sum() / m["target_signings"].sum()
    lines = [
        f"Global YTD signings are {m['signings'].sum():,.0f} against a target of {m['target_signings'].sum():,.0f} "
        f"({total_att:.0%} attainment) through {as_of:%b %Y}.",
        f"Strongest market: {m.iloc[0]['market_name']} (score {m.iloc[0]['score']:.0%}); "
        f"weakest: {m.iloc[-1]['market_name']} (score {m.iloc[-1]['score']:.0%}, biggest gap: {m.iloc[-1]['biggest_gap']}).",
    ]
    tiers = r["tier"].value_counts()
    lines.append("Rep tier mix: " + ", ".join(f"{k} {tiers.get(k, 0)}" for k in TIER_FILL) + f" (of {len(r)} reps).")
    watch = t.head(3)
    lines.append("Territories needing attention: " + "; ".join(
        f"{x.territory_name} ({x.score:.0%}, {x.biggest_gap})" for x in watch.itertuples()) + ".")
    gd = con.execute("SELECT * FROM insights.gap_drivers WHERE level = 'market' ORDER BY gap").df()
    if len(gd):
        w = gd.iloc[0]
        effects = {"headcount": w["headcount_effect"], "ramp & role mix": w["ramp_effect"], "productivity": w["productivity_effect"]}
        lines.append(f"Largest gap to target: {w['market_name']} ({w['gap']:+,.0f} signings), driven mainly by "
                     f"{w['main_driver'].lower()} (headcount {effects['headcount']:+,.0f}, ramp & role mix "
                     f"{effects['ramp & role mix']:+,.0f}, productivity {effects['productivity']:+,.0f}).")
    gap_counts = r.loc[r["tier"].isin(["Below", "Needs Attention"]), "biggest_gap"].value_counts()
    if len(gap_counts):
        lines.append(f"Most common gap among under-performing reps: {gap_counts.index[0]} ({gap_counts.iloc[0]} reps).")
    dq = con.execute("SELECT COUNT(*) FILTER (WHERE NOT passed AND severity='CRITICAL'), "
                     "COALESCE(SUM(failing_rows) FILTER (WHERE category='Validity' AND severity='WARNING'), 0) FROM dq.results").fetchone()
    lines.append(f"Data quality: {dq[0]} critical failures; {int(dq[1]):,} source rows quarantined with reason codes.")
    return lines


def publish(con: duckdb.DuckDBPyConnection, plan_year: int, next_plan_year: int) -> dict[str, Path]:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    as_of = pd.Timestamp(con.execute("SELECT as_of_month FROM staging.run_params").fetchone()[0])
    wb = Workbook()
    ws = wb.active
    ws.title = "Executive Summary"
    ws["A1"] = f"Field Performance Scorecard - {plan_year} YTD through {as_of:%B %Y}"
    ws["A1"].font = Font(bold=True, size=16, color=NAVY)
    for i, line in enumerate(insights(con), 3):
        ws.cell(i, 1, f"• {line}").alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[i].height = 32
    ws.column_dimensions["A"].width = 140

    m = con.execute("SELECT * FROM scorecard.market ORDER BY score DESC").df()
    t = con.execute("SELECT * FROM scorecard.territory ORDER BY market_id, rank_in_market").df()
    r = con.execute("SELECT * FROM scorecard.rep ORDER BY market_id, rank_in_market").df()
    _write_sheet(wb, "Market Scorecard", m, {"market_name": "Market", "region": "Region"},
                 "Attainment capped at 150%. Score = weighted KPI attainment.")
    _write_sheet(wb, "Territory Scorecard", t, {"market_name": "Market", "territory_name": "Territory",
                                                "rank_in_market": "Rank"}, "Ranked within market.")
    _write_sheet(wb, "Rep Scorecard", r, {"market_name": "Market", "territory_name": "Territory",
                                          "rep_name": "Rep", "role": "Role", "rank_in_market": "Rank"},
                 "Targets are ramp-adjusted for tenure.")
    plan = con.execute(f"""
        SELECT market_id, territory_name, prior_12m_signings, historical_share, whitespace_share,
               capacity_share, blended_share, final_share, annual_target, change_vs_prior_pct
        FROM targets.territory_plan WHERE plan_year = {next_plan_year} ORDER BY market_id, territory_name
    """).df()
    _write_plain(wb, f"{next_plan_year} Targets", plan,
                 {c: "0.0%" for c in ["historical_share", "whitespace_share", "capacity_share", "blended_share",
                                      "final_share", "change_vs_prior_pct"]} | {"annual_target": "#,##0"})
    _write_plain(wb, "Gap Drivers", con.execute(
        "SELECT level, market_name, territory_name, actual, ROUND(target, 1) AS target, ROUND(gap, 1) AS gap, gap_pct, "
        "ROUND(headcount_effect, 1) AS headcount_effect, ROUND(ramp_effect, 1) AS ramp_effect, "
        "ROUND(productivity_effect, 1) AS productivity_effect, main_driver, ROUND(avg_reps_actual, 1) AS avg_reps_actual, "
        "ROUND(avg_reps_plan, 1) AS avg_reps_plan FROM insights.gap_drivers ORDER BY level, gap").df(),
        {"gap_pct": "0.0%"})
    _write_plain(wb, "Forecast Models", con.execute(
        "SELECT market_name, model, mean_wape, selected FROM forecast.model_selection ORDER BY market_name, mean_wape").df(),
        {"mean_wape": "0.0%"})
    _write_plain(wb, "Data Quality", con.execute(
        "SELECT check_id, category, table_name, severity, passed, failing_rows, detail FROM dq.results").df())
    xlsx = EXPORT_DIR / f"field_scorecard_{plan_year}_YTD_{as_of:%Y%m}.xlsx"
    wb.save(xlsx)

    # flat extracts for Tableau Public / Power BI
    bi = EXPORT_DIR / "bi"
    bi.mkdir(exist_ok=True)
    con.execute(f"COPY (SELECT * FROM marts.rep_monthly) TO '{(bi/'rep_monthly.csv').as_posix()}' (HEADER)")
    con.execute(f"COPY (SELECT * FROM scorecard.rep) TO '{(bi/'rep_scorecard.csv').as_posix()}' (HEADER)")
    con.execute(f"""COPY (
        SELECT m.market_id, m.market_name, m.month, m.signings AS actual, NULL AS forecast, NULL AS lower, NULL AS upper
        FROM marts.market_monthly m
        UNION ALL
        SELECT market_id, market_name, month, NULL, forecast, lower, upper FROM forecast.signings
    ) TO '{(bi/'forecast.csv').as_posix()}' (HEADER)""")
    con.execute(f"COPY (SELECT * FROM targets.rep_monthly) TO '{(bi/'targets.csv').as_posix()}' (HEADER)")

    brief = EXPORT_DIR / "insights.md"
    brief.write_text(f"# Field performance brief - {as_of:%B %Y}\n\n" + "\n".join(f"- {l}" for l in insights(con)) + "\n")
    return {"excel": xlsx, "bi_extracts": bi, "brief": brief}
