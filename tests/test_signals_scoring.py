"""Tests for the §5 scoring model.

Organised to mirror `docs/technical-signals-layer-scope.md` §5: one section per
row of the scoring table, then the three cross-cutting rules (regime gate,
event freshness, bands).

The recurring theme is that **an unknown input scores zero, never a default**.
Roughly a third of these tests exist solely to pin that, because the
2026-06-21 audit found "missing data = confident neutral signal" as a recurring
bug in this codebase and the scoring layer is where it would do the most damage.
"""

from __future__ import annotations

import pytest

from app.services.signals.config import (
    COUNTER_TREND_BADGE,
    COUNTER_TREND_CAP,
    ENTRY_MAX,
    FRESHNESS_DECAY_DAYS,
    FRESHNESS_FULL_DAYS,
    band_for_score,
)
from app.services.signals.indicators import (
    REGIME_BEARISH,
    REGIME_BULLISH,
    REGIME_NEUTRAL,
    IndicatorSnapshot,
    RegimeState,
)
from app.services.signals.scoring import (
    compute_entry_score,
    compute_exit_score,
    decay_factor,
)
from app.services.signals.td_sequential import TDState


# ── Builders ─────────────────────────────────────────────────────────────────


def _snap(**kwargs) -> IndicatorSnapshot:
    """An indicator snapshot with benign defaults; override what a test cares about.

    Defaults are deliberately *neutral-but-known* (not None) so that a test
    exercising one component isn't accidentally also testing the missing-data
    path. Tests that want missing data pass None explicitly.
    """
    base = dict(
        symbol="TEST",
        timeframe="daily",
        close=100.0,
        ema21=100.0,
        sma50=100.0,
        sma200=100.0,
        sma200_rising=True,
        rsi=50.0,
        rsi_prev=50.0,
        atr=2.0,
        zscore=0.0,
        atr_stretch=0.0,
        rvol=1.0,
        roc20=0.0,
        regime=RegimeState(regime=REGIME_NEUTRAL, composite=0),
        bullish_divergence=False,
        bearish_divergence=False,
    )
    base.update(kwargs)
    return IndicatorSnapshot(**base)


def _td(**kwargs) -> TDState:
    base = dict(symbol="TEST", timeframe="daily")
    base.update(kwargs)
    return TDState(**base)


def _entry(daily=None, weekly=None, dtd=None, wtd=None):
    return compute_entry_score(daily or _snap(), weekly or _snap(timeframe="weekly"), dtd or _td(), wtd or _td(timeframe="weekly"))


def _exit(daily=None, weekly=None, dtd=None, wtd=None):
    return compute_exit_score(daily or _snap(), weekly or _snap(timeframe="weekly"), dtd or _td(), wtd or _td(timeframe="weekly"))


def _component(score, key):
    return next(c for c in score.components if c.key == key)


# ── Event freshness (§5) ─────────────────────────────────────────────────────


def test_decay_full_credit_within_the_full_window():
    """§5: "score fully for 3 trading days"."""
    for bars in range(0, FRESHNESS_FULL_DAYS + 1):
        assert decay_factor(bars) == 1.0


def test_decay_reaches_zero_after_the_decay_window():
    """§5: "then decay linearly to zero over 10" — expired at 3 + 10 = 13."""
    assert decay_factor(FRESHNESS_FULL_DAYS + FRESHNESS_DECAY_DAYS) == 0.0
    assert decay_factor(FRESHNESS_FULL_DAYS + FRESHNESS_DECAY_DAYS + 5) == 0.0


def test_decay_is_linear_across_the_ramp():
    """Halfway through the ramp must be half credit."""
    midpoint = FRESHNESS_FULL_DAYS + FRESHNESS_DECAY_DAYS // 2
    assert decay_factor(midpoint) == pytest.approx(0.5)


def test_decay_is_monotonically_decreasing():
    values = [decay_factor(b) for b in range(0, 20)]
    assert values == sorted(values, reverse=True)


def test_decay_of_none_is_zero_not_full():
    """An event that never fired must score nothing — the missing-data rule.

    Returning 1.0 here would hand full TD-13 credit (25 points) to every symbol
    whose loaded history happens to contain no 13 at all.
    """
    assert decay_factor(None) == 0.0


def test_decay_of_negative_is_zero():
    assert decay_factor(-1) == 0.0


# ── §5 row: TD Sequential (25) ───────────────────────────────────────────────


def test_td_setup_7_scores_ten():
    score = _entry(dtd=_td(setup_direction="buy", setup_count=7))
    assert _component(score, "td_sequential").points == 10


def test_td_setup_9_complete_scores_eighteen_when_fresh():
    score = _entry(dtd=_td(bars_since_buy_setup_9=0))
    assert _component(score, "td_sequential").points == 18


def test_td_countdown_13_scores_the_row_maximum_when_fresh():
    score = _entry(dtd=_td(bars_since_buy_countdown_13=0))
    assert _component(score, "td_sequential").points == ENTRY_MAX["td_sequential"]


def test_td_countdown_13_outranks_setup_9():
    """Tiers are exclusive and the best wins — 13 beats 9."""
    score = _entry(dtd=_td(bars_since_buy_setup_9=0, bars_since_buy_countdown_13=0))
    assert _component(score, "td_sequential").points == ENTRY_MAX["td_sequential"]


def test_td_stale_event_decays_toward_zero():
    fresh = _entry(dtd=_td(bars_since_buy_setup_9=0))
    stale = _entry(dtd=_td(bars_since_buy_setup_9=8))
    expired = _entry(dtd=_td(bars_since_buy_setup_9=13))
    f, s, e = (_component(x, "td_sequential").points for x in (fresh, stale, expired))
    assert f > s > e
    assert e == 0


def test_weekly_confluence_adds_five_and_caps_at_row_max():
    """§5: "Weekly 9/13 confluence: +5 (capped 25)"."""
    daily_only = _entry(dtd=_td(bars_since_buy_setup_9=0))
    with_weekly = _entry(
        dtd=_td(bars_since_buy_setup_9=0),
        wtd=_td(timeframe="weekly", bars_since_buy_setup_9=0),
    )
    assert _component(with_weekly, "td_sequential").points == pytest.approx(
        _component(daily_only, "td_sequential").points + 5
    )
    # And with a 13 already at the cap, the bonus must not push past 25.
    capped = _entry(
        dtd=_td(bars_since_buy_countdown_13=0),
        wtd=_td(timeframe="weekly", bars_since_buy_countdown_13=0),
    )
    assert _component(capped, "td_sequential").points == ENTRY_MAX["td_sequential"]


def test_weekly_confluence_alone_scores_nothing():
    """A bonus with no daily signal to attach to is not itself a signal."""
    score = _entry(wtd=_td(timeframe="weekly", bars_since_buy_setup_9=0))
    assert _component(score, "td_sequential").points == 0


def test_sell_side_td_does_not_score_on_the_entry_side():
    score = _entry(dtd=_td(bars_since_sell_countdown_13=0))
    assert _component(score, "td_sequential").points == 0


# ── §5 row: Regime Composite (25) ────────────────────────────────────────────


def test_neutral_to_bullish_flip_scores_the_row_maximum():
    snap = _snap(regime=RegimeState(regime=REGIME_BULLISH, composite=4,
                                    previous_regime=REGIME_NEUTRAL, bars_since_flip=1))
    assert _component(_entry(daily=snap), "regime").points == ENTRY_MAX["regime"]


def test_bearish_to_neutral_flip_scores_twelve():
    snap = _snap(regime=RegimeState(regime=REGIME_NEUTRAL, composite=0,
                                    previous_regime=REGIME_BEARISH, bars_since_flip=2))
    assert _component(_entry(daily=snap), "regime").points == 12


def test_established_bullish_in_pullback_zone_scores_fifteen():
    # Price inside the 21 EMA / 50 SMA band.
    snap = _snap(close=99.0, ema21=100.0, sma50=95.0,
                 regime=RegimeState(regime=REGIME_BULLISH, composite=5, bars_since_flip=50))
    assert _component(_entry(daily=snap), "regime").points == 15


def test_established_bullish_when_extended_scores_nothing():
    """Bullish but far above the 21 EMA is not a pullback entry."""
    snap = _snap(close=130.0, ema21=100.0, sma50=95.0,
                 regime=RegimeState(regime=REGIME_BULLISH, composite=5, bars_since_flip=50))
    assert _component(_entry(daily=snap), "regime").points == 0


def test_uncomputable_regime_scores_zero_and_says_so():
    """The missing-data rule — must not be treated as Neutral."""
    snap = _snap(regime=RegimeState(regime=None, composite=None))
    comp = _component(_entry(daily=snap), "regime")
    assert comp.points == 0
    assert "not computable" in comp.detail.lower()


def test_regime_none_entirely_scores_zero():
    comp = _component(_entry(daily=_snap(regime=None)), "regime")
    assert comp.points == 0


def test_stale_flip_decays():
    fresh = _snap(regime=RegimeState(regime=REGIME_BULLISH, composite=4,
                                     previous_regime=REGIME_NEUTRAL, bars_since_flip=1))
    old = _snap(regime=RegimeState(regime=REGIME_BULLISH, composite=4,
                                   previous_regime=REGIME_NEUTRAL, bars_since_flip=20))
    assert _component(_entry(daily=fresh), "regime").points > _component(_entry(daily=old), "regime").points


# ── §5 row: Mean reversion stretch (20) ──────────────────────────────────────


def test_extreme_z_scores_twenty():
    assert _component(_entry(daily=_snap(zscore=-2.5)), "stretch").points == 20


def test_extreme_atr_stretch_alone_scores_twenty():
    """§5's "or": either trigger suffices, which is what makes it cross-asset."""
    assert _component(_entry(daily=_snap(zscore=-0.5, atr_stretch=-3.0)), "stretch").points == 20


def test_strong_and_mild_z_tiers():
    assert _component(_entry(daily=_snap(zscore=-1.6)), "stretch").points == 12
    assert _component(_entry(daily=_snap(zscore=-1.1)), "stretch").points == 6


def test_shallow_stretch_scores_nothing():
    assert _component(_entry(daily=_snap(zscore=-0.5)), "stretch").points == 0


def test_positive_z_scores_nothing_on_entry():
    """Extended to the upside is an exit condition, not an entry one."""
    assert _component(_entry(daily=_snap(zscore=2.5, atr_stretch=3.0)), "stretch").points == 0


def test_uncomputable_stretch_scores_zero():
    comp = _component(_entry(daily=_snap(zscore=None, atr_stretch=None)), "stretch")
    assert comp.points == 0
    assert "not computable" in comp.detail.lower()


# ── §5 row: Trend structure (15) ─────────────────────────────────────────────


def test_trend_components_are_additive_to_the_row_maximum():
    """§5 writes "+4" and "+3", and 8+4+3 = 15 = the row max."""
    snap = _snap(close=99.0, ema21=100.0, sma50=98.0, sma200=90.0, sma200_rising=True)
    assert _component(_entry(daily=snap), "trend").points == ENTRY_MAX["trend"]


def test_above_rising_200_alone_scores_eight():
    snap = _snap(close=130.0, ema21=120.0, sma50=90.0, sma200=100.0, sma200_rising=True)
    # Above a rising 200D (8); 50D < 200D so no golden cross; not in pullback zone.
    assert _component(_entry(daily=snap), "trend").points == 8


def test_above_falling_200_scores_no_trend_points():
    snap = _snap(close=130.0, ema21=120.0, sma50=90.0, sma200=100.0, sma200_rising=False)
    assert _component(_entry(daily=snap), "trend").points == 0


def test_uncomputable_200_scores_zero_and_says_so():
    snap = _snap(sma200=None, sma200_rising=None)
    comp = _component(_entry(daily=snap), "trend")
    assert "not computable" in comp.detail.lower()


# ── §5 row: RSI (10) ─────────────────────────────────────────────────────────


def test_oversold_turning_up_scores_ten():
    assert _component(_entry(daily=_snap(rsi=28.0, rsi_prev=25.0)), "rsi").points == 10


def test_oversold_still_falling_does_not_score_the_top_tier():
    """"Turning up" means turning up — a falling oversold RSI has not turned."""
    assert _component(_entry(daily=_snap(rsi=25.0, rsi_prev=28.0)), "rsi").points < 10


def test_mild_oversold_with_weekly_support_scores_seven():
    daily = _snap(rsi=33.0, rsi_prev=33.0)
    weekly = _snap(timeframe="weekly", rsi=45.0)
    assert _component(_entry(daily=daily, weekly=weekly), "rsi").points == 7


def test_bullish_divergence_scores_eight():
    assert _component(_entry(daily=_snap(rsi=40.0, bullish_divergence=True)), "rsi").points == 8


def test_uncomputable_rsi_scores_zero():
    assert _component(_entry(daily=_snap(rsi=None)), "rsi").points == 0


# ── §5 row: Volume (5) ───────────────────────────────────────────────────────


def test_rvol_tiers():
    assert _component(_entry(daily=_snap(rvol=2.5)), "volume").points == 5
    assert _component(_entry(daily=_snap(rvol=1.7)), "volume").points == 3
    assert _component(_entry(daily=_snap(rvol=1.0)), "volume").points == 0


def test_missing_volume_scores_zero_and_explains_why():
    """Legitimate for commodities and some crypto feeds — must be explicit."""
    comp = _component(_entry(daily=_snap(rvol=None)), "volume")
    assert comp.points == 0
    assert "no volume data" in comp.detail.lower()


# ── §5 regime gate ───────────────────────────────────────────────────────────


def test_gate_caps_entry_at_sixty_below_a_falling_200():
    """§5: "below a falling 200D, entry score capped at 60, badged counter-trend"."""
    snap = _snap(close=80.0, ema21=85.0, sma50=90.0, sma200=100.0, sma200_rising=False,
                 zscore=-2.5, rsi=25.0, rsi_prev=20.0, rvol=2.5,
                 regime=RegimeState(regime=REGIME_BULLISH, composite=4,
                                    previous_regime=REGIME_NEUTRAL, bars_since_flip=1))
    score = _entry(daily=snap, dtd=_td(bars_since_buy_countdown_13=0))
    assert score.raw_score > COUNTER_TREND_CAP     # would have scored higher ungated
    assert score.score == COUNTER_TREND_CAP
    assert score.counter_trend is True
    assert score.badge == COUNTER_TREND_BADGE


def test_gate_does_not_apply_below_a_rising_200():
    """A pullback under a rising 200D is not counter-trend."""
    snap = _snap(close=95.0, sma200=100.0, sma200_rising=True, zscore=-2.5,
                 rsi=25.0, rsi_prev=20.0, rvol=2.5)
    score = _entry(daily=snap, dtd=_td(bars_since_buy_countdown_13=0))
    assert score.counter_trend is False
    assert score.score == score.raw_score


def test_gate_does_not_apply_when_the_200_is_unknown():
    """An unknown trend is not evidence of an adverse one — see _apply_regime_gate."""
    snap = _snap(sma200=None, sma200_rising=None, zscore=-2.5, rsi=25.0, rsi_prev=20.0, rvol=2.5)
    score = _entry(daily=snap, dtd=_td(bars_since_buy_countdown_13=0))
    assert score.counter_trend is False
    assert score.score == score.raw_score


def test_gate_does_not_raise_a_low_score():
    """The gate is a cap, not a floor — a weak counter-trend score stays weak."""
    snap = _snap(close=80.0, sma200=100.0, sma200_rising=False)
    score = _entry(daily=snap)
    assert score.score < COUNTER_TREND_CAP


def test_exit_scores_are_never_gated():
    """§5 scopes the cap to entries; capping exits would mute the loudest warnings."""
    snap = _snap(close=80.0, ema21=90.0, sma50=95.0, sma200=100.0, sma200_rising=False,
                 zscore=2.5, rsi=80.0,
                 regime=RegimeState(regime=REGIME_BEARISH, composite=-5,
                                    previous_regime=REGIME_NEUTRAL, bars_since_flip=1))
    score = _exit(daily=snap, dtd=_td(bars_since_sell_countdown_13=0))
    assert score.score > COUNTER_TREND_CAP
    assert score.counter_trend is False


# ── §5 bands ─────────────────────────────────────────────────────────────────


def test_band_boundaries_match_the_scope_doc():
    """§5: "85-100 Prime · 70-84 Strong · 50-69 Watch · <50 no signal"."""
    assert band_for_score(100) == "Prime"
    assert band_for_score(85) == "Prime"
    assert band_for_score(84) == "Strong"
    assert band_for_score(70) == "Strong"
    assert band_for_score(69) == "Watch"
    assert band_for_score(50) == "Watch"
    assert band_for_score(49) == "No signal"
    assert band_for_score(0) == "No signal"


# ── Totals and invariants ────────────────────────────────────────────────────


def test_entry_component_maxima_sum_to_one_hundred():
    """§5's table must add up — a typo in config.py would silently rescale scoring."""
    assert sum(ENTRY_MAX.values()) == 100


def test_a_fully_blank_snapshot_scores_zero_not_fifty():
    """The headline missing-data test.

    Every indicator unknown must produce 0 with a "no signal" band — not a
    mid-range score implying the engine looked and found nothing notable.
    """
    blank = _snap(ema21=None, sma50=None, sma200=None, sma200_rising=None, rsi=None,
                  rsi_prev=None, atr=None, zscore=None, atr_stretch=None, rvol=None,
                  roc20=None, regime=None)
    score = _entry(daily=blank, weekly=_snap(timeframe="weekly", rsi=None))
    assert score.score == 0
    assert score.band == "No signal"


def test_score_is_clamped_to_0_100():
    """Stack every entry component at maximum and confirm the total is bounded."""
    snap = _snap(close=99.0, ema21=100.0, sma50=98.0, sma200=90.0, sma200_rising=True,
                 zscore=-3.0, atr_stretch=-3.0, rsi=25.0, rsi_prev=20.0, rvol=3.0,
                 bullish_divergence=True,
                 regime=RegimeState(regime=REGIME_BULLISH, composite=5,
                                    previous_regime=REGIME_NEUTRAL, bars_since_flip=0))
    score = _entry(daily=snap,
                   dtd=_td(bars_since_buy_countdown_13=0),
                   wtd=_td(timeframe="weekly", bars_since_buy_countdown_13=0))
    assert 0 <= score.score <= 100
    assert score.band == "Prime"


def test_drivers_only_list_components_that_scored():
    """§6.3's "Firing now" — a zero-point component is not firing."""
    score = _entry(daily=_snap(zscore=-2.5))
    assert any("Z-score" in d for d in score.drivers)
    assert not any("No TD buy signal" in d for d in score.drivers)


def test_to_dict_is_json_safe():
    import json

    score = _entry(daily=_snap(zscore=-2.5))
    json.dumps(score.to_dict())    # must not raise
