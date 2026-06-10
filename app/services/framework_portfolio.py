"""Framework Portfolio backtest — "if you had followed this dashboard's allocation."

Per build-28th-may.md §8, this is the answer to:
    "If I had followed this dashboard, would it have protected capital and
     compounded well?"

Algorithm (see docs/devlog-hermes-build.md §3 for full design):

  For each month-end `t` in [start_date, end_date):
    1. Compute the Risk Budget at t using factor z-scores AS OF t
       (rolling 60-month window — no forward leakage).
    2. Map score → stance band → deployed % and basket weights.
    3. r_t = deploy% * Σ(w_i * r_i,t→t+1) + cash% * r_BIL,t→t+1
    4. E_{t+1} = E_t * (1 + r_t)

Cash return uses BIL (1-3mo T-Bills) for periods where BIL exists; zero return
before BIL inception (2007-06). Basket components missing for a given month
(e.g. BTC pre-2014) are dropped and remaining weights renormalized for that
month — documented in the devlog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from app.services.risk_budget import band_for_score, compute as compute_rb
from app.services.stats import clip_to_unit, rolling_zscore


PriceFetcher = Callable[[str, str], pd.Series]


FACTOR_KEYS = ["risk_on_off", "growth", "inflation", "short_rates", "liquidity", "dollar", "oil"]


# Stance → basket weights for the deployed portion. Cash is handled separately.
# Weights sum to 1.0 within each stance.
#
# Design rationale (see docs/devlog-hermes-build.md §3.3):
# - Fortress / Defensive lean on Bittel "Winter / Fall" allocations: gold + bonds
#   + a touch of SPY for tail-risk protection that still earns in mild risk-on prints.
# - Cautious / Constructive / Full ride the "Spring → Summer" tilt: SPY/QQQ core
#   + commodity hedge (IAU) + BTC for the long-horizon debasement leg.
# - SMH only at Full Risk-On (5%) because semis are the highest-beta cyclical
#   the dashboard tracks; appropriate only when liquidity + cycle + risk appetite
#   all confirm.
# These weights are deliberately fixed (not optimised) so the backtest answers
# the literal spec §8 question — "what would following THIS dashboard have done?"
# — without introducing a second optimisation that could overfit history.
BAND_BASKETS: dict[str, dict[str, float]] = {
    "Fortress Mode":         {"IAU": 0.60, "IEF": 0.40},
    "Defensive":             {"IAU": 0.30, "IEF": 0.30, "SPY": 0.40},
    "Cautious Risk-On":      {"SPY": 0.50, "QQQ": 0.30, "IAU": 0.20},
    "Constructive Risk-On":  {"QQQ": 0.40, "SPY": 0.30, "BTC": 0.15, "IAU": 0.15},
    "Full Risk-On":          {"QQQ": 0.50, "SPY": 0.25, "BTC": 0.20, "SMH": 0.05},
}

# Ticker lookup for the basket components. Mirrors backtest.ASSET_SPECS.
TICKERS: dict[str, str] = {
    "IAU": "IAU",
    "IEF": "IEF",
    "SPY": "SPY",
    "QQQ": "QQQ",
    "BTC": "BTC-USD",
    "SMH": "SMH",
    "BIL": "BIL",
}

Z_WINDOW_MONTHS = 60   # 5y rolling window, matches PhaseService + scenario_inputs


@dataclass
class FrameworkPortfolioResult:
    initial_amount: float
    start_date: str
    end_date: str
    ending_value: float
    total_return: float          # decimal
    annualised_return: float     # decimal (CAGR)
    max_drawdown: float          # decimal, negative or zero
    average_cash_level: float    # decimal (0..1)
    best_12m: float              # decimal
    worst_12m: float             # decimal
    n_months: int
    equity_curve: list[dict[str, Any]] = field(default_factory=list)   # [{date, value, stance, score, cash}]
    stance_distribution: dict[str, int] = field(default_factory=dict)  # stance → months count
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_amount": round(self.initial_amount, 2),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "ending_value": round(self.ending_value, 2),
            "total_return": round(self.total_return, 4),
            "annualised_return": round(self.annualised_return, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "average_cash_level": round(self.average_cash_level, 4),
            "best_12m": round(self.best_12m, 4),
            "worst_12m": round(self.worst_12m, 4),
            "n_months": self.n_months,
            "equity_curve": self.equity_curve,
            "stance_distribution": self.stance_distribution,
            "warnings": self.warnings,
        }


def _to_month_end(series: pd.Series) -> pd.Series:
    monthly = series.groupby(series.index.to_period("M")).last()
    monthly.index = monthly.index.to_timestamp("M")
    return monthly.dropna().sort_index()


def _compute_z_panels(factor_series: dict[str, pd.Series]) -> pd.DataFrame:
    """Apply rolling z-score (60-month window) to each factor; align on month-end."""
    panels: dict[str, pd.Series] = {}
    for key in FACTOR_KEYS:
        series = factor_series.get(key)
        if series is None or series.empty:
            continue
        monthly = _to_month_end(series)
        z = rolling_zscore(monthly, Z_WINDOW_MONTHS)
        panels[key] = z
    return pd.DataFrame(panels)


def _fetch_monthly_prices(price_fetcher: PriceFetcher, start_date: str) -> dict[str, pd.Series]:
    """Fetch every ticker in BAND_BASKETS plus BIL, resampled to month-end."""
    out: dict[str, pd.Series] = {}
    tickers = set()
    for basket in BAND_BASKETS.values():
        tickers.update(basket.keys())
    tickers.add("BIL")
    for asset in tickers:
        ticker = TICKERS[asset]
        try:
            prices = price_fetcher(ticker, start_date)
        except Exception:
            continue
        if prices is None or len(prices) == 0:
            continue
        out[asset] = _to_month_end(prices)
    return out


def _scenario_at(z_panel: pd.DataFrame, ts: pd.Timestamp) -> dict[str, float | None]:
    """Pull the z-score for every factor at month-end `ts`, clipped to [-1, +1]."""
    if ts not in z_panel.index:
        # asof fallback — z_panel is dense at month-end so this is rare.
        idx = z_panel.index[z_panel.index <= ts]
        if len(idx) == 0:
            return {k: None for k in FACTOR_KEYS}
        ts = idx[-1]
    row = z_panel.loc[ts]
    return {k: clip_to_unit(float(row[k])) if k in z_panel.columns and pd.notna(row[k]) else None for k in FACTOR_KEYS}


def _basket_period_return(
    basket: dict[str, float],
    prices: dict[str, pd.Series],
    t: pd.Timestamp,
    next_t: pd.Timestamp,
) -> tuple[float, list[str]]:
    """Compute the basket's return from t to next_t. Components missing prices
    are dropped and remaining weights renormalized."""
    available: dict[str, tuple[float, float, float]] = {}  # asset → (weight, price_t, price_next)
    missing: list[str] = []
    for asset, w in basket.items():
        series = prices.get(asset)
        if series is None or series.empty:
            missing.append(asset)
            continue
        # asof so partial-month dates still find a price.
        p_t = series.asof(t)
        p_next = series.asof(next_t)
        if pd.isna(p_t) or pd.isna(p_next) or p_t <= 0:
            missing.append(asset)
            continue
        available[asset] = (w, float(p_t), float(p_next))

    if not available:
        return 0.0, missing

    total_w = sum(w for w, _, _ in available.values())
    if total_w == 0:
        return 0.0, missing
    period_return = 0.0
    for asset, (w, p_t, p_next) in available.items():
        r = p_next / p_t - 1.0
        period_return += (w / total_w) * r
    return period_return, missing


def _cash_period_return(prices: dict[str, pd.Series], t: pd.Timestamp, next_t: pd.Timestamp) -> float:
    """Cash leg → BIL when available, 0% otherwise (BIL inception 2007-06)."""
    series = prices.get("BIL")
    if series is None or series.empty:
        return 0.0
    p_t = series.asof(t)
    p_next = series.asof(next_t)
    if pd.isna(p_t) or pd.isna(p_next) or p_t <= 0:
        return 0.0
    return float(p_next / p_t - 1.0)


def _drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peaks = equity.cummax()
    dd = equity / peaks - 1.0
    return float(dd.min())


def _annualised(total_return: float, days: int) -> float:
    if days <= 0:
        return 0.0
    years = days / 365.25
    if years <= 0:
        return 0.0
    return float((1.0 + total_return) ** (1.0 / years) - 1.0)


def _rolling_window_extreme(returns: pd.Series, window: int, op: str) -> float:
    if len(returns) < window:
        return 0.0
    rolling_total = (1.0 + returns).rolling(window=window).apply(lambda r: r.prod() - 1.0, raw=True).dropna()
    if rolling_total.empty:
        return 0.0
    return float(rolling_total.max() if op == "max" else rolling_total.min())


def run_backtest(
    initial_amount: float,
    start_date: str,
    end_date: str | None,
    factor_series: dict[str, pd.Series],
    price_fetcher: PriceFetcher,
) -> FrameworkPortfolioResult:
    z_panel = _compute_z_panels(factor_series)
    if z_panel.empty:
        raise ValueError("No factor history available")
    # Note: we intentionally do NOT dropna here. A factor with zero variance
    # produces all-NaN z-scores; _scenario_at converts NaN → None, and the
    # Risk Budget treats None as a neutral 0. Keeping the dense month-end
    # index ensures the iteration grid is complete.

    prices = _fetch_monthly_prices(price_fetcher, start_date)
    if not prices:
        raise ValueError("No price data fetched for any basket component")

    warnings: list[str] = []

    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date) if end_date else max(
        max((s.index.max() for s in prices.values()), default=pd.Timestamp.today()),
        z_panel.index.max(),
    )

    # Build the month-end iteration grid. Use z_panel index intersected with price coverage.
    grid = z_panel.index[(z_panel.index >= start_ts) & (z_panel.index <= end_ts)]
    if len(grid) < 2:
        raise ValueError(f"Not enough months between {start_date} and {end_date or 'today'}")

    equity = float(initial_amount)
    curve: list[dict[str, Any]] = [{"date": grid[0].date().isoformat(), "value": round(equity, 2),
                                    "stance": None, "score": None, "cash": None}]
    monthly_returns: list[float] = []
    cash_levels: list[float] = []
    stance_counts: dict[str, int] = {}

    for i in range(len(grid) - 1):
        t = grid[i]
        next_t = grid[i + 1]
        scenario = _scenario_at(z_panel, t)
        rb = compute_rb(scenario)
        band = band_for_score(rb.score)
        basket = BAND_BASKETS[band["stance"]]

        basket_ret, missing = _basket_period_return(basket, prices, t, next_t)
        cash_ret = _cash_period_return(prices, t, next_t)

        deploy_w = band["deploy_pct"] / 100.0
        cash_w = band["cash_pct"] / 100.0
        period_return = deploy_w * basket_ret + cash_w * cash_ret
        equity *= (1.0 + period_return)
        monthly_returns.append(period_return)
        cash_levels.append(cash_w)
        stance_counts[band["stance"]] = stance_counts.get(band["stance"], 0) + 1

        if missing:
            warnings.append(f"{t.date()}: dropped missing basket assets {missing} (renormalised remaining weights)")

        curve.append({
            "date": next_t.date().isoformat(),
            "value": round(equity, 2),
            "stance": band["stance"],
            "score": band["score"],
            "cash": round(cash_w, 3),
        })

    total_return = equity / float(initial_amount) - 1.0
    days = max((grid[-1] - grid[0]).days, 1)
    annualised = _annualised(total_return, days)
    equity_series = pd.Series([row["value"] for row in curve],
                              index=[pd.Timestamp(row["date"]) for row in curve])
    mdd = _drawdown(equity_series)
    avg_cash = sum(cash_levels) / len(cash_levels) if cash_levels else 0.0
    returns_series = pd.Series(monthly_returns)
    best12 = _rolling_window_extreme(returns_series, 12, "max")
    worst12 = _rolling_window_extreme(returns_series, 12, "min")

    # Cap noisy duplicate warnings (keep first 5 + count)
    if len(warnings) > 5:
        extra = len(warnings) - 5
        warnings = warnings[:5] + [f"...+{extra} more month(s) with dropped basket components"]

    return FrameworkPortfolioResult(
        initial_amount=float(initial_amount),
        start_date=grid[0].date().isoformat(),
        end_date=grid[-1].date().isoformat(),
        ending_value=equity,
        total_return=total_return,
        annualised_return=annualised,
        max_drawdown=mdd,
        average_cash_level=avg_cash,
        best_12m=best12,
        worst_12m=worst12,
        n_months=len(monthly_returns),
        equity_curve=curve,
        stance_distribution=stance_counts,
        warnings=warnings,
    )
