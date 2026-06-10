"""What-If Outcome — buy-and-hold growth of a fixed amount in an asset or basket.

Spec: build-28th-may.md §6-7. Defaults: £100,000, end = latest available, mode =
buy-and-hold. Output: ending value, total return, annualised return, max drawdown,
best/worst month, plus an equity curve for charting.

Price fetching is dependency-injected (a callable taking ticker, start) so this
module is unit-testable without yfinance. The API endpoint wires
`BacktestService._download_close` as the fetcher.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import pandas as pd


PriceFetcher = Callable[[str, str], pd.Series]


# Asset key → yfinance ticker. Keeps the API surface stable; if the user picks
# "Gold" the dashboard maps to IAU. Mirrors the assets in backtest.ASSET_SPECS
# the dashboard already uses, per spec §6.
ASSET_TICKERS: dict[str, dict[str, str]] = {
    "GOLD":   {"ticker": "IAU",     "label": "Gold (IAU)"},
    "QQQ":    {"ticker": "QQQ",     "label": "Nasdaq (QQQ)"},
    "SMH":    {"ticker": "SMH",     "label": "Semiconductors (SMH)"},
    "SPY":    {"ticker": "SPY",     "label": "S&P 500 (SPY)"},
    "HYG":    {"ticker": "HYG",     "label": "High Yield Credit (HYG)"},
    "LQD":    {"ticker": "LQD",     "label": "IG Credit (LQD)"},
    "TLT":    {"ticker": "TLT",     "label": "Long Bonds (TLT)"},
    "BIL":    {"ticker": "BIL",     "label": "Cash / T-Bills (BIL)"},
    "BTC":    {"ticker": "BTC-USD", "label": "Bitcoin"},
    "IEF":    {"ticker": "IEF",     "label": "Medium Bonds (IEF)"},
}


# Pre-defined baskets users can pick. "FRAMEWORK_PORTFOLIO" is special-cased by
# the API to call the framework_portfolio module instead.
BASKET_PRESETS: dict[str, dict[str, Any]] = {
    "60_40":      {"label": "60/40 SPY/AGG (proxy: SPY/IEF)", "weights": {"SPY": 0.60, "IEF": 0.40}},
    "ALL_WEATHER": {"label": "All-Weather (proxy)",            "weights": {"SPY": 0.30, "TLT": 0.40, "IEF": 0.15, "GOLD": 0.075, "BIL": 0.075}},
}


@dataclass
class WhatIfResult:
    asset_label: str
    initial_amount: float
    start_date: str
    end_date: str
    mode: str
    ending_value: float
    total_return: float        # decimal (0.264 = 26.4%)
    annualised_return: float   # decimal
    max_drawdown: float        # decimal, negative or zero
    best_month: float          # decimal
    worst_month: float         # decimal
    n_months: int
    equity_curve: list[dict[str, Any]] = field(default_factory=list)  # [{date, value}]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_label": self.asset_label,
            "initial_amount": round(self.initial_amount, 2),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "mode": self.mode,
            "ending_value": round(self.ending_value, 2),
            "total_return": round(self.total_return, 4),
            "annualised_return": round(self.annualised_return, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "best_month": round(self.best_month, 4),
            "worst_month": round(self.worst_month, 4),
            "n_months": self.n_months,
            "equity_curve": self.equity_curve,
            "warnings": self.warnings,
        }


def _resolve_asset(key: str) -> tuple[str, str]:
    info = ASSET_TICKERS.get(key.upper())
    if not info:
        raise ValueError(f"Unknown asset: {key}. Known: {sorted(ASSET_TICKERS)}")
    return info["ticker"], info["label"]


def _basket_weights(basket_key: str) -> tuple[dict[str, float], str]:
    info = BASKET_PRESETS.get(basket_key.upper())
    if not info:
        raise ValueError(f"Unknown basket: {basket_key}")
    return info["weights"], info["label"]


def _build_basket_series(
    weights: dict[str, float],
    start_date: str,
    fetcher: PriceFetcher,
) -> tuple[pd.Series, list[str]]:
    """Return a synthetic basket price series with monthly rebalance to fixed weights.

    For an asset weight `w_i` and monthly return `r_i,t`, the basket return at month t is
    `Σ w_i * r_i,t`. We then compound that into an equity index starting at 100.
    """
    series_by_asset: dict[str, pd.Series] = {}
    warnings: list[str] = []
    for asset_key, weight in weights.items():
        ticker, _label = _resolve_asset(asset_key)
        try:
            prices = fetcher(ticker, start_date)
        except Exception as exc:
            warnings.append(f"Failed to fetch {ticker}: {exc}")
            continue
        if prices is None or len(prices) == 0:
            warnings.append(f"No prices returned for {ticker}")
            continue
        monthly = prices.groupby(prices.index.to_period("M")).last()
        monthly.index = monthly.index.to_timestamp("M")
        series_by_asset[asset_key] = monthly

    if not series_by_asset:
        raise ValueError("No basket components fetched successfully")

    df = pd.DataFrame(series_by_asset).sort_index()
    returns = df.pct_change()
    # Re-normalize weights against assets that actually came back
    available = {k: weights[k] for k in series_by_asset}
    total_w = sum(available.values())
    if total_w == 0:
        raise ValueError("Basket weights all zero after filtering missing components")
    norm = {k: w / total_w for k, w in available.items()}
    basket_returns = sum(returns[k] * w for k, w in norm.items())
    # Compound into an index starting at 100 on the first valid date.
    basket_returns = basket_returns.dropna()
    if basket_returns.empty:
        raise ValueError("Basket has no overlapping return history")
    index = (1.0 + basket_returns).cumprod() * 100.0
    # Prepend the starting 100 at the first usable date
    first_idx = basket_returns.index[0]
    start_anchor = pd.Series([100.0], index=[first_idx - pd.offsets.MonthEnd(1)])
    return pd.concat([start_anchor, index]).sort_index(), warnings


def _fetch_single_asset_monthly(ticker: str, start_date: str, fetcher: PriceFetcher) -> pd.Series:
    prices = fetcher(ticker, start_date)
    if prices is None or len(prices) == 0:
        raise ValueError(f"No prices returned for {ticker}")
    monthly = prices.groupby(prices.index.to_period("M")).last()
    monthly.index = monthly.index.to_timestamp("M")
    return monthly.dropna()


def _max_drawdown(equity: pd.Series) -> float:
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


def run(
    amount: float,
    asset_key: str | None,
    basket_key: str | None,
    start_date: str,
    end_date: str | None,
    fetcher: PriceFetcher,
    mode: str = "buy_and_hold",
) -> WhatIfResult:
    """Run a What-If buy-and-hold scenario.

    Provide either `asset_key` (e.g. "GOLD") or `basket_key` (e.g. "60_40").
    Dates as ISO strings. `end_date=None` → latest available data.
    """
    if (asset_key is None) == (basket_key is None):
        raise ValueError("Provide exactly one of asset_key or basket_key")

    warnings: list[str] = []

    if asset_key is not None:
        ticker, label = _resolve_asset(asset_key)
        monthly = _fetch_single_asset_monthly(ticker, start_date, fetcher)
    else:
        weights, label = _basket_weights(basket_key)
        monthly, w_warnings = _build_basket_series(weights, start_date, fetcher)
        warnings.extend(w_warnings)

    start_ts = pd.Timestamp(start_date)
    if end_date:
        end_ts = pd.Timestamp(end_date)
    else:
        end_ts = monthly.index.max()

    if start_ts < monthly.index.min():
        warnings.append(
            f"Start date {start_date} pre-dates earliest data {monthly.index.min().date().isoformat()}; "
            "using earliest available."
        )
        start_ts = monthly.index.min()
    if end_ts > monthly.index.max():
        warnings.append(
            f"End date {end_date} exceeds latest data {monthly.index.max().date().isoformat()}; using latest."
        )
        end_ts = monthly.index.max()

    # Slice and asof so partial-month dates still find a price.
    window = monthly[(monthly.index >= start_ts - pd.offsets.MonthEnd(1)) & (monthly.index <= end_ts)]
    if window.empty:
        raise ValueError(f"No price data for {label} between {start_date} and {end_date or 'today'}")

    start_price = float(window.asof(start_ts))
    end_price = float(window.asof(end_ts))
    if start_price <= 0 or end_price <= 0:
        raise ValueError("Non-positive price in the window — cannot compute return")

    # Equity curve in £-amount terms, monthly.
    equity = window / start_price * amount
    monthly_returns = window.pct_change().dropna()
    n_months = int(len(monthly_returns))

    total_return = end_price / start_price - 1.0
    days = max((end_ts - start_ts).days, 1)
    annualised = _annualised(total_return, days)
    mdd = _max_drawdown(window)
    best_m = float(monthly_returns.max()) if not monthly_returns.empty else 0.0
    worst_m = float(monthly_returns.min()) if not monthly_returns.empty else 0.0

    curve = [
        {"date": ts.date().isoformat(), "value": round(float(val), 2)}
        for ts, val in equity.items()
    ]

    return WhatIfResult(
        asset_label=label,
        initial_amount=float(amount),
        start_date=start_ts.date().isoformat(),
        end_date=end_ts.date().isoformat(),
        mode=mode,
        ending_value=float(amount) * (1.0 + total_return),
        total_return=float(total_return),
        annualised_return=float(annualised),
        max_drawdown=float(mdd),
        best_month=best_m,
        worst_month=worst_m,
        n_months=n_months,
        equity_curve=curve,
        warnings=warnings,
    )


def list_options() -> dict[str, Any]:
    """Surface the asset + basket choices for the frontend dropdown."""
    return {
        "assets": [{"key": k, "label": v["label"]} for k, v in ASSET_TICKERS.items()],
        "baskets": [{"key": k, "label": v["label"]} for k, v in BASKET_PRESETS.items()],
        "framework_portfolio_key": "FRAMEWORK_PORTFOLIO",
    }
