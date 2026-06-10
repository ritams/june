import numpy as np
import pandas as pd
import pytest

from app.services.framework_portfolio import (
    BAND_BASKETS, FACTOR_KEYS, TICKERS, run_backtest, _basket_period_return,
)


def _flat_factor_series(level: float = 0.0, periods: int = 240, start="2005-01-31") -> dict[str, pd.Series]:
    """Each factor is a constant (post warm-up the rolling z is 0)."""
    dates = pd.date_range(start=start, periods=periods, freq="ME")
    return {k: pd.Series([level] * periods, index=dates, dtype="float64") for k in FACTOR_KEYS}


def _synthetic_prices(start="2005-01-01", periods=300, drift=0.005, vol=0.03, seed=42, level0=100.0):
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start=start, periods=periods, freq="ME")
    log_rets = rng.normal(loc=drift, scale=vol, size=periods)
    prices = level0 * np.exp(np.cumsum(log_rets))
    return pd.Series(prices, index=dates)


def _make_fetcher(per_ticker_seed: dict[str, int] | None = None):
    """Each ticker gets its own deterministic series, optionally with a custom seed."""
    seeds = per_ticker_seed or {}
    cache: dict[str, pd.Series] = {}
    def fetcher(ticker: str, start: str) -> pd.Series:
        if ticker not in cache:
            cache[ticker] = _synthetic_prices(seed=seeds.get(ticker, hash(ticker) % 1000))
        return cache[ticker]
    return fetcher


def test_band_baskets_sum_to_one():
    for stance, basket in BAND_BASKETS.items():
        total = sum(basket.values())
        assert abs(total - 1.0) < 1e-9, f"Basket {stance} sums to {total}, not 1.0"


def test_every_basket_asset_has_a_ticker():
    used = set()
    for basket in BAND_BASKETS.values():
        used.update(basket.keys())
    for asset in used:
        assert asset in TICKERS, f"No ticker for basket asset {asset}"


def test_neutral_factors_land_cautious_and_deploys_55():
    factors = _flat_factor_series(0.0)
    fetcher = _make_fetcher()
    res = run_backtest(100_000, "2015-01-31", "2020-12-31", factors, fetcher)
    assert res.n_months > 0
    # All months should be Cautious Risk-On (score = 50) → cash level = 0.45
    assert res.stance_distribution == {"Cautious Risk-On": res.n_months}
    assert abs(res.average_cash_level - 0.45) < 1e-9
    # Sanity: ending value differs from initial (random drift)
    assert res.ending_value != pytest.approx(100_000.0, abs=1.0)


def test_basket_period_return_renormalises_when_component_missing():
    basket = {"SPY": 0.5, "BTC": 0.5}
    t = pd.Timestamp("2010-01-31")
    next_t = pd.Timestamp("2010-02-28")
    prices = {
        "SPY": pd.Series([100.0, 110.0], index=[t, next_t]),
        # BTC entirely missing
    }
    ret, missing = _basket_period_return(basket, prices, t, next_t)
    assert "BTC" in missing
    # SPY contributed 100% (renormalized from 50%) → +10%
    assert abs(ret - 0.10) < 1e-9


def test_basket_period_return_zero_when_all_missing():
    ret, missing = _basket_period_return({"BTC": 1.0}, {}, pd.Timestamp("2010-01-31"), pd.Timestamp("2010-02-28"))
    assert ret == 0.0
    assert missing == ["BTC"]


def test_extreme_risk_on_factors_lock_into_full_band():
    # Liquidity/growth/risk_on_off way positive, dollar/rates/inflation/oil way negative
    dates = pd.date_range("2005-01-31", periods=240, freq="ME")
    factors: dict[str, pd.Series] = {}
    for k in FACTOR_KEYS:
        # Strong, persistent trend → z-scores will stretch beyond ±1
        if k in {"liquidity", "growth", "risk_on_off"}:
            vals = np.linspace(0.0, 100.0, 240)
        else:
            vals = np.linspace(0.0, -100.0, 240)
        factors[k] = pd.Series(vals, index=dates)

    fetcher = _make_fetcher()
    res = run_backtest(100_000, "2015-01-31", "2020-12-31", factors, fetcher)
    # Should be mostly Full Risk-On
    full_months = res.stance_distribution.get("Full Risk-On", 0)
    assert full_months / res.n_months > 0.9
    # Deployed 90%, so average cash level near 10%
    assert res.average_cash_level < 0.15


def test_extreme_risk_off_factors_lock_into_fortress_band():
    dates = pd.date_range("2005-01-31", periods=240, freq="ME")
    factors: dict[str, pd.Series] = {}
    for k in FACTOR_KEYS:
        if k in {"liquidity", "growth", "risk_on_off"}:
            vals = np.linspace(0.0, -100.0, 240)
        else:
            vals = np.linspace(0.0, 100.0, 240)
        factors[k] = pd.Series(vals, index=dates)

    fetcher = _make_fetcher()
    res = run_backtest(100_000, "2015-01-31", "2020-12-31", factors, fetcher)
    fortress = res.stance_distribution.get("Fortress Mode", 0)
    assert fortress / res.n_months > 0.9
    assert res.average_cash_level > 0.7


def test_equity_curve_endpoints_match_metrics():
    factors = _flat_factor_series(0.0)
    fetcher = _make_fetcher()
    res = run_backtest(50_000, "2015-01-31", "2020-12-31", factors, fetcher)
    assert res.equity_curve[0]["value"] == 50_000.0
    assert res.equity_curve[-1]["value"] == pytest.approx(res.ending_value, abs=1.0)
    expected_total = res.ending_value / 50_000.0 - 1.0
    assert res.total_return == pytest.approx(expected_total, abs=1e-9)


def test_raises_when_no_factor_history():
    fetcher = _make_fetcher()
    with pytest.raises(ValueError, match="No factor history"):
        run_backtest(100_000, "2015-01-31", "2020-12-31", {}, fetcher)
