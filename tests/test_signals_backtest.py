"""Tests for the §5 Phase 4 historical replay.

The load-bearing property is **no lookahead** — a backtest that peeks at the
future produces flattering numbers that would then be used to tune live weights.
Two tests attack that directly; the rest pin the aggregation and the honesty
guards (baseline comparison, small-sample warnings, "cannot tell" over false
confidence).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.signals import backtest as bt
from app.services.signals.bars import Bar, BarSeries


def _series(closes, symbol="TEST"):
    start = date(2024, 1, 1)
    bars = [
        Bar(bar_date=start + timedelta(days=i), open=c, high=c + 0.6, low=c - 0.6, close=c, volume=1_000_000.0)
        for i, c in enumerate(closes)
    ]
    return BarSeries(symbol=symbol, timeframe="daily", bars=bars)


def _trending(n=200, trend=0.0005):
    price, out = 100.0, []
    for i in range(n):
        price *= 1.0 + trend
        out.append(price + ((i * 7919) % 23 - 11) * 0.12)
    return out


# ── No lookahead ─────────────────────────────────────────────────────────────


def test_no_lookahead_in_replay(monkeypatch):
    """Every scored window must end at the bar being scored.

    If a future bar could reach the scorer, the whole exercise is invalid.
    Captures the length of each series handed to _evaluate and asserts it equals
    the index being scored + 1.
    """
    seen = []
    real = bt._evaluate

    def spy(series, as_of=None):
        seen.append((len(series), series.bars[-1].bar_date))
        return real(series, as_of=as_of)

    monkeypatch.setattr(bt, "_evaluate", spy)
    series = _series(_trending(200))
    bt.replay_symbol(series, horizons=(5,), step=1)

    assert seen, "nothing was scored"
    for length, last_date in seen:
        # The last bar of the scored window must be the (length-1)th bar of the
        # full series — i.e. the window is a strict prefix, never padded ahead.
        assert series.bars[length - 1].bar_date == last_date


def test_entry_is_the_next_bar_open():
    """A daily-close signal cannot be acted on at that same close.

    Entering at the signal bar's close would book a free overnight gap and
    inflate every result.
    """
    bars = _series([10.0, 10.0, 20.0, 30.0]).bars
    out = bt._forward_returns(bars, 0, (2,))
    # Entry = bars[1].open = 10.0; exit = bars[2].close = 20.0 → +100%
    assert out[2] == pytest.approx(100.0)


def test_forward_returns_drop_truncated_horizons():
    """A partial horizon would mix 3-day and 20-day outcomes into one statistic."""
    bars = _series([10.0, 11.0, 12.0]).bars
    out = bt._forward_returns(bars, 0, (2, 60))
    assert 2 in out and 60 not in out


def test_forward_returns_empty_at_the_series_end():
    bars = _series([10.0, 11.0]).bars
    assert bt._forward_returns(bars, 1, (5,)) == {}


# ── Replay mechanics ─────────────────────────────────────────────────────────


def test_replay_produces_observations():
    obs = bt.replay_symbol(_series(_trending(220)), horizons=(5, 20), step=5)
    assert obs
    for o in obs:
        assert 0 <= o.entry_score <= 100
        assert o.entry_band in ("Prime", "Strong", "Watch", "No signal")
        assert 5 in o.forward_returns


def test_replay_scores_historical_bars_as_fresh_not_stale():
    """Without as_of, every historical bar evaluates as stale against today and
    the entire replay would be flagged."""
    obs = bt.replay_symbol(_series(_trending(200)), horizons=(5,), step=10)
    assert obs, "replay produced nothing — likely every bar was rejected as stale"


def test_replay_of_a_short_series_is_empty_not_an_error():
    assert bt.replay_symbol(_series(_trending(40)), horizons=(5,)) == []


def test_step_reduces_the_observation_count():
    dense = bt.replay_symbol(_series(_trending(200)), horizons=(5,), step=1)
    sparse = bt.replay_symbol(_series(_trending(200)), horizons=(5,), step=10)
    assert len(sparse) < len(dense)


# ── Aggregation ──────────────────────────────────────────────────────────────


def _obs(band, ret, asset_class="equity", horizon=20):
    return bt.Observation(
        symbol="X", asset_class=asset_class, bar_date=date(2026, 1, 1),
        entry_score=90 if band == "Prime" else 40, entry_band=band,
        exit_score=0, counter_trend=False, regime="Bullish",
        forward_returns={horizon: ret},
    )


def test_aggregate_computes_hit_rate_and_baseline():
    obs = [_obs("Prime", 5.0), _obs("Prime", 3.0), _obs("Prime", -1.0),
           _obs("Watch", -2.0), _obs("Watch", -3.0)]
    stats = {s.band: s for s in bt.aggregate(obs, horizon=20, by_asset_class=False)}
    assert stats["Prime"].hit_rate == pytest.approx(66.67, abs=0.1)
    assert stats["Watch"].hit_rate == pytest.approx(0.0)
    # Baseline is the whole universe: 2 of 5 positive.
    assert stats["Prime"].baseline_hit_rate == pytest.approx(40.0)


def test_edge_vs_baseline_is_the_number_that_matters():
    """A 62% hit-rate means nothing if the universe returned 60% of the time."""
    obs = [_obs("Prime", 1.0), _obs("Prime", 1.0), _obs("Watch", 1.0), _obs("Watch", 1.0)]
    prime = next(s for s in bt.aggregate(obs, horizon=20, by_asset_class=False) if s.band == "Prime")
    assert prime.hit_rate == 100.0
    assert prime.edge_vs_baseline == pytest.approx(0.0)   # everything went up; no edge


def test_aggregate_splits_by_asset_class_with_its_own_baseline():
    """Crypto must be judged against crypto, not against equities."""
    obs = [_obs("Prime", 5.0, "crypto"), _obs("Prime", -5.0, "crypto"),
           _obs("Prime", 5.0, "equity"), _obs("Prime", 5.0, "equity")]
    stats = {(s.band, s.asset_class): s for s in bt.aggregate(obs, horizon=20)}
    assert stats[("Prime", "crypto")].hit_rate == pytest.approx(50.0)
    assert stats[("Prime", "equity")].hit_rate == pytest.approx(100.0)


def test_aggregate_ignores_observations_without_the_horizon():
    obs = [_obs("Prime", 5.0, horizon=5), _obs("Prime", 5.0, horizon=20)]
    stats = bt.aggregate(obs, horizon=20, by_asset_class=False)
    assert stats[0].count == 1


def test_aggregate_of_nothing_is_empty():
    assert bt.aggregate([], horizon=20) == []


def test_median_handles_even_and_odd_lengths():
    assert bt._median([1.0, 3.0]) == pytest.approx(2.0)
    assert bt._median([1.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert bt._median([]) == 0.0


# ── Honesty guards ───────────────────────────────────────────────────────────


def test_monotonicity_is_none_when_the_sample_is_too_thin():
    """"We cannot tell" beats a confident True from 3 observations."""
    obs = [_obs("Prime", 1.0), _obs("Watch", -1.0)]
    assert bt._is_monotonic(bt.aggregate(obs, horizon=20, by_asset_class=False)) is None


def test_monotonicity_detects_a_working_score():
    obs = ([_obs("Prime", 1.0)] * 35) + ([_obs("Watch", -1.0)] * 35)
    stats = bt.aggregate(obs, horizon=20, by_asset_class=False)
    assert bt._is_monotonic(stats) is True


def test_monotonicity_detects_an_inverted_score():
    """If Watch outperforms Prime, the weights are wrong — that is Phase 4's point."""
    obs = ([_obs("Prime", -1.0)] * 35) + ([_obs("Watch", 1.0)] * 35)
    stats = bt.aggregate(obs, horizon=20, by_asset_class=False)
    assert bt._is_monotonic(stats) is False


def test_run_on_an_empty_watchlist_reports_the_reason(monkeypatch):
    monkeypatch.setattr(bt.store, "list_watchlist", lambda: [])
    assert "error" in bt.run()


def test_run_reports_its_conventions(monkeypatch):
    """The two open §5 decisions must travel with the numbers, not be assumed."""
    class Fake:
        name = "fake"
        def daily_bars(self, symbol, bars=300): return _series(_trending(200), symbol)
        def search(self, q, limit=10): return []

    monkeypatch.setattr(bt, "get_provider", lambda name=None: Fake())
    result = bt.run(["AAA"], horizons=(5,), horizon=5, step=20)
    assert result["entry_convention"] == "next bar open → horizon close"
    assert "unconditional rate" in result["hit_definition"]
    assert result["observations"] > 0


def test_run_flags_small_samples(monkeypatch):
    class Fake:
        name = "fake"
        def daily_bars(self, symbol, bars=300): return _series(_trending(200), symbol)
        def search(self, q, limit=10): return []

    monkeypatch.setattr(bt, "get_provider", lambda name=None: Fake())
    result = bt.run(["AAA"], horizons=(5,), horizon=5, step=30)
    assert result["sample_warnings"], "a handful of observations must be flagged"


def test_run_isolates_a_failing_symbol(monkeypatch):
    from app.services.signals.providers import ProviderError

    class Fake:
        name = "fake"
        def daily_bars(self, symbol, bars=300):
            if symbol == "BAD":
                raise ProviderError("down")
            return _series(_trending(200), symbol)
        def search(self, q, limit=10): return []

    monkeypatch.setattr(bt, "get_provider", lambda name=None: Fake())
    result = bt.run(["AAA", "BAD"], horizons=(5,), horizon=5, step=20)
    assert "BAD" in result["failed"]
    assert result["observations"] > 0
