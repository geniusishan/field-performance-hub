"""Data-quality and governance checks.

Three kinds of control, matching how a governance / risk function thinks about reporting:
  * Validity    - every rule violation is quarantined with a reason code, never silently dropped
  * Integrity   - keys unique, joins complete, no negative money
  * Reconciliation - every raw row is accounted for (clean + quarantined = raw), and each
                     mart layer ties back to the one below it
plus freshness and a statistical anomaly scan on the published numbers.

Results are written to dq.results (latest run) and dq.results_history (audit trail).
A CRITICAL failure stops the pipeline before anything is published.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import duckdb
import pandas as pd

from fph.config import settings


@dataclass
class Check:
    check_id: str
    category: str
    table_name: str
    severity: str          # CRITICAL | WARNING | INFO
    passed: bool
    failing_rows: int
    detail: str


def _scalar(con, sql: str):
    return con.execute(sql).fetchone()[0]


def run_checks(con: duckdb.DuckDBPyConnection, run_id: str) -> pd.DataFrame:
    cfg = settings()["data_quality"]
    checks: list[Check] = []

    # --- validity: quarantine with reason codes ------------------------------
    con.execute("CREATE SCHEMA IF NOT EXISTS dq")
    con.execute("CREATE OR REPLACE TABLE dq.quarantine_merchants AS "
                "SELECT * FROM staging.merchants_flagged WHERE dq_issue IS NOT NULL")
    con.execute("CREATE OR REPLACE TABLE dq.quarantine_volume AS "
                "SELECT * FROM staging.volume_flagged WHERE dq_issue IS NOT NULL")
    for tbl, src in (("merchants", "staging.merchants_flagged"), ("merchant_volume", "staging.volume_flagged")):
        for issue, n in con.execute(
            f"SELECT dq_issue, COUNT(*) FROM {src} WHERE dq_issue IS NOT NULL GROUP BY 1 ORDER BY 1"
        ).fetchall():
            checks.append(Check(f"validity.{tbl}.{issue.lower()}", "Validity", f"raw.{tbl}", "WARNING",
                                False, n, f"{n:,} rows quarantined as {issue}"))
        total = _scalar(con, f"SELECT COUNT(*) FROM {src}")
        bad = _scalar(con, f"SELECT COUNT(*) FROM {src} WHERE dq_issue IS NOT NULL")
        rate = bad / total if total else 0
        checks.append(Check(f"validity.{tbl}.quarantine_rate", "Validity", f"raw.{tbl}", "CRITICAL",
                            rate <= cfg["max_quarantine_rate"], bad,
                            f"quarantine rate {rate:.2%} (threshold {cfg['max_quarantine_rate']:.0%})"))

    # --- integrity -----------------------------------------------------------
    integrity = [
        ("integrity.merchants.unique_id", "staging.merchants",
         "SELECT COUNT(*) - COUNT(DISTINCT merchant_id) FROM staging.merchants"),
        ("integrity.merchants.not_null_keys", "staging.merchants",
         "SELECT COUNT(*) FROM staging.merchants WHERE merchant_id IS NULL OR rep_id IS NULL OR territory_id IS NULL"),
        ("integrity.signings.rep_fk", "marts.fct_signings",
         "SELECT COUNT(*) FROM staging.merchants m LEFT JOIN marts.dim_rep d USING (rep_id) WHERE d.rep_id IS NULL"),
        ("integrity.volume.non_negative", "staging.merchant_volume",
         "SELECT COUNT(*) FROM staging.merchant_volume WHERE charge_volume_usd < 0"),
        ("integrity.volume.unique_grain", "staging.merchant_volume",
         "SELECT COUNT(*) FROM (SELECT merchant_id, month FROM staging.merchant_volume GROUP BY 1,2 HAVING COUNT(*) > 1)"),
        ("integrity.rep_monthly.unique_grain", "marts.rep_monthly",
         "SELECT COUNT(*) FROM (SELECT rep_id, month FROM marts.rep_monthly GROUP BY 1,2 HAVING COUNT(*) > 1)"),
    ]
    for cid, tbl, sql in integrity:
        n = _scalar(con, sql)
        checks.append(Check(cid, "Integrity", tbl, "CRITICAL", n == 0, n,
                            "OK" if n == 0 else f"{n:,} violating rows"))

    # --- reconciliation ------------------------------------------------------
    recon = [
        ("recon.merchants.raw_equals_clean_plus_quarantine", "staging.merchants",
         "SELECT (SELECT COUNT(*) FROM raw.merchants) - (SELECT COUNT(*) FROM staging.merchants)"
         " - (SELECT COUNT(*) FROM dq.quarantine_merchants)"),
        ("recon.volume.raw_equals_clean_plus_quarantine", "staging.merchant_volume",
         "SELECT (SELECT COUNT(*) FROM raw.merchant_volume) - (SELECT COUNT(*) FROM staging.merchant_volume)"
         " - (SELECT COUNT(*) FROM dq.quarantine_volume)"),
        ("recon.signings.fact_to_rep_monthly", "marts.rep_monthly",
         "SELECT (SELECT COUNT(*) FROM marts.fct_signings) - (SELECT SUM(signings) FROM marts.rep_monthly)"),
        ("recon.signings.rep_to_market", "marts.market_monthly",
         "SELECT (SELECT SUM(signings) FROM marts.rep_monthly) - (SELECT SUM(signings) FROM marts.market_monthly)"),
        ("recon.volume.rep_to_market_usd", "marts.market_monthly",
         "SELECT ROUND((SELECT SUM(new_merchant_volume_usd) FROM marts.rep_monthly)"
         " - (SELECT SUM(new_merchant_volume_usd) FROM marts.market_monthly), 2)"),
    ]
    for cid, tbl, sql in recon:
        diff = _scalar(con, sql) or 0
        checks.append(Check(cid, "Reconciliation", tbl, "CRITICAL", abs(diff) < 0.01, int(abs(diff)),
                            "ties out" if abs(diff) < 0.01 else f"difference of {diff:,}"))

    # --- freshness -----------------------------------------------------------
    latest, as_of = con.execute(
        "SELECT (SELECT MAX(month) FROM marts.market_monthly), (SELECT as_of_month FROM staging.run_params)"
    ).fetchone()
    checks.append(Check("freshness.market_monthly.latest_month", "Freshness", "marts.market_monthly",
                        "CRITICAL", latest == as_of, 0, f"latest month {latest}, expected {as_of}"))

    # --- anomaly scan (robust z-score vs trailing 12 months) -------------------
    mm = con.execute("SELECT market_id, month, signings FROM marts.market_monthly ORDER BY market_id, month").df()
    anomalies = []
    for mkt, g in mm.groupby("market_id"):
        s = g.set_index("month")["signings"].astype(float)
        med = s.shift(1).rolling(12, min_periods=6).median()
        mad = (s.shift(1) - med).abs().rolling(12, min_periods=6).median().replace(0, 1)
        z = 0.6745 * (s - med) / mad
        for m, zz in z[z.abs() > 3.5].items():
            anomalies.append(f"{mkt} {pd.Timestamp(m):%Y-%m} (z={zz:+.1f})")
    checks.append(Check("anomaly.market_monthly.signings", "Anomaly", "marts.market_monthly", "WARNING",
                        not anomalies, len(anomalies),
                        "no anomalies" if not anomalies else "review: " + ", ".join(anomalies[:8])))

    df = pd.DataFrame([asdict(c) for c in checks])
    df.insert(0, "run_id", run_id)
    df["checked_at"] = pd.Timestamp.now().floor("s")
    con.execute("CREATE OR REPLACE TABLE dq.results AS SELECT * FROM df")
    con.execute("CREATE TABLE IF NOT EXISTS dq.results_history AS SELECT * FROM df WHERE 1=0")
    con.execute("INSERT INTO dq.results_history SELECT * FROM df")
    return df


def critical_failures(results: pd.DataFrame) -> pd.DataFrame:
    return results[(results["severity"] == "CRITICAL") & (~results["passed"])]
