"""Tests for the native TD Sequential engine.

These are the *first line* of the acceptance test in
`docs/technical-signals-layer-scope.md` §3.1:

    "native counts must match the paid TradingView indicator on 10 reference
     charts (Fred supplies screenshots) before Phase 1 signs off."

The reference-chart comparison cannot run until those screenshots exist. What
these tests do in the meantime is pin the *mechanics* against hand-constructed
series where the correct answer is derivable by hand — so that when a reference
chart does disagree, we know the disagreement is about an **[AMBIGUOUS]** rule
(see TDConfig) rather than a broken counter.

Construction helper `_series` builds bars from a list of closes; highs and lows
default to the close ± a small band so setup counting (which is close-only) is
isolated from perfection and countdown logic (which are high/low-sensitive).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.signals.bars import Bar, BarSeries
from app.services.signals.td_sequential import (
    COUNTDOWN_LENGTH,
    SETUP_LENGTH,
    TDConfig,
    compute,
    _is_perfected,
    _tdst_level,
)


def _series(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    symbol: str = "TEST",
    timeframe: str = "daily",
) -> BarSeries:
    """Build a BarSeries from closes, with optional explicit highs/lows.

    Default high/low band is close ± 0.5 — wide enough that it never
    accidentally satisfies a countdown or perfection test, so tests that care
    about those must set them explicitly.
    """
    start = date(2026, 1, 1)
    bars = []
    for i, c in enumerate(closes):
        hi = highs[i] if highs else c + 0.5
        lo = lows[i] if lows else c - 0.5
        bars.append(Bar(bar_date=start + timedelta(days=i), open=c, high=hi, low=lo, close=c, volume=1000.0))
    return BarSeries(symbol=symbol, timeframe=timeframe, bars=bars)


# ── Guard rails ──────────────────────────────────────────────────────────────


def test_short_series_returns_empty_state_without_raising():
    """§6 on-demand lookup must degrade gracefully for a newly listed ticker."""
    state = compute(_series([10, 11, 12]))
    assert state.setup_count == 0
    assert state.setup_direction is None
    assert state.events == []


def test_empty_series_is_safe():
    state = compute(BarSeries("TEST", "daily", []))
    assert state.setup_count == 0
    assert state.events == []


# ── Price flip ───────────────────────────────────────────────────────────────


def test_buy_setup_requires_a_bearish_price_flip():
    """A falling close alone does not start a Buy Setup — bar 1 needs a flip.

    Series falls monotonically from the start. The first bar that *could* be
    setup bar 1 needs its predecessor to have closed ABOVE the close 4 bars
    before it. In a monotonic decline that never happens, so no setup starts
    until the comparison window has a rising bar behind it.
    """
    closes = [100 - i for i in range(20)]  # strictly falling
    state = compute(_series(closes))
    # Every bar satisfies close < close[-4], but none is preceded by a bar where
    # close > close[-4], so no flip ever occurs and no setup ever begins.
    assert state.setup_count == 0
    assert state.setup_direction is None


def test_buy_setup_counts_to_nine_after_a_flip():
    """Rise for 6 bars (to establish the up-comparison), then fall for 9."""
    rising = [100, 101, 102, 103, 104, 105]
    # After the rise, drop hard so every subsequent close is below the close 4 back.
    falling = [95, 94, 93, 92, 91, 90, 89, 88, 87]
    state = compute(_series(rising + falling))

    assert state.setup_direction == "buy"
    assert state.setup_count == SETUP_LENGTH
    assert state.setup_complete is True
    assert len(state.events) == 1
    assert state.events[0].event_type == "setup_9"
    assert state.events[0].direction == "buy"


def test_sell_setup_counts_to_nine_after_a_flip():
    """Mirror of the buy case."""
    falling = [100, 99, 98, 97, 96, 95]
    rising = [105, 106, 107, 108, 109, 110, 111, 112, 113]
    state = compute(_series(falling + rising))

    assert state.setup_direction == "sell"
    assert state.setup_count == SETUP_LENGTH
    assert state.events[0].direction == "sell"


def test_interrupted_setup_resets_the_count():
    """One non-conforming close resets the run to zero — no partial credit."""
    rising = [100, 101, 102, 103, 104, 105]
    # 5 down bars, then a bar that closes ABOVE its 4-bars-back comparison,
    # which breaks the run.
    broken = [95, 94, 93, 92, 91, 200, 90, 89]
    state = compute(_series(rising + broken))
    assert state.setup_count < SETUP_LENGTH
    assert not any(e.event_type == "setup_9" for e in state.events)


def test_setup_of_exactly_nine_does_not_report_ten():
    """The counter must stop at 9 and reset, not run on to 10."""
    rising = [100, 101, 102, 103, 104, 105]
    falling = [95 - i for i in range(12)]  # 12 conforming down bars
    state = compute(_series(rising + falling))
    # Exactly one 9 fires; the count then restarts rather than continuing to 12.
    nines = [e for e in state.events if e.event_type == "setup_9"]
    assert len(nines) >= 1
    assert state.setup_count <= SETUP_LENGTH


# ── Setup perfection (§3.1) ──────────────────────────────────────────────────


def test_perfection_true_when_bar8_low_undercuts_bars_6_and_7():
    """Buy setup perfects when the low of bar 8 or 9 <= lows of bars 6 and 7."""
    # setup_start = 0; bars 6,7,8,9 are indices 5,6,7,8.
    lows = [10, 10, 10, 10, 10, 5.0, 5.0, 4.0, 9.0]   # bar 8 (idx 7) undercuts
    highs = [11] * 9
    assert _is_perfected("buy", 0, highs, lows) is True


def test_perfection_true_when_bar9_low_undercuts():
    lows = [10, 10, 10, 10, 10, 5.0, 5.0, 9.0, 4.0]   # bar 9 (idx 8) undercuts
    highs = [11] * 9
    assert _is_perfected("buy", 0, highs, lows) is True


def test_perfection_false_when_neither_bar_undercuts():
    lows = [10, 10, 10, 10, 10, 5.0, 5.0, 9.0, 9.0]   # neither 8 nor 9 <= 5.0
    highs = [11] * 9
    assert _is_perfected("buy", 0, highs, lows) is False


def test_sell_perfection_mirrors_on_highs():
    highs = [10, 10, 10, 10, 10, 15.0, 15.0, 16.0, 11.0]  # bar 8 exceeds
    lows = [9] * 9
    assert _is_perfected("sell", 0, highs, lows) is True
    highs_bad = [10, 10, 10, 10, 10, 15.0, 15.0, 11.0, 11.0]
    assert _is_perfected("sell", 0, highs_bad, lows) is False


# ── TDST levels (§3.1, §4) ───────────────────────────────────────────────────


def test_buy_setup_projects_tdst_resistance_at_highest_high():
    highs = [10, 12, 11, 14, 13, 10, 9, 8, 7]   # max = 14
    lows = [1] * 9
    assert _tdst_level("buy", 0, highs, lows) == 14


def test_sell_setup_projects_tdst_support_at_lowest_low():
    lows = [10, 8, 9, 6, 7, 10, 11, 12, 13]     # min = 6
    highs = [20] * 9
    assert _tdst_level("sell", 0, lows and highs, lows) == 6


def test_completed_buy_setup_records_tdst_resistance_on_state():
    rising = [100, 101, 102, 103, 104, 105]
    falling = [95, 94, 93, 92, 91, 90, 89, 88, 87]
    highs = [c + 1.0 for c in rising + falling]
    lows = [c - 1.0 for c in rising + falling]
    state = compute(_series(rising + falling, highs=highs, lows=lows))
    assert state.tdst_resistance is not None
    # TDST resistance is the highest high across setup bars 1-9, which are the
    # nine falling bars — highest is the first one at 95 + 1.0.
    assert state.tdst_resistance == pytest.approx(96.0)


# ── Countdown (§3.1) ─────────────────────────────────────────────────────────


def test_countdown_requires_a_completed_setup_first():
    """No setup → no countdown, however many bars satisfy the close/low test."""
    # Monotonic decline: never flips, so never sets up, so never counts down.
    closes = [100 - i for i in range(40)]
    state = compute(_series(closes))
    assert state.countdown_count == 0
    assert state.countdown_direction is None


def test_buy_countdown_increments_on_close_at_or_below_low_two_bars_back():
    """Build a completed buy setup, then feed bars that each qualify.

    Qualification is close <= low[i-2]. We force it by making every post-setup
    bar close below the prior bars' lows.
    """
    rising = [100, 101, 102, 103, 104, 105]
    setup = [95, 94, 93, 92, 91, 90, 89, 88, 87]
    # Continue declining so each close undercuts the low two bars earlier.
    countdown = [86 - i * 2 for i in range(6)]
    closes = rising + setup + countdown
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]

    state = compute(_series(closes, highs=highs, lows=lows))
    assert state.countdown_direction == "buy"
    assert state.countdown_count > 0


def test_bar_thirteen_defers_when_low_does_not_undercut_countdown_bar_eight_close():
    """The deferral rule is the highest-value test in this file.

    §3.1's acceptance test is most likely to fail on 13s, and the most common
    public-implementation bug is comparing against *setup* bar 8 rather than
    *countdown* bar 8 (see the module docstring). This test pins that the
    engine defers rather than printing a 13 when the low test fails.
    """
    closes, highs, lows = _twelve_countdown_bars()

    # Append the 13th candidate: its close still qualifies (it keeps declining),
    # but its LOW is held impossibly high, so it cannot undercut countdown bar
    # 8's close and the 13 must defer.
    closes.append(closes[-1] - 2)
    highs.append(closes[-1] + 0.1)
    lows.append(max(closes) + 100.0)

    state = compute(_series(closes, highs=highs, lows=lows))
    assert state.countdown_count == COUNTDOWN_LENGTH - 1     # held at 12
    assert state.countdown_deferred is True
    assert not any(e.event_type == "countdown_13" for e in state.events)


def test_bar_thirteen_completes_when_the_deferral_test_passes():
    """Positive counterpart to the deferral test — a valid 13 must fire.

    Identical setup, but the 13th bar's low is allowed to undercut countdown
    bar 8's close. Without this test, an engine that simply never prints a 13
    would pass the deferral test above.
    """
    closes, highs, lows = _twelve_countdown_bars()

    closes.append(closes[-1] - 2)
    highs.append(closes[-1] + 0.1)
    lows.append(closes[-1] - 50.0)      # deep low → deferral test passes

    state = compute(_series(closes, highs=highs, lows=lows))
    assert state.countdown_count == COUNTDOWN_LENGTH
    assert state.countdown_complete is True
    assert any(e.event_type == "countdown_13" and e.direction == "buy" for e in state.events)


def _twelve_countdown_bars() -> tuple[list[float], list[float], list[float]]:
    """Build a series whose buy countdown sits at exactly 12, awaiting bar 13.

    Bar arithmetic, which is fiddly enough to be worth stating: the setup-9 bar
    itself qualifies as countdown bar 1 (DeMark permits this, and compute()
    evaluates countdown on the same bar the setup completes). So reaching 12
    needs 11 further qualifying bars, not 12.
    """
    rising = [100.0, 101, 102, 103, 104, 105]
    setup = [95.0, 94, 93, 92, 91, 90, 89, 88, 87]      # 9 bars → setup complete, countdown = 1
    more = [86.0 - i * 2 for i in range(11)]            # → countdown 2..12
    closes = rising + setup + more
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]
    return closes, highs, lows


def test_opposite_setup_cancels_a_running_countdown():
    """Unambiguous DeMark rule, flagged in TDConfig for isolation."""
    rising = [100, 101, 102, 103, 104, 105]
    buy_setup = [95, 94, 93, 92, 91, 90, 89, 88, 87]
    # A couple of countdown bars, then a hard reversal that completes a SELL setup.
    partial_cd = [86, 85]
    sell_setup = [100, 101, 102, 103, 104, 105, 106, 107, 108]
    closes = rising + buy_setup + partial_cd + sell_setup

    state = compute(_series(closes), TDConfig(cancel_countdown_on_opposite_setup=True))
    # After the sell setup completes, a buy countdown must not still be running.
    assert state.countdown_direction != "buy" or state.countdown_count == 0


def test_recycle_flag_is_off_by_default():
    """**[AMBIGUOUS]** rule — default must be the common TradingView behaviour."""
    assert TDConfig().recycle_countdown_on_new_setup is False
    assert TDConfig().cancel_countdown_on_tdst_break is False
    assert TDConfig().cancel_countdown_on_opposite_setup is True


# ── Freshness bookkeeping (feeds §5 decay) ───────────────────────────────────


def test_bars_since_is_none_when_event_never_fired():
    state = compute(_series([100 + i for i in range(20)]))
    assert state.bars_since_buy_countdown_13 is None


def test_bars_since_counts_back_from_the_final_bar():
    rising = [100, 101, 102, 103, 104, 105]
    setup = [95, 94, 93, 92, 91, 90, 89, 88, 87]
    trailing = [88, 89, 90]   # three bars after the 9 completed
    state = compute(_series(rising + setup + trailing))
    assert state.bars_since_buy_setup_9 == len(trailing)


# ── Plain-language output (§6.3) ─────────────────────────────────────────────


def test_describe_renders_scope_doc_phrasing():
    """§6.3 asks for "TD buy setup on bar 8 of 9 · Daily"."""
    rising = [100, 101, 102, 103, 104, 105]
    partial = [95, 94, 93, 92, 91, 90, 89, 88]   # 8 of 9
    state = compute(_series(rising + partial))
    lines = state.describe()
    assert any("bar 8 of 9" in line and "Daily" in line for line in lines)


def test_weekly_timeframe_is_labelled_in_describe():
    rising = [100, 101, 102, 103, 104, 105]
    partial = [95, 94, 93, 92, 91, 90, 89, 88]
    state = compute(_series(rising + partial, timeframe="weekly"))
    assert any("Weekly" in line for line in state.describe())
