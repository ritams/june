from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd


def rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    series = series.astype("float64").sort_index()
    mean = series.rolling(window=window, min_periods=max(2, window // 4)).mean()
    std = series.rolling(window=window, min_periods=max(2, window // 4)).std()
    return (series - mean) / std.replace(0.0, np.nan)


def latest_zscore(series: pd.Series, window: int) -> float | None:
    z = rolling_zscore(series, window).dropna()
    if z.empty:
        return None
    value = float(z.iloc[-1])
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def clip_to_unit(value: float | None) -> float | None:
    if value is None or math.isnan(value) or math.isinf(value):
        return None
    return max(-1.0, min(1.0, value))


_TSTAT_CAP = 10.0


def tstat_from_corr(corr: float, n: int) -> float | None:
    if n is None or n < 3:
        return None
    if corr is None or math.isnan(corr):
        return None
    bounded = max(-0.999, min(0.999, corr))
    denom = math.sqrt(max(1.0 - bounded * bounded, 1e-6))
    raw = bounded * math.sqrt(n - 2) / denom
    return max(-_TSTAT_CAP, min(_TSTAT_CAP, raw))


def tstat_from_returns(returns: Iterable[float]) -> float | None:
    arr = np.asarray([r for r in returns if r is not None and not math.isnan(r)], dtype="float64")
    if arr.size < 3:
        return None
    std = arr.std(ddof=1)
    if std == 0:
        return None
    return float(arr.mean() / (std / math.sqrt(arr.size)))


def exponential_weights(index: pd.DatetimeIndex, half_life_years: float = 10.0) -> pd.Series:
    if len(index) == 0:
        return pd.Series(dtype="float64")
    most_recent = index.max()
    age_years = (most_recent - index).days / 365.25
    decay = math.log(2.0) / max(half_life_years, 0.25)
    weights = np.exp(-decay * age_years)
    return pd.Series(weights, index=index)


def weighted_mean(values: pd.Series, weights: pd.Series) -> float | None:
    aligned = pd.concat([values, weights], axis=1).dropna()
    if aligned.empty:
        return None
    v = aligned.iloc[:, 0].to_numpy()
    w = aligned.iloc[:, 1].to_numpy()
    total_weight = w.sum()
    if total_weight == 0:
        return None
    return float((v * w).sum() / total_weight)


def weighted_corr(x: pd.Series, y: pd.Series, weights: pd.Series) -> float | None:
    aligned = pd.concat([x, y, weights], axis=1).dropna()
    if len(aligned) < 3:
        return None
    a = aligned.iloc[:, 0].to_numpy()
    b = aligned.iloc[:, 1].to_numpy()
    w = aligned.iloc[:, 2].to_numpy()
    w_sum = w.sum()
    if w_sum == 0:
        return None
    a_mean = (a * w).sum() / w_sum
    b_mean = (b * w).sum() / w_sum
    cov = ((a - a_mean) * (b - b_mean) * w).sum() / w_sum
    a_var = ((a - a_mean) ** 2 * w).sum() / w_sum
    b_var = ((b - b_mean) ** 2 * w).sum() / w_sum
    denom = math.sqrt(a_var * b_var)
    if denom == 0:
        return None
    value = float(cov / denom)
    if math.isnan(value) or math.isinf(value):
        return None
    return value
