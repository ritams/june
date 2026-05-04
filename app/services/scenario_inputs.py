from __future__ import annotations

import math
import threading
import time
from typing import Any

from app.services.backtest import BacktestService
from app.services.correlations import FACTOR_KEYS
from app.services.stats import clip_to_unit, latest_zscore


_FACTOR_CACHE: dict[str, Any] = {"timestamp": 0.0, "stats": None}
_CACHE_TTL_SECONDS = 60 * 60 * 24  # 24h — factor stats only need refreshing daily
_LOCK = threading.Lock()
_ZSCORE_WINDOW_MONTHS = 60  # 5 years of monthly observations


def _load_factor_stats(backtest: BacktestService) -> dict[str, dict[str, float | None]]:
    """Compute the current z-score for each factor against its 5-year rolling distribution."""
    factors = backtest.factor_series()
    out: dict[str, dict[str, float | None]] = {}
    for key, series in factors.items():
        if series is None or series.empty:
            out[key] = {"current": None, "zscore": None}
            continue
        out[key] = {
            "current": float(series.iloc[-1]),
            "zscore": latest_zscore(series, _ZSCORE_WINDOW_MONTHS),
        }
    return out


def get_factor_stats(backtest: BacktestService, force: bool = False) -> dict[str, dict[str, float | None]]:
    now = time.time()
    if not force and _FACTOR_CACHE["stats"] and (now - _FACTOR_CACHE["timestamp"]) < _CACHE_TTL_SECONDS:
        return _FACTOR_CACHE["stats"]
    with _LOCK:
        if not force and _FACTOR_CACHE["stats"] and (time.time() - _FACTOR_CACHE["timestamp"]) < _CACHE_TTL_SECONDS:
            return _FACTOR_CACHE["stats"]
        stats = _load_factor_stats(backtest)
        _FACTOR_CACHE["stats"] = stats
        _FACTOR_CACHE["timestamp"] = time.time()
        return stats


def auto_fill_scenario(snapshot: dict, backtest: BacktestService) -> dict[str, float]:
    """Map the current macro state into a -1..+1 scenario dict using rolling z-scores."""
    stats = get_factor_stats(backtest)
    scenario: dict[str, float] = {}
    for factor_key in FACTOR_KEYS:
        z = stats.get(factor_key, {}).get("zscore")
        clipped = clip_to_unit(z)
        # Convention: rising rates / dollar / inflation are "+" factors. risk_on_off uses
        # SPY MoM return — already aligned (positive = risk on).
        scenario[factor_key] = 0.0 if clipped is None else clipped
    return scenario
