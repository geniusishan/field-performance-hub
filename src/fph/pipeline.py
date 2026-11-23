"""One command runs the full monthly close:

    generate/ingest -> SQL models -> data-quality gate -> forecast -> targets -> scorecards -> publish

    python -m fph.pipeline run                      # as of the month in settings.yaml
    python -m fph.pipeline run --as-of 2026-06-01   # rebuild any historical month
    python -m fph.pipeline run --skip-generate      # reuse existing raw files (real-data mode)

Writes a run manifest (row counts, timings, DQ outcome, model choices) to
exports/run_manifest.json and appends it to ops.run_log for auditability.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime

import pandas as pd

from fph import dq, export, forecast, generate, scorecard, targets, warehouse
from fph.config import EXPORT_DIR, settings


def run(as_of: str | None = None, skip_generate: bool = False, verbose: bool = True) -> dict:
    cfg = settings()
    as_of = pd.Timestamp(as_of or cfg["data"]["end_month"]).to_period("M").to_timestamp()
    plan_year = as_of.year
    run_id = f"{as_of:%Y%m}-{uuid.uuid4().hex[:8]}"
    t0 = time.perf_counter()
    log = (lambda *a: print(*a, flush=True)) if verbose else (lambda *a: None)
    manifest: dict = {"run_id": run_id, "as_of_month": str(as_of.date()), "started_at": datetime.now().isoformat(timespec="seconds")}

    if not skip_generate:
        ds = generate.generate()
        generate.write_raw(ds)
        manifest["injected_defects"] = ds.injected
        log(f"[1/7] generated synthetic source data ({len(ds.merchants):,} merchant rows)")

    con = warehouse.connect()
    try:
        manifest["raw_rows"] = warehouse.load_raw(con)
        warehouse.set_run_params(con, as_of)
        runs = warehouse.run_models(con)
        manifest["models"] = [r.__dict__ for r in runs]
        log(f"[2/7] built {len(runs)} SQL models")

        results = dq.run_checks(con, run_id)
        crit = dq.critical_failures(results)
        manifest["dq"] = {"checks": len(results), "passed": int(results["passed"].sum()),
                          "critical_failures": crit["check_id"].tolist()}
        log(f"[3/7] data quality: {int(results['passed'].sum())}/{len(results)} checks passed, "
            f"{len(crit)} critical failures")
        if len(crit) and cfg["data_quality"]["fail_pipeline_on_critical"]:
            manifest["status"] = "FAILED_DQ"
            _finish(con, manifest, t0)
            raise SystemExit("Critical data-quality failure, nothing published:\n" + crit.to_string())

        sel = forecast.run(con)
        manifest["forecast_models"] = sel[sel["selected"]].set_index("market_id")[["model", "mean_wape"]].round(4).to_dict("index")
        log(f"[4/7] forecast: mean back-test WAPE of selected models {sel[sel['selected']]['mean_wape'].mean():.1%}")

        prev_dec = (as_of - pd.DateOffset(months=as_of.month)).strftime("%Y-%m-01")
        targets.set_targets(con, plan_year, prev_dec)           # targets as they were set at start of year
        targets.set_targets(con, plan_year + 1, str(as_of.date()))  # next year's plan
        log(f"[5/7] targets set for {plan_year} (as of {prev_dec}) and {plan_year + 1} (as of {as_of:%Y-%m})")

        sc = scorecard.build(con, plan_year, str(as_of.date()))
        manifest["scorecard"] = {"reps": len(sc["rep"]), "tier_mix": sc["rep"]["tier"].value_counts().to_dict()}
        log(f"[6/7] scorecards built for {len(sc['rep'])} reps")

        out = export.publish(con, plan_year, plan_year + 1)
        manifest["outputs"] = {k: str(v) for k, v in out.items()}
        manifest["status"] = "SUCCESS"
        log(f"[7/7] published {out['excel'].name}")
    finally:
        if manifest.get("status") != "FAILED_DQ":
            _finish(con, manifest, t0)
        con.close()
    return manifest


def _finish(con, manifest: dict, t0: float) -> None:
    manifest["seconds"] = round(time.perf_counter() - t0, 2)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    (EXPORT_DIR / "run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    con.execute("CREATE SCHEMA IF NOT EXISTS ops")
    con.execute("CREATE TABLE IF NOT EXISTS ops.run_log (run_id VARCHAR, as_of_month DATE, status VARCHAR, "
                "seconds DOUBLE, manifest JSON, logged_at TIMESTAMP)")
    con.execute("INSERT INTO ops.run_log VALUES (?, ?, ?, ?, ?, now())",
                [manifest["run_id"], manifest["as_of_month"], manifest.get("status", "UNKNOWN"),
                 manifest["seconds"], json.dumps(manifest, default=str)])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="fph")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the full monthly pipeline")
    r.add_argument("--as-of", help="reporting month, e.g. 2026-10-01")
    r.add_argument("--skip-generate", action="store_true", help="reuse raw files in data/raw")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        m = run(args.as_of, args.skip_generate)
        print(f"\n{m['status']} in {m['seconds']}s  run_id={m['run_id']}")
        sys.exit(0 if m["status"] == "SUCCESS" else 1)


if __name__ == "__main__":
    main()
