"""Tests for the native indicator stack (scope doc §4).

Two things these tests exist to protect:

1. **Correctness against known values.** SMA/EMA/RSI/ATR are standard, so where
   possible they are pinned to hand-computable results rather than to whatever
   the implementation happens to emit.

2. **The warmup contract.** Every indicator returns None where it is not yet
   computable, and None must never silently become 0 or "neutral". The
   2026-06-21 audit found that exact bug elsewhere in this codebase; these
   tests are the guard against reintroducing it here.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.signals.bars import Bar, BarSeries
from app.services.signals.indicators import (
    REGIME_BEARISH,
    REGIME_BULLISH,
    REGIME_NEUTRAL,
    atr,
    atr_distance,
    bearish_divergence,
    bullish_divergence,
    ema,
    regime_series,
    roc,
    rolling_zscore,
    rsi,
    rvol,
    sma,
    snapshot,
    true_range,
)


def _series(closes, *, highs=None, lows=None, volumes=None, symbol="TEST", timeframe="daily") -> BarSeries:
    start = date(2020, 1, 1)
    bars = []
    for i, c in enumerate(closes):
        bars.append(
            Bar(
                bar_date=start + timedelta(days=i),
                open=c,
                high=highs[i] if highs else c + 0.5,
                low=lows[i] if lows else c - 0.5,
                close=c,
                volume=volumes[i] if volumes is not None else 1000.0,
            )
        )
    return BarSeries(symbol=symbol, timeframe=timeframe, bars=bars)


# ── SMA ──────────────────────────────────────────────────────────────────────


def test_sma_warmup_is_none_then_correct():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = sma(values, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)     # (1+2+3)/3
    assert out[3] == pytest.approx(3.0)     # (2+3+4)/3
    assert out[4] == pytest.approx(4.0)     # (3+4+5)/3


def test_sma_running_sum_matches_naive_recomputation():
    """The O(n) running-sum optimisation must equal the O(n·p) definition."""
    values = [float((i * 37) % 91) for i in range(200)]
    period = 20
    fast = sma(values, period)
    for i in range(period - 1, len(values)):
        naive = sum(values[i - period + 1 : i + 1]) / period
        assert fast[i] == pytest.approx(naive)


def test_sma_rejects_nonpositive_period():
    with pytest.raises(ValueError):
        sma([1.0, 2.0], 0)


# ── EMA ──────────────────────────────────────────────────────────────────────


def test_ema_seeds_with_sma_of_first_period():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = ema(values, 3)
    assert out[1] is None
    assert out[2] == pytest.approx(2.0)     # seed = SMA(1,2,3)
    # multiplier = 2/(3+1) = 0.5 → (4 - 2)*0.5 + 2 = 3.0
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_ema_shorter_than_period_is_all_none():
    assert ema([1.0, 2.0], 5) == [None, None]


def test_ema_of_constant_series_equals_the_constant():
    out = ema([7.0] * 50, 21)
    assert out[-1] == pytest.approx(7.0)


# ── RSI ──────────────────────────────────────────────────────────────────────


def test_rsi_all_gains_is_100():
    out = rsi([float(i) for i in range(1, 40)], 14)
    assert out[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    out = rsi([float(i) for i in range(40, 1, -1)], 14)
    assert out[-1] == pytest.approx(0.0)


def test_rsi_flat_series_is_neutral_not_100():
    """Zero gains AND zero losses is flat — 50, not the all-gains 100.

    Guards the divide-by-zero branch in _rsi_from_averages. Returning 100 here
    would make a halted instrument look maximally overbought.
    """
    out = rsi([10.0] * 40, 14)
    assert out[-1] == pytest.approx(50.0)


def test_rsi_warmup_region_is_none():
    out = rsi([float(i) for i in range(30)], 14)
    assert all(v is None for v in out[:14])
    assert out[14] is not None


def test_rsi_stays_within_bounds_on_noisy_input():
    values = [50.0 + ((i * 17) % 13) - 6 for i in range(200)]
    out = rsi(values, 14)
    for v in out:
        assert v is None or 0.0 <= v <= 100.0


# ── ATR / True Range ─────────────────────────────────────────────────────────


def test_true_range_uses_the_widest_of_the_three_measures():
    highs = [10.0, 12.0]
    lows = [9.0, 11.5]
    closes = [9.5, 11.8]
    tr = true_range(highs, lows, closes)
    assert tr[0] is None                    # no previous close
    # candidates: 12-11.5=0.5 | |12-9.5|=2.5 | |11.5-9.5|=2.0 → 2.5
    assert tr[1] == pytest.approx(2.5)


def test_atr_warmup_then_positive():
    closes = [float(10 + (i % 5)) for i in range(40)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    out = atr(highs, lows, closes, 14)
    assert all(v is None for v in out[:14])
    assert out[14] is not None and out[14] > 0


def test_atr_too_short_is_all_none():
    out = atr([1.0, 2.0], [0.5, 1.5], [0.8, 1.8], 14)
    assert all(v is None for v in out)


# ── Z-score and ATR stretch ──────────────────────────────────────────────────


def test_zscore_of_flat_window_is_none_not_zero():
    """Zero variance is unmeasurable, not "no deviation" — see the warmup contract."""
    out = rolling_zscore([5.0] * 60, 50)
    assert out[-1] is None


def test_zscore_sign_and_magnitude():
    # 49 bars at 10, then one at 20. Mean ≈ 10.2, so the last bar is a large
    # positive z. Exact value is less interesting than the sign and scale.
    values = [10.0] * 49 + [20.0]
    out = rolling_zscore(values, 50)
    assert out[-1] is not None
    assert out[-1] > 5.0


def test_atr_stretch_is_positive_above_the_anchor():
    closes = [110.0]
    anchor = [100.0]
    atr_values = [5.0]
    out = atr_distance(closes, anchor, atr_values)
    assert out[0] == pytest.approx(2.0)     # 10 above, ATR 5 → +2 ATRs


def test_atr_stretch_is_negative_below_the_anchor():
    out = atr_distance([87.5], [100.0], [5.0])
    assert out[0] == pytest.approx(-2.5)    # §5's "2.5 ATR below 21 EMA" trigger


def test_atr_stretch_none_when_atr_unknown_or_zero():
    assert atr_distance([100.0], [100.0], [None])[0] is None
    assert atr_distance([100.0], [100.0], [0.0])[0] is None


# ── ROC and RVOL ─────────────────────────────────────────────────────────────


def test_roc_percent_change():
    values = [100.0] * 20 + [110.0]
    out = roc(values, 20)
    assert out[20] == pytest.approx(10.0)


def test_roc_guards_zero_base():
    values = [0.0] + [1.0] * 25
    out = roc(values, 20)
    assert out[20] is None


def test_rvol_ratio_against_trailing_average():
    volumes = [100.0] * 20 + [250.0]
    out = rvol(volumes, 20)
    assert out[20] == pytest.approx(2.5)


def test_rvol_is_none_when_any_window_volume_is_missing():
    """Missing volume must not be treated as zero — it would fake a capitulation.

    §5 scores "RVOL >= 2 on signal day: 5". Substituting 0 for the unknown days
    would halve the average and manufacture that signal.
    """
    volumes = [100.0] * 10 + [None] + [100.0] * 9 + [150.0]
    out = rvol(volumes, 20)
    assert out[20] is None


def test_rvol_is_none_when_current_volume_missing():
    volumes = [100.0] * 20 + [None]
    assert rvol(volumes, 20)[20] is None


# ── Divergence ───────────────────────────────────────────────────────────────


# Pivot confirmation needs 2 bars either side, so a pivot can never sit in the
# first or last two indices of the series. These fixtures place both pivots at
# indices 3 and 9 of a 13-bar window accordingly.
_DIV_LOWS = [12, 11, 10, 8, 10, 11, 12, 11, 10, 7, 10, 11, 12]     # pivots at 3 (=8) and 9 (=7)
_DIV_HIGHS = [8, 9, 10, 12, 10, 9, 8, 9, 10, 14, 10, 9, 8]         # pivots at 3 (=12) and 9 (=14)


def test_bullish_divergence_detects_lower_low_with_higher_rsi_low():
    # Price pivot 8 → 7 (lower low); RSI pivot 30 → 35 (higher low) = divergence.
    rsi_vals = [55, 50, 45, 30, 45, 50, 55, 50, 45, 35, 45, 50, 55]
    assert bullish_divergence([float(v) for v in _DIV_LOWS], [float(v) for v in rsi_vals]) is True


def test_bullish_divergence_false_when_rsi_also_makes_a_lower_low():
    rsi_vals = [55, 50, 45, 30, 45, 50, 55, 50, 45, 25, 45, 50, 55]   # RSI lower too
    assert bullish_divergence([float(v) for v in _DIV_LOWS], [float(v) for v in rsi_vals]) is False


def test_bearish_divergence_detects_higher_high_with_lower_rsi_high():
    # Price pivot 12 → 14 (higher high); RSI pivot 70 → 65 (lower high) = divergence.
    rsi_vals = [45, 50, 55, 70, 55, 50, 45, 50, 55, 65, 55, 50, 45]
    assert bearish_divergence([float(v) for v in _DIV_HIGHS], [float(v) for v in rsi_vals]) is True


def test_divergence_on_short_series_is_false_not_error():
    assert bullish_divergence([1.0, 2.0], [50.0, 50.0]) is False
    assert bearish_divergence([1.0, 2.0], [50.0, 50.0]) is False


# ── Regime Composite (§4) ────────────────────────────────────────────────────


def test_regime_is_none_during_warmup():
    """A 200 SMA needs 200 bars — before that the regime is unknown, not Neutral.

    This is the single most important warmup test in the file: §5 pays up to 25
    points for a regime flip, and a warmup region that reported "Neutral" would
    manufacture a Neutral→Bullish flip on bar 200 of every symbol.
    """
    closes = [100.0 + i * 0.1 for i in range(150)]   # fewer than 200 bars
    states = regime_series(_series(closes))
    assert all(st.regime is None for st in states)
    assert all(st.composite is None for st in states)


def test_sustained_uptrend_reads_bullish():
    closes = [100.0 * (1.004 ** i) for i in range(320)]
    states = regime_series(_series(closes))
    assert states[-1].regime == REGIME_BULLISH
    assert states[-1].composite is not None and states[-1].composite >= 3


def test_sustained_downtrend_reads_bearish():
    closes = [100.0 * (0.996 ** i) for i in range(320)]
    states = regime_series(_series(closes))
    assert states[-1].regime == REGIME_BEARISH
    assert states[-1].composite is not None and states[-1].composite <= -3


def test_regime_requires_all_inputs_before_calling_a_regime():
    """A composite from a partial input set is not comparable to a full one."""
    closes = [100.0 + i * 0.1 for i in range(260)]
    states = regime_series(_series(closes))
    for st in states:
        if st.regime is not None:
            assert all(v is not None for v in st.inputs.values())


def test_flip_detection_records_previous_regime_and_bars_since():
    """Rise for a long time, then collapse — the regime must flip and be dated."""
    up = [100.0 * (1.004 ** i) for i in range(300)]
    down = [up[-1] * (0.97 ** i) for i in range(1, 60)]
    states = regime_series(_series(up + down))
    last = states[-1]
    assert last.regime in (REGIME_NEUTRAL, REGIME_BEARISH)
    assert last.previous_regime is not None
    assert last.bars_since_flip is not None
    assert last.flip_label is not None and "→" in last.flip_label


def test_first_computable_bar_is_not_reported_as_a_flip():
    """We cannot know the regime before the first computable bar, so the
    baseline must not masquerade as a Neutral→Bullish transition."""
    closes = [100.0 * (1.004 ** i) for i in range(260)]
    states = regime_series(_series(closes))
    first_known = next(st for st in states if st.regime is not None)
    assert first_known.bars_since_flip is None
    assert first_known.previous_regime is None


# ── Snapshot bundle ──────────────────────────────────────────────────────────


def test_snapshot_populates_the_stack_on_a_long_series():
    closes = [100.0 + (i % 17) * 0.8 + i * 0.05 for i in range(320)]
    snap = snapshot(_series(closes))
    assert snap.close == pytest.approx(closes[-1])
    for field_name in ("ema21", "sma50", "sma200", "rsi", "atr", "zscore", "atr_stretch", "rvol", "roc20"):
        assert getattr(snap, field_name) is not None, f"{field_name} should be computable at 320 bars"
    assert snap.regime is not None


def test_snapshot_on_short_series_leaves_slow_indicators_none():
    """30 bars: RSI is computable, the 200 SMA is not. Neither may be faked."""
    closes = [100.0 + i for i in range(30)]
    snap = snapshot(_series(closes))
    assert snap.rsi is not None
    assert snap.sma200 is None
    assert snap.sma200_rising is None
    assert snap.regime is not None and snap.regime.regime is None


def test_snapshot_rejects_empty_series():
    with pytest.raises(ValueError):
        snapshot(BarSeries("TEST", "daily", []))
