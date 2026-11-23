"""Tiny dbt-style model runner on DuckDB, with a BigQuery transpile path.

Each file in src/fph/sql/<layer>/NN_name.sql is a single SELECT. The runner materialises it as
<layer>.<name> in dependency order (by filename prefix). Because models are plain
SELECTs, the same files are transpiled to BigQuery Standard SQL with sqlglot
(`python -m fph.warehouse --to-bigquery`), so the project runs locally for free and
moves to GCP without a rewrite.
"""
from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd
import sqlglot

from fph.config import DATA_DIR, ROOT, SQL_DIR, warehouse_path

LAYERS = ["staging", "marts"]
RAW_TABLES = ["markets", "territories", "reps", "merchants", "merchant_volume"]


@dataclass
class ModelRun:
    layer: str
    name: str
    rows: int
    seconds: float


def connect(path: Path | str | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path or warehouse_path()), read_only=read_only)


def load_raw(con: duckdb.DuckDBPyConnection, raw_dir: Path | None = None) -> dict[str, int]:
    raw_dir = raw_dir or DATA_DIR / "raw"
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    counts = {}
    for t in RAW_TABLES:
        f = raw_dir / f"{t}.parquet"
        con.execute(f"CREATE OR REPLACE TABLE raw.{t} AS SELECT * FROM read_parquet('{f.as_posix()}')")
        counts[t] = con.execute(f"SELECT COUNT(*) FROM raw.{t}").fetchone()[0]
    return counts


def set_run_params(con: duckdb.DuckDBPyConnection, as_of_month: str | pd.Timestamp) -> None:
    """Every model reads the reporting cut-off from here, so the whole warehouse can be
    rebuilt 'as of' any historical month (used to back-test target setting)."""
    m = pd.Timestamp(as_of_month).to_period("M").to_timestamp()
    con.execute("CREATE SCHEMA IF NOT EXISTS staging")
    con.execute(
        "CREATE OR REPLACE TABLE staging.run_params AS SELECT ?::DATE AS as_of_month, ?::DATE AS as_of_month_end",
        [m.date(), (m + pd.offsets.MonthEnd(0)).date()],
    )


def model_files() -> list[tuple[str, str, Path]]:
    out = []
    for layer in LAYERS:
        for f in sorted((SQL_DIR / layer).glob("*.sql")):
            name = re.sub(r"^\d+_", "", f.stem)
            out.append((layer, name, f))
    return out


def run_models(con: duckdb.DuckDBPyConnection) -> list[ModelRun]:
    runs = []
    for layer, name, f in model_files():
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {layer}")
        t0 = time.perf_counter()
        con.execute(f"CREATE OR REPLACE TABLE {layer}.{name} AS\n{f.read_text()}")
        rows = con.execute(f"SELECT COUNT(*) FROM {layer}.{name}").fetchone()[0]
        runs.append(ModelRun(layer, name, rows, round(time.perf_counter() - t0, 3)))
    return runs


def transpile_to_bigquery(dataset_prefix: str = "fph") -> Path:
    """Write BigQuery-dialect copies of every model to sql_bigquery/."""
    out_dir = ROOT / "sql_bigquery"
    for layer, name, f in model_files():
        sql = sqlglot.transpile(f.read_text(), read="duckdb", write="bigquery", pretty=True)[0]
        # schema names become BigQuery datasets: raw -> fph_raw, marts -> fph_marts ...
        sql = re.sub(r"\b(raw|staging|marts)\.", rf"{dataset_prefix}_\1.", sql)
        target = out_dir / layer / f"{f.stem}.sql"
        target.parent.mkdir(parents=True, exist_ok=True)
        header = f"-- Auto-generated from src/fph/sql/{layer}/{f.name} by sqlglot. Target: {dataset_prefix}_{layer}.{name}\n"
        target.write_text(header + f"CREATE OR REPLACE TABLE {dataset_prefix}_{layer}.{name} AS\n{sql};\n")
    return out_dir


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--to-bigquery", action="store_true", help="transpile models to BigQuery SQL")
    args = ap.parse_args()
    if args.to_bigquery:
        print(f"BigQuery SQL written to {transpile_to_bigquery()}")
