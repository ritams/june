import numpy as np
import pandas as pd
import pytest

from app.services.whatif import run, list_options


def _synthetic_prices(start="2020-01-01", periods=60, drift=0.01, vol=0.02, seed=42):
    """Generate a synthetic daily price series so tests are deterministic."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start=start, periods=periods, freq="ME")  # month-end
    log_rets = rng.normal(loc=drift, scale=vol, size=periods)
    prices = 100.0 * np.exp(np.cumsum(log_rets))
    return pd.Series(prices, index=dates)


def test_single_asset_buy_and_hold_matches_simple_math():
    prices = _synthetic_prices()
    def fetcher(ticker, start):
        return prices
    res = run(
        amount=100_000.0,
        asset_key="GOLD",
        basket_key=None,
        start_date="2020-01-31",
        end_date="2024-12-31",
        fetcher=fetcher,
    )
    # Total return matches end/start - 1
    expected_total = prices.iloc[-1] / prices.iloc[0] - 1.0
    assert abs(res.total_return - expected_total) < 1e-9
    # Ending value matches amount * (1 + total)
    assert abs(res.ending_value - 100_000.0 * (1 + expected_total)) < 1e-6
    # Equity curve has the right shape
    assert res.equity_curve[0]["value"] == pytest.approx(100_000.0, abs=1.0)


def test_basket_buy_and_hold_basic():
    a = _synthetic_prices(seed=1)
    b = _synthetic_prices(seed=2, drift=0.005)

    def fetcher(ticker, start):
        return a if ticker == "SPY" else b

    res = run(
        amount=100_000.0,
        asset_key=None,
        basket_key="60_40",
        start_date="2020-01-31",
        end_date="2024-12-31",
        fetcher=fetcher,
    )
    assert res.ending_value > 0
    assert res.n_months > 0
    assert -1.0 <= res.max_drawdown <= 0.0
    assert res.worst_month <= res.best_month


def test_annualised_consistent_with_total():
    # Construct a series with exactly +21% total over ~3 years → CAGR ~6.5%
    dates = pd.date_range("2021-01-31", periods=37, freq="ME")
    prices = pd.Series(np.linspace(100.0, 121.0, 37), index=dates)
    def fetcher(_t, _s):
        return prices
    res = run(100_000.0, "GOLD", None, "2021-01-31", "2024-01-31", fetcher)
    assert res.total_return == pytest.approx(0.21, abs=0.001)
    # ~3-year CAGR for 21% total ≈ 0.0656
    assert 0.06 < res.annualised_return < 0.07


def test_max_drawdown_negative_when_drawdown_exists():
    dates = pd.date_range("2021-01-31", periods=12, freq="ME")
    # Up 50%, then down 30%, then up 10% — max DD = -30%
    vals = [100, 110, 120, 130, 140, 150, 130, 120, 105, 110, 115, 120]
    prices = pd.Series(vals, index=dates, dtype="float64")
    def fetcher(_t, _s):
        return prices
    res = run(100_000.0, "GOLD", None, "2021-01-31", None, fetcher)
    # Peak 150 → trough 105 → drawdown = (105 - 150) / 150 = -0.30
    assert res.max_drawdown == pytest.approx(-0.30, abs=0.001)


def test_invalid_dual_input_rejected():
    with pytest.raises(ValueError, match="exactly one"):
        run(100_000, "GOLD", "60_40", "2020-01-01", None, lambda *_: None)


def test_unknown_asset_raises():
    with pytest.raises(ValueError, match="Unknown asset"):
        run(100_000, "DOGECOIN", None, "2020-01-01", None, lambda *_: None)


def test_unknown_basket_raises():
    with pytest.raises(ValueError, match="Unknown basket"):
        run(100_000, None, "TURBO_LEVERAGE", "2020-01-01", None, lambda *_: None)


def test_list_options_shape():
    opts = list_options()
    assert "assets" in opts and len(opts["assets"]) > 0
    assert "baskets" in opts and len(opts["baskets"]) > 0
    assert opts["framework_portfolio_key"] == "FRAMEWORK_PORTFOLIO"


def test_warning_on_start_before_data():
    prices = _synthetic_prices(start="2022-01-01")
    def fetcher(_t, _s):
        return prices
    res = run(100_000, "GOLD", None, "2010-01-01", None, fetcher)
    assert any("pre-dates" in w for w in res.warnings)
    assert res.start_date >= "2022-01-31"


def test_ending_value_matches_curve_last_point():
    prices = _synthetic_prices()
    def fetcher(_t, _s):
        return prices
    res = run(100_000, "GOLD", None, "2020-01-31", None, fetcher)
    assert res.equity_curve[-1]["value"] == pytest.approx(res.ending_value, abs=1.0)
