"""Back-tested signings forecast per market.

For each market, three candidate models compete in a rolling-origin back-test.
The model with the lowest WAPE on held-out months wins that market. Prediction
intervals come from the winner's own back-test errors (empirical, not assumed normal),
so the bands show how wrong the model has actually been before.

Candidates:
  * seasonal_naive_growth - last year's same month x trailing YoY growth (strong baseline)
  * holt_winters          - damped additive trend, multiplicative seasonality
  * trend_seasonal_ols    - linear trend + month-of-year effects
"""
from __future__ import annotations

import warnings

import duckdb
import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from fph.config import settings

warnings.filterwarnings("ignore")


def _snaive_growth(y: pd.Series, h: int) -> np.ndarray:
    growth = y[-12:].sum() / max(y[-24:-12].sum(), 1) if len(y) >= 24 else 1.0
    growth = float(np.clip(growth, 0.7, 1.5))
    last_year = y[-12:].to_numpy()
    return np.array([last_year[i % 12] * growth ** (1 + i // 12) for i in range(h)])


def _holt_winters(y: pd.Series, h: int) -> np.ndarray:
    model = ExponentialSmoothing(y.clip(lower=1), trend="add", damped_trend=True,
                                 seasonal="mul", seasonal_periods=12,
                                 initialization_method="estimated").fit()
    return np.asarray(model.forecast(h))


def _trend_seasonal_ols(y: pd.Series, h: int) -> np.ndarray:
    n = len(y)
    t = np.arange(n + h)
    months = (pd.DatetimeIndex(y.index)[0].month - 1 + t) % 12
    X = np.column_stack([np.ones(n + h), t] + [(months == k).astype(float) for k in range(1, 12)])
    fit = sm.OLS(y.to_numpy(), X[:n]).fit()
    return fit.predict(X[n:])


MODELS = {
    "seasonal_naive_growth": _snaive_growth,
    "holt_winters": _holt_winters,
    "trend_seasonal_ols": _trend_seasonal_ols,
}


def wape(actual: np.ndarray, pred: np.ndarray) -> float:
    return float(np.abs(actual - pred).sum() / max(np.abs(actual).sum(), 1e-9))


def backtest(y: pd.Series, origins: int, horizon: int) -> pd.DataFrame:
    rows = []
    for k in range(origins, 0, -1):
        cut = len(y) - k * horizon
        train, test = y.iloc[:cut], y.iloc[cut:cut + horizon]
        if len(train) < 24 or test.empty:
            continue
        for name, fn in MODELS.items():
            pred = np.maximum(fn(train, len(test)), 0)
            rows.append({"model": name, "origin": train.index[-1], "wape": wape(test.to_numpy(), pred),
                         "ratios": list(test.to_numpy() / np.maximum(pred, 1e-9))})
    return pd.DataFrame(rows)


def forecast_market(y: pd.Series, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    bt = backtest(y, cfg["backtest_origins"], cfg["backtest_horizon"])
    score = bt.groupby("model")["wape"].mean().sort_values()
    best = score.index[0]
    ratios = np.concatenate(bt.loc[bt["model"] == best, "ratios"].to_list())
    a = (1 - cfg["interval_level"]) / 2
    lo_r, hi_r = np.quantile(ratios, [a, 1 - a])
    h = cfg["horizon_months"]
    point = np.maximum(MODELS[best](y, h), 0)
    widen = np.sqrt(np.arange(1, h + 1) / cfg["backtest_horizon"]).clip(min=1)  # uncertainty grows with horizon
    idx = pd.date_range(y.index[-1] + pd.offsets.MonthBegin(1), periods=h, freq="MS")
    fc = pd.DataFrame({
        "month": idx, "forecast": point,
        "lower": point * (1 - (1 - lo_r) * widen).clip(min=0),
        "upper": point * (1 + (hi_r - 1) * widen),
        "model": best,
    })
    sel = score.rename("mean_wape").reset_index()
    sel["selected"] = sel["model"] == best
    return fc, sel


def run(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    cfg = settings()["forecast"]
    mm = con.execute("""
        SELECT market_id, market_name, month, signings
        FROM marts.market_monthly
        WHERE month <= (SELECT as_of_month FROM staging.run_params)
        ORDER BY market_id, month
    """).df()
    fcs, sels = [], []
    for (mid, mname), g in mm.groupby(["market_id", "market_name"]):
        y = g.set_index(pd.DatetimeIndex(g["month"]))["signings"].astype(float).asfreq("MS").fillna(0)
        fc, sel = forecast_market(y, cfg)
        fcs.append(fc.assign(market_id=mid, market_name=mname))
        sels.append(sel.assign(market_id=mid, market_name=mname))
    forecast = pd.concat(fcs, ignore_index=True)
    selection = pd.concat(sels, ignore_index=True)
    forecast["month"] = forecast["month"].dt.date
    con.execute("CREATE SCHEMA IF NOT EXISTS forecast")
    con.execute("CREATE OR REPLACE TABLE forecast.signings AS SELECT * FROM forecast")
    con.execute("CREATE OR REPLACE TABLE forecast.model_selection AS SELECT * FROM selection")
    return selection
