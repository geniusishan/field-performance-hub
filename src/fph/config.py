"""Load settings.yaml once and expose project paths."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parent
# repo root when run from a clone; current folder when installed as a package
ROOT = PKG.parents[1] if (PKG.parents[1] / "pyproject.toml").exists() else Path.cwd()
CONFIG_PATH = Path(os.environ.get("FPH_SETTINGS", PKG / "settings.yaml"))
SQL_DIR = PKG / "sql"
# env overrides let tests and CI build into a scratch folder
DATA_DIR = Path(os.environ.get("FPH_DATA_DIR", ROOT / "data"))
EXPORT_DIR = Path(os.environ.get("FPH_EXPORT_DIR", ROOT / "exports"))


@lru_cache(maxsize=1)
def settings() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    w = cfg["targets"]["allocation_weights"]
    if abs(sum(w.values()) - 1.0) > 1e-9:
        raise ValueError(f"targets.allocation_weights must sum to 1, got {sum(w.values())}")
    k = cfg["scorecard"]["kpis"]
    total = sum(v["weight"] for v in k.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"scorecard KPI weights must sum to 1, got {total}")


def warehouse_path() -> Path:
    p = Path(os.environ.get("FPH_WAREHOUSE", ROOT / settings()["warehouse"]["path"]))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
