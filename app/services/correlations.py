from __future__ import annotations

import json
import math
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import Settings
from app.services.backtest import (
    ASSET_CLASS_PROXIES,
    ASSET_SPECS,
    BENCHMARK_SPECS,
    BacktestService,
)
from app.services.stats import (
    exponential_weights,
    tstat_from_corr,
    weighted_corr,
    weighted_mean,
)


FACTOR_KEYS = ["risk_on_off", "growth", "inflation", "short_rates", "liquidity", "dollar", "oil"]
FACTOR_LABELS = {
    "risk_on_off": "Risk On/Off",
    "growth": "Growth",
    "inflation": "Inflation",
    "short_rates": "Short Rates",
    "liquidity": "Liquidity",
    "dollar": "Dollar",
    "oil": "Oil",
}

# Bittel's pre-programmed scenarios from the transcript.
# Each value is in [-1, +1] representing factor magnitude/direction.
SCENARIO_PRESETS = {
    "spring":             {"label": "Spring",              "risk_on_off": 0.5,  "growth": 0.7,  "inflation": -0.5, "short_rates": -0.3, "liquidity": 0.5,  "dollar": -0.3, "oil": 0.0},
    "summer":             {"label": "Summer",              "risk_on_off": 0.8,  "growth": 1.0,  "inflation": 0.3,  "short_rates": 0.3,  "liquidity": 0.5,  "dollar": -0.5, "oil": 0.5},
    "fall":               {"label": "Fall",                "risk_on_off": -0.2, "growth": -0.3, "inflation": 0.7,  "short_rates": 0.5,  "liquidity": -0.2, "dollar": 0.2,  "oil": 0.7},
    "winter":             {"label": "Winter",              "risk_on_off": -0.8, "growth": -1.0, "inflation": -0.5, "short_rates": -0.5, "liquidity": -0.7, "dollar": 0.5,  "oil": -0.5},
    "dollar_wrecking_ball": {"label": "Dollar Wrecking Ball", "risk_on_off": -0.5, "growth": -0.5, "inflation": 0.0,  "short_rates": 0.5,  "liquidity": -0.7, "dollar": 1.0,  "oil": -0.3},
    "tightening_hikes":   {"label": "Tightening + Hikes",  "risk_on_off": -0.5, "growth": -0.3, "inflation": 0.5,  "short_rates": 1.0,  "liquidity": -0.7, "dollar": 0.5,  "oil": 0.0},
    "easing_cuts":        {"label": "Easing + Cuts",       "risk_on_off": 0.7,  "growth": 0.2,  "inflation": -0.3, "short_rates": -1.0, "liquidity": 0.7,  "dollar": -0.3, "oil": 0.2},
    "oil_shock":          {"label": "Oil Shock",           "risk_on_off": -0.3, "growth": -0.5, "inflation": 1.0,  "short_rates": 0.5,  "liquidity": -0.3, "dollar": 0.2,  "oil": 1.0},
    "market_melt_up":     {"label": "Market Melt-Up",      "risk_on_off": 1.0,  "growth": 0.5,  "inflation": 0.0,  "short_rates": -0.3, "liquidity": 1.0,  "dollar": -0.5, "oil": 0.3},
}

MIN_OBSERVATIONS = 24
HALF_LIFE_YEARS = 10.0


def _as_monthly_returns(prices: pd.Series) -> pd.Series:
    monthly = prices.groupby(prices.index.to_period("M")).last()
    monthly.index = monthly.index.to_timestamp("M")
    return monthly.pct_change().dropna()


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    return round(value, digits)


class CorrelationService:
    def __init__(self, settings: Settings, backtest: BacktestService) -> None:
        self.settings = settings
        self.backtest = backtest
        self.cache_path: Path = settings.runtime_dir / "correlation_matrix.json"
        self.lock = threading.Lock()
        self._refresh_thread: threading.Thread | None = None

    # --- Cache lifecycle ---

    def cache_available(self) -> bool:
        return self.cache_path.exists()

    def last_calculated(self) -> str | None:
        if not self.cache_path.exists():
            return None
        try:
            payload = json.loads(self.cache_path.read_text())
            return payload.get("last_calculated")
        except Exception:
            return None

    def is_stale(self) -> bool:
        last = self.last_calculated()
        if not last:
            return True
        try:
            last_date = date.fromisoformat(last)
        except ValueError:
            return True
        today = datetime.now().date()
        return (today - last_date).days >= 30

    def ensure_cache_async(self) -> None:
        if self.cache_available() and not self.is_stale():
            return
        with self.lock:
            if self._refresh_thread and self._refresh_thread.is_alive():
                return
            self._refresh_thread = threading.Thread(target=self.refresh_cache, daemon=True)
            self._refresh_thread.start()

    def load_cache(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {"last_calculated": None, "factors": {}, "assets": {}}
        return json.loads(self.cache_path.read_text())

    def refresh_cache(self) -> dict[str, Any]:
        payload = self._build_matrix()
        with self.lock:
            self.cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return payload

    # --- Matrix construction ---

    def _build_matrix(self) -> dict[str, Any]:
        factors = self.backtest.factor_series()
        prices = self.backtest.load_universe()

        benchmark_returns = {
            key: _as_monthly_returns(prices[key])
            for key in BENCHMARK_SPECS
            if key in prices
        }

        # Compute the return series each asset is correlated against
        asset_return_series: dict[str, pd.Series] = {}
        asset_meta: dict[str, dict[str, Any]] = {}
        for asset_key, spec in ASSET_SPECS.items():
            if spec["bucket"] in {"_internal", "_cash_proxy"}:
                continue
            if asset_key not in prices:
                continue
            absolute = _as_monthly_returns(prices[asset_key])
            benchmark_key = spec.get("benchmark")
            if benchmark_key and benchmark_key in benchmark_returns:
                relative = (absolute - benchmark_returns[benchmark_key]).dropna()
                series = relative
                basis = f"excess vs {benchmark_key}"
            else:
                series = absolute
                basis = "absolute"
            asset_return_series[asset_key] = series
            asset_meta[asset_key] = {
                "label": spec["label"],
                "bucket": spec["bucket"],
                "benchmark": benchmark_key,
                "basis": basis,
                "ticker": spec["ticker"],
            }

        # Asset-class proxies (always absolute returns)
        for class_key, info in ASSET_CLASS_PROXIES.items():
            proxy = info["proxy"]
            if proxy not in prices:
                continue
            asset_return_series[f"class:{class_key}"] = _as_monthly_returns(prices[proxy])
            asset_meta[f"class:{class_key}"] = {
                "label": info["label"],
                "bucket": "asset_class",
                "benchmark": None,
                "basis": "absolute",
                "ticker": ASSET_SPECS[proxy]["ticker"],
            }

        factors_payload: dict[str, Any] = {}
        assets_payload: dict[str, Any] = {}

        for factor_key in FACTOR_KEYS:
            factor_series = factors[factor_key]
            cells: dict[str, Any] = {}
            for asset_key, asset_returns in asset_return_series.items():
                # Skip degenerate self-correlation: risk_on_off is SPY MoM, so
                # absolute SPY / class:equities returns equal the factor by construction.
                if factor_key == "risk_on_off" and asset_key in {"SPY", "class:equities"}:
                    continue
                cell = self._compute_cell(factor_series, asset_returns)
                if cell is not None:
                    cells[asset_key] = cell
            factors_payload[factor_key] = {
                "label": FACTOR_LABELS[factor_key],
                "cells": cells,
            }

        for asset_key, meta in asset_meta.items():
            cells = {
                factor_key: factors_payload[factor_key]["cells"].get(asset_key)
                for factor_key in FACTOR_KEYS
            }
            assets_payload[asset_key] = {**meta, "cells": cells}

        return {
            "last_calculated": datetime.now().date().isoformat(),
            "training_start": "full history (exponentially weighted, half-life 10y)",
            "factor_keys": FACTOR_KEYS,
            "factors": factors_payload,
            "assets": assets_payload,
        }

    def _compute_cell(self, factor: pd.Series, asset_returns: pd.Series) -> dict[str, Any] | None:
        # Align on month-end
        factor_monthly = factor.copy()
        factor_monthly.index = factor_monthly.index.to_period("M").to_timestamp("M")
        asset_returns = asset_returns.copy()
        asset_returns.index = asset_returns.index.to_period("M").to_timestamp("M")
        aligned = pd.concat([factor_monthly, asset_returns], axis=1).dropna()
        aligned.columns = ["factor", "ret"]
        if len(aligned) < MIN_OBSERVATIONS:
            return None

        weights = exponential_weights(aligned.index, half_life_years=HALF_LIFE_YEARS)
        corr = weighted_corr(aligned["factor"], aligned["ret"], weights)
        if corr is None:
            return None
        n = len(aligned)
        t = tstat_from_corr(corr, n)

        upper = aligned["factor"].quantile(0.75)
        lower = aligned["factor"].quantile(0.25)
        bull_mask = aligned["factor"] >= upper
        bear_mask = aligned["factor"] <= lower
        bull_ret = weighted_mean(aligned.loc[bull_mask, "ret"], weights.loc[bull_mask])
        bear_ret = weighted_mean(aligned.loc[bear_mask, "ret"], weights.loc[bear_mask])

        return {
            "correlation": _round(corr, 3),
            "t_stat": _round(t, 2),
            "n": int(n),
            "bull_return": _round((bull_ret or 0.0) * 100, 2),
            "bear_return": _round((bear_ret or 0.0) * 100, 2),
        }

    # --- Scenario engine ---

    def rank_scenario(self, scenario: dict[str, float]) -> dict[str, Any]:
        cache = self.load_cache()
        if not cache.get("assets"):
            return {
                "available": False,
                "last_calculated": cache.get("last_calculated"),
                "scenario": scenario,
                "buckets": {},
                "heat_map": {},
            }

        assets = cache["assets"]
        scored: dict[str, dict[str, Any]] = {}
        for asset_key, asset in assets.items():
            cells = asset.get("cells", {})
            weighted_return = 0.0
            t_sum = 0.0
            count = 0
            for factor_key, factor_value in scenario.items():
                cell = cells.get(factor_key)
                if not cell or factor_value is None:
                    continue
                value = float(factor_value)
                if value > 0:
                    contribution = (cell.get("bull_return") or 0.0) * value
                elif value < 0:
                    contribution = (cell.get("bear_return") or 0.0) * abs(value)
                else:
                    continue
                weighted_return += contribution
                t_sum += abs(cell.get("t_stat") or 0.0)
                count += 1
            if count == 0:
                continue
            avg_return = weighted_return / count
            avg_t = t_sum / count
            composite = avg_return * avg_t
            scored[asset_key] = {
                "key": asset_key,
                "label": asset["label"],
                "bucket": asset["bucket"],
                "benchmark": asset.get("benchmark"),
                "basis": asset.get("basis"),
                "ticker": asset.get("ticker"),
                "expected_return": round(avg_return, 2),
                "avg_t_stat": round(avg_t, 2),
                "composite_score": round(composite, 2),
                "factors_used": count,
            }

        buckets: dict[str, dict[str, Any]] = {}
        for asset_key, entry in scored.items():
            bucket = entry["bucket"]
            buckets.setdefault(bucket, {"assets": []})["assets"].append(entry)

        for bucket_key, bucket in buckets.items():
            ranked = sorted(bucket["assets"], key=lambda x: x["composite_score"], reverse=True)
            by_return = sorted(bucket["assets"], key=lambda x: x["expected_return"], reverse=True)
            by_significance = sorted(bucket["assets"], key=lambda x: x["avg_t_stat"], reverse=True)
            bucket["ranked"] = ranked
            bucket["top_3"] = ranked[:3]
            bucket["bottom_3"] = ranked[-3:][::-1]
            bucket["top_by_return"] = by_return[:3]
            bucket["top_by_significance"] = by_significance[:3]

        heat_map = self._heat_map(cache)

        return {
            "available": True,
            "last_calculated": cache.get("last_calculated"),
            "scenario": scenario,
            "buckets": buckets,
            "heat_map": heat_map,
        }

    def _heat_map(self, cache: dict[str, Any]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for asset_key, asset in cache.get("assets", {}).items():
            row = {
                "key": asset_key,
                "label": asset["label"],
                "bucket": asset["bucket"],
                "benchmark": asset.get("benchmark"),
                "basis": asset.get("basis"),
            }
            for factor_key in FACTOR_KEYS:
                cell = asset.get("cells", {}).get(factor_key)
                row[factor_key] = {
                    "correlation": cell["correlation"] if cell else None,
                    "t_stat": cell["t_stat"] if cell else None,
                } if cell else None
            rows.append(row)
        return {
            "factors": [{"key": k, "label": FACTOR_LABELS[k]} for k in FACTOR_KEYS],
            "rows": rows,
        }
