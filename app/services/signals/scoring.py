"""Technical Signals Layer — entry and exit scoring.

Implements `docs/technical-signals-layer-scope.md` §5 verbatim. Every weight,
threshold and band lives in config.py (§5: "Weights and thresholds live in a
config file"); this module holds only the logic that combines them.

§5's entry table, which the component functions below implement one-for-one:

    | Component              | Max | Logic (summary)
    | TD Sequential          | 25  | Daily buy setup 7-8: 10 · Daily 9 complete: 18
    |                        |     | Daily 13 countdown: 25 · Weekly 9/13 confluence: +5 (capped 25)
    | Regime Composite       | 25  | Bearish→Neutral flip: 12 · Neutral→Bullish flip: 25
    |                        |     | Established Bullish, pullback context: 15
    | Mean reversion stretch | 20  | Z <= -2 vs 50D or >= 2.5 ATR below 21 EMA: 20 · Z <= -1.5: 12 · Z <= -1: 6
    | Trend structure        | 15  | Above rising 200D: 8 · 50D > 200D: +4 · Pullback into 21 EMA/50D zone: +3
    | RSI                    | 10  | Daily < 30 turning up: 10 · Daily < 35 with weekly > 40: 7 · Bullish divergence: 8
    | Volume                 |  5  | RVOL >= 2 on signal day: 5 · RVOL >= 1.5: 3

Plus three cross-cutting rules, each implemented once and applied to both sides:

  * **Regime gate** — "below a falling 200D, entry score capped at 60, badged
    'counter-trend'."
  * **Event freshness** — "TD completions and regime flips score fully for 3
    trading days, then decay linearly to zero over 10."
  * **Bands** — "85-100 Prime · 70-84 Strong · 50-69 Watch · <50 no signal."

═══════════════════════════════════════════════════════════════════════════════
THE MISSING-DATA RULE
═══════════════════════════════════════════════════════════════════════════════

Indicators arrive as None when not computable (see indicators.py's warmup
contract). Every component below awards **zero points** for a None input and
records why in its detail string. It never substitutes a neutral value.

This is deliberate and specific: the 2026-06-21 audit of this codebase found a
recurring "missing data = confident neutral signal" bug. Here that bug would
mean a freshly listed ticker with no 200 SMA scoring as though its trend
structure had been evaluated. Instead it scores 0 for that component and the
§6.3 breakdown says so, so a low score is visibly "we don't know yet" rather
than silently "this looks bad".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.signals.config import (
    COUNTER_TREND_BADGE,
    COUNTER_TREND_CAP,
    ENTRY_MAX,
    EXIT_MAX,
    EXIT_RSI_OVERBOUGHT,
    EXIT_STRETCH_ATR_EXTREME,
    EXIT_STRETCH_Z_EXTREME,
    FRESHNESS_DECAY_DAYS,
    FRESHNESS_FULL_DAYS,
    PULLBACK_ZONE_TOLERANCE,
    REGIME_ESTABLISHED_BULL_PULLBACK_POINTS,
    REGIME_FLIP_BEAR_TO_NEUTRAL_POINTS,
    REGIME_FLIP_NEUTRAL_TO_BULL_POINTS,
    RSI_BULLISH_DIVERGENCE_POINTS,
    RSI_MILD_OVERSOLD,
    RSI_MILD_WITH_WEEKLY_POINTS,
    RSI_OVERSOLD,
    RSI_OVERSOLD_TURNING_POINTS,
    RSI_WEEKLY_SUPPORT,
    RVOL_ELEVATED,
    RVOL_ELEVATED_POINTS,
    RVOL_HIGH,
    RVOL_HIGH_POINTS,
    STRETCH_ATR_EXTREME,
    STRETCH_EXTREME_POINTS,
    STRETCH_MILD_POINTS,
    STRETCH_STRONG_POINTS,
    STRETCH_Z_EXTREME,
    STRETCH_Z_MILD,
    STRETCH_Z_STRONG,
    TD_COUNTDOWN_13_POINTS,
    TD_SETUP_7_8_POINTS,
    TD_SETUP_9_POINTS,
    TD_WEEKLY_CONFLUENCE_BONUS,
    TREND_ABOVE_RISING_200_POINTS,
    TREND_GOLDEN_CROSS_POINTS,
    TREND_PULLBACK_ZONE_POINTS,
    band_for_score,
)
from app.services.signals.indicators import (
    REGIME_BEARISH,
    REGIME_BULLISH,
    REGIME_NEUTRAL,
    IndicatorSnapshot,
)
from app.services.signals.td_sequential import TDState


# ── Result containers ────────────────────────────────────────────────────────


@dataclass
class Component:
    """One row of the §5 scoring table, as evaluated for a specific bar.

    `detail` is user-facing: it is what the §9 row-expand card shows in the
    component breakdown, and it must explain a zero as clearly as a maximum.
    """

    key: str
    label: str
    points: float
    max_points: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Score:
    """A complete §5 score for one symbol on one side (entry or exit)."""

    symbol: str
    side: str                     # 'entry' | 'exit'
    score: int                    # 0-100, post-gate
    band: str                     # §5 bands
    raw_score: int                # pre-gate, for the §9 breakdown
    counter_trend: bool = False   # §5 regime gate applied
    badge: str | None = None      # COUNTER_TREND_BADGE when gated
    components: list[Component] = field(default_factory=list)
    drivers: list[str] = field(default_factory=list)   # §6.3 "Firing now"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "score": self.score,
            "band": self.band,
            "raw_score": self.raw_score,
            "counter_trend": self.counter_trend,
            "badge": self.badge,
            "components": [c.to_dict() for c in self.components],
            "drivers": self.drivers,
        }


# ── Event freshness (§5) ─────────────────────────────────────────────────────


def decay_factor(
    bars_since: int | None,
    full_days: int = FRESHNESS_FULL_DAYS,
    decay_days: int = FRESHNESS_DECAY_DAYS,
) -> float:
    """Freshness weight for an event that fired `bars_since` bars ago — §5.

    §5: "TD completions and regime flips score fully for 3 trading days, then
    decay linearly to zero over 10."

    Read as a piecewise curve:

        bars_since <= 3           → 1.0        (full credit)
        3 < bars_since < 13       → linear ramp from 1.0 down to 0.0
        bars_since >= 13          → 0.0        (expired)

    The decay window runs for `decay_days` bars *after* the full-credit window
    ends, so an event is worth nothing from bar 13 onward (3 + 10).

    `bars_since is None` means the event never fired in the loaded history and
    returns 0.0 — never full credit. Defaulting an unknown to "fresh" would
    hand 25 points to every symbol whose history is too short to contain a TD
    13, which is the missing-data bug this module exists to avoid.
    """
    if bars_since is None or bars_since < 0:
        return 0.0
    if bars_since <= full_days:
        return 1.0
    elapsed = bars_since - full_days
    if elapsed >= decay_days:
        return 0.0
    return 1.0 - (elapsed / decay_days)


# ── Entry components (§5) ────────────────────────────────────────────────────


def _entry_td(daily: TDState, weekly: TDState) -> Component:
    """§5 TD Sequential row, entry side (buy signals only).

    Tiers are mutually exclusive and we take the best one, then add the weekly
    confluence bonus and cap at the row maximum — exactly as §5 writes it:
    "Weekly 9/13 confluence: +5 (capped 25)".

    Freshness (§5) applies to the two *event* tiers (a completed 9, a completed
    13) because those fired on a specific bar. It does not apply to the "setup
    7-8" tier, which describes the count as it stands right now — a live count
    is not a stale event.
    """
    max_points = ENTRY_MAX["td_sequential"]
    best = 0.0
    detail = "No TD buy signal"

    # Tier 1: an in-progress buy setup on bar 7 or 8. Current state, no decay.
    if daily.setup_direction == "buy" and daily.setup_count in (7, 8):
        best = float(TD_SETUP_7_8_POINTS)
        detail = f"Daily buy setup on bar {daily.setup_count} of 9"

    # Tier 2: a completed daily buy setup 9, decayed by age.
    nine_factor = decay_factor(daily.bars_since_buy_setup_9)
    if nine_factor > 0:
        points = TD_SETUP_9_POINTS * nine_factor
        if points > best:
            best = points
            age = daily.bars_since_buy_setup_9
            detail = f"Daily buy setup 9 complete ({age}d ago, freshness {nine_factor:.0%})"

    # Tier 3: a completed daily buy countdown 13, decayed by age. Highest tier.
    thirteen_factor = decay_factor(daily.bars_since_buy_countdown_13)
    if thirteen_factor > 0:
        points = TD_COUNTDOWN_13_POINTS * thirteen_factor
        if points > best:
            best = points
            age = daily.bars_since_buy_countdown_13
            detail = f"Daily buy countdown 13 ({age}d ago, freshness {thirteen_factor:.0%})"

    # Weekly confluence bonus: a weekly buy 9 or 13 within its own freshness
    # window. Only awarded when the daily side already fired something —
    # a bonus with nothing to add to is not a signal.
    if best > 0:
        weekly_factor = max(
            decay_factor(weekly.bars_since_buy_setup_9),
            decay_factor(weekly.bars_since_buy_countdown_13),
        )
        if weekly_factor > 0:
            best += TD_WEEKLY_CONFLUENCE_BONUS * weekly_factor
            detail += " + weekly confluence"

    return Component(
        key="td_sequential",
        label="TD Sequential",
        points=min(best, max_points),
        max_points=max_points,
        detail=detail,
    )


def _entry_regime(snap: IndicatorSnapshot) -> Component:
    """§5 Regime Composite row, entry side.

    Three mutually exclusive states, best wins:
      * Neutral→Bullish flip: 25   (decayed)
      * Bearish→Neutral flip: 12   (decayed)
      * Established Bullish in a pullback context: 15  (state, no decay)
    """
    max_points = ENTRY_MAX["regime"]
    regime = snap.regime

    if regime is None or regime.regime is None:
        return Component(
            key="regime",
            label="Regime Composite",
            points=0.0,
            max_points=max_points,
            # Explicit: not enough history, as opposed to a Neutral reading.
            detail="Regime not computable (insufficient history)",
        )

    flip_factor = decay_factor(regime.bars_since_flip)
    label = regime.flip_label

    if label == f"{REGIME_NEUTRAL}→{REGIME_BULLISH}" and flip_factor > 0:
        return Component(
            key="regime",
            label="Regime Composite",
            points=REGIME_FLIP_NEUTRAL_TO_BULL_POINTS * flip_factor,
            max_points=max_points,
            detail=f"Neutral→Bullish flip ({regime.bars_since_flip}d ago, freshness {flip_factor:.0%})",
        )

    if label == f"{REGIME_BEARISH}→{REGIME_NEUTRAL}" and flip_factor > 0:
        return Component(
            key="regime",
            label="Regime Composite",
            points=REGIME_FLIP_BEAR_TO_NEUTRAL_POINTS * flip_factor,
            max_points=max_points,
            detail=f"Bearish→Neutral flip ({regime.bars_since_flip}d ago, freshness {flip_factor:.0%})",
        )

    if regime.regime == REGIME_BULLISH and _in_pullback_zone(snap):
        return Component(
            key="regime",
            label="Regime Composite",
            points=float(REGIME_ESTABLISHED_BULL_PULLBACK_POINTS),
            max_points=max_points,
            detail="Established Bullish, price in pullback zone",
        )

    return Component(
        key="regime",
        label="Regime Composite",
        points=0.0,
        max_points=max_points,
        detail=f"Regime {regime.regime}, no scoring flip or pullback",
    )


def _in_pullback_zone(snap: IndicatorSnapshot) -> bool:
    """Has price retraced into the 21 EMA / 50 SMA band? — §5 "pullback context".

    **[INTERPRETATION]** — §5 names the zone but not its edges; see
    PULLBACK_ZONE_TOLERANCE in config.py. Price counts as in-zone when it sits
    at or below the 21 EMA (plus tolerance) and at or above the 50 SMA (minus
    tolerance): it has pulled back off the fast average but has not broken the
    intermediate trend.

    Returns False when either average is unknown — no zone, no points.
    """
    if snap.ema21 is None or snap.sma50 is None:
        return False
    upper = snap.ema21 * (1.0 + PULLBACK_ZONE_TOLERANCE)
    lower = snap.sma50 * (1.0 - PULLBACK_ZONE_TOLERANCE)
    return lower <= snap.close <= upper


def _entry_stretch(snap: IndicatorSnapshot) -> Component:
    """§5 Mean reversion stretch row, entry side.

    "Z <= -2 vs 50D or >= 2.5 ATR below 21 EMA: 20 · Z <= -1.5: 12 · Z <= -1: 6"

    The 20-point tier fires on *either* trigger (§5's "or"), which matters for
    cross-asset comparability (§4): a crypto name can be 2.5 ATRs below its 21
    EMA without a -2 z-score, and vice versa for a low-volatility ETF.
    """
    max_points = ENTRY_MAX["stretch"]
    z = snap.zscore
    stretch = snap.atr_stretch

    if z is None and stretch is None:
        return Component("stretch", "Mean reversion", 0.0, max_points, "Stretch not computable")

    # atr_stretch is signed with positive = above the anchor, so "2.5 ATR below"
    # is <= -2.5. See indicators.atr_distance for the sign convention.
    extreme = (z is not None and z <= STRETCH_Z_EXTREME) or (
        stretch is not None and stretch <= -STRETCH_ATR_EXTREME
    )
    if extreme:
        return Component("stretch", "Mean reversion", float(STRETCH_EXTREME_POINTS), max_points,
                         _stretch_detail(z, stretch, "extreme"))
    if z is not None and z <= STRETCH_Z_STRONG:
        return Component("stretch", "Mean reversion", float(STRETCH_STRONG_POINTS), max_points,
                         _stretch_detail(z, stretch, "strong"))
    if z is not None and z <= STRETCH_Z_MILD:
        return Component("stretch", "Mean reversion", float(STRETCH_MILD_POINTS), max_points,
                         _stretch_detail(z, stretch, "mild"))

    return Component("stretch", "Mean reversion", 0.0, max_points, _stretch_detail(z, stretch, "none"))


def _stretch_detail(z: float | None, stretch: float | None, tier: str) -> str:
    parts = []
    if z is not None:
        parts.append(f"Z-score {z:+.2f} vs 50D")
    if stretch is not None:
        parts.append(f"{stretch:+.2f} ATR vs 21 EMA")
    body = " · ".join(parts) if parts else "no stretch data"
    return body if tier == "none" else f"{body} ({tier})"


def _entry_trend(snap: IndicatorSnapshot) -> Component:
    """§5 Trend structure row, entry side.

    "Above rising 200D: 8 · 50D > 200D: +4 · Pullback into 21 EMA/50D zone: +3"

    These are **additive**, not exclusive — §5 writes the second and third with
    a leading "+", and 8 + 4 + 3 = 15 = the row maximum, which confirms the
    reading.
    """
    max_points = ENTRY_MAX["trend"]
    points = 0.0
    notes: list[str] = []

    if snap.sma200 is None or snap.sma200_rising is None:
        notes.append("200D not computable")
    elif snap.close > snap.sma200 and snap.sma200_rising:
        points += TREND_ABOVE_RISING_200_POINTS
        notes.append("above rising 200D")
    else:
        notes.append("not above a rising 200D")

    if snap.sma50 is not None and snap.sma200 is not None and snap.sma50 > snap.sma200:
        points += TREND_GOLDEN_CROSS_POINTS
        notes.append("50D > 200D")

    if _in_pullback_zone(snap):
        points += TREND_PULLBACK_ZONE_POINTS
        notes.append("in 21 EMA/50D pullback zone")

    return Component("trend", "Trend structure", min(points, max_points), max_points, " · ".join(notes))


def _entry_rsi(daily: IndicatorSnapshot, weekly: IndicatorSnapshot) -> Component:
    """§5 RSI row, entry side.

    "Daily < 30 turning up: 10 · Daily < 35 with weekly > 40: 7 · Bullish divergence: 8"

    Three tiers, best wins. "Turning up" is read as the current RSI being above
    the previous bar's — an oversold reading that is still falling has not
    turned, which is the distinction the wording draws.
    """
    max_points = ENTRY_MAX["rsi"]
    r, r_prev = daily.rsi, daily.rsi_prev

    if r is None:
        return Component("rsi", "RSI", 0.0, max_points, "RSI not computable")

    best = 0.0
    detail = f"Daily RSI {r:.1f}"

    if r < RSI_OVERSOLD and r_prev is not None and r > r_prev:
        best = float(RSI_OVERSOLD_TURNING_POINTS)
        detail = f"Daily RSI {r:.1f} (<30) turning up from {r_prev:.1f}"

    if daily.bullish_divergence and RSI_BULLISH_DIVERGENCE_POINTS > best:
        best = float(RSI_BULLISH_DIVERGENCE_POINTS)
        detail = f"Bullish RSI divergence (daily RSI {r:.1f})"

    weekly_rsi = weekly.rsi
    if (
        r < RSI_MILD_OVERSOLD
        and weekly_rsi is not None
        and weekly_rsi > RSI_WEEKLY_SUPPORT
        and RSI_MILD_WITH_WEEKLY_POINTS > best
    ):
        best = float(RSI_MILD_WITH_WEEKLY_POINTS)
        detail = f"Daily RSI {r:.1f} (<35) with weekly RSI {weekly_rsi:.1f} (>40)"

    return Component("rsi", "RSI", best, max_points, detail)


def _entry_volume(snap: IndicatorSnapshot) -> Component:
    """§5 Volume row: "RVOL >= 2 on signal day: 5 · RVOL >= 1.5: 3"."""
    max_points = ENTRY_MAX["volume"]
    rv = snap.rvol
    if rv is None:
        # Common and legitimate for commodities and some crypto feeds — see
        # Bar.volume. Zero points, stated plainly.
        return Component("volume", "Volume", 0.0, max_points, "No volume data for this instrument")
    if rv >= RVOL_HIGH:
        return Component("volume", "Volume", float(RVOL_HIGH_POINTS), max_points, f"RVOL {rv:.2f} (>=2)")
    if rv >= RVOL_ELEVATED:
        return Component("volume", "Volume", float(RVOL_ELEVATED_POINTS), max_points, f"RVOL {rv:.2f} (>=1.5)")
    return Component("volume", "Volume", 0.0, max_points, f"RVOL {rv:.2f}")


# ── Exit components (§5, "Exit Score (0-100), mirrored") ─────────────────────


def _exit_td(daily: TDState, weekly: TDState) -> Component:
    """§5 exit: "TD sell 9/13 (25)" — the buy-side logic mirrored onto sells."""
    max_points = EXIT_MAX["td_sequential"]
    best = 0.0
    detail = "No TD sell signal"

    if daily.setup_direction == "sell" and daily.setup_count in (7, 8):
        best = float(TD_SETUP_7_8_POINTS)
        detail = f"Daily sell setup on bar {daily.setup_count} of 9"

    nine_factor = decay_factor(daily.bars_since_sell_setup_9)
    if nine_factor > 0 and TD_SETUP_9_POINTS * nine_factor > best:
        best = TD_SETUP_9_POINTS * nine_factor
        detail = f"Daily sell setup 9 complete ({daily.bars_since_sell_setup_9}d ago)"

    thirteen_factor = decay_factor(daily.bars_since_sell_countdown_13)
    if thirteen_factor > 0 and TD_COUNTDOWN_13_POINTS * thirteen_factor > best:
        best = TD_COUNTDOWN_13_POINTS * thirteen_factor
        detail = f"Daily sell countdown 13 ({daily.bars_since_sell_countdown_13}d ago)"

    if best > 0:
        weekly_factor = max(
            decay_factor(weekly.bars_since_sell_setup_9),
            decay_factor(weekly.bars_since_sell_countdown_13),
        )
        if weekly_factor > 0:
            best += TD_WEEKLY_CONFLUENCE_BONUS * weekly_factor
            detail += " + weekly confluence"

    return Component("td_sequential", "TD Sequential", min(best, max_points), max_points, detail)


def _exit_regime(snap: IndicatorSnapshot) -> Component:
    """§5 exit: "Regime deterioration (25)".

    Mirrors the entry flips: Bullish→Neutral is partial deterioration,
    Neutral→Bearish is full. Same decay treatment.
    """
    max_points = EXIT_MAX["regime"]
    regime = snap.regime
    if regime is None or regime.regime is None:
        return Component("regime", "Regime Composite", 0.0, max_points,
                         "Regime not computable (insufficient history)")

    factor = decay_factor(regime.bars_since_flip)
    label = regime.flip_label

    if label == f"{REGIME_NEUTRAL}→{REGIME_BEARISH}" and factor > 0:
        return Component("regime", "Regime Composite", REGIME_FLIP_NEUTRAL_TO_BULL_POINTS * factor,
                         max_points, f"Neutral→Bearish flip ({regime.bars_since_flip}d ago)")
    if label == f"{REGIME_BULLISH}→{REGIME_NEUTRAL}" and factor > 0:
        return Component("regime", "Regime Composite", REGIME_FLIP_BEAR_TO_NEUTRAL_POINTS * factor,
                         max_points, f"Bullish→Neutral flip ({regime.bars_since_flip}d ago)")
    if regime.regime == REGIME_BEARISH:
        return Component("regime", "Regime Composite", float(REGIME_ESTABLISHED_BULL_PULLBACK_POINTS),
                         max_points, "Established Bearish regime")

    return Component("regime", "Regime Composite", 0.0, max_points,
                     f"Regime {regime.regime}, no deterioration")


def _exit_stretch(snap: IndicatorSnapshot) -> Component:
    """§5 exit: "stretch >= +2 z / +2.5 ATR above 21 EMA (20)"."""
    max_points = EXIT_MAX["stretch"]
    z, stretch = snap.zscore, snap.atr_stretch
    if z is None and stretch is None:
        return Component("stretch", "Mean reversion", 0.0, max_points, "Stretch not computable")

    extreme = (z is not None and z >= EXIT_STRETCH_Z_EXTREME) or (
        stretch is not None and stretch >= EXIT_STRETCH_ATR_EXTREME
    )
    if extreme:
        return Component("stretch", "Mean reversion", float(STRETCH_EXTREME_POINTS), max_points,
                         _stretch_detail(z, stretch, "extended"))
    # §5 gives the exit side only the one 20-point tier, unlike the entry side's
    # three. Transcribed as written — not an omission.
    return Component("stretch", "Mean reversion", 0.0, max_points, _stretch_detail(z, stretch, "none"))


def _exit_structure(snap: IndicatorSnapshot) -> Component:
    """§5 exit: "loss of 21 EMA / 50D on close (15)".

    Split evenly: losing the 21 EMA is the earlier, weaker warning; losing the
    50 SMA as well is the fuller breakdown. **[INTERPRETATION]** — §5 gives one
    combined figure and does not weight the two legs, so an even split is the
    neutral reading. A tuning candidate for the §5 Phase 4 backtest.
    """
    max_points = EXIT_MAX["structure"]
    if snap.ema21 is None and snap.sma50 is None:
        return Component("structure", "Structure", 0.0, max_points, "Moving averages not computable")

    points = 0.0
    notes: list[str] = []
    half = max_points / 2.0

    if snap.ema21 is not None and snap.close < snap.ema21:
        points += half
        notes.append("closed below 21 EMA")
    if snap.sma50 is not None and snap.close < snap.sma50:
        points += half
        notes.append("closed below 50D")

    return Component("structure", "Structure", points, max_points,
                     " · ".join(notes) if notes else "holding 21 EMA and 50D")


def _exit_rsi(daily: IndicatorSnapshot, weekly: IndicatorSnapshot) -> Component:
    """§5 exit: "daily RSI > 75 or weekly bearish divergence (10)"."""
    max_points = EXIT_MAX["rsi"]
    r = daily.rsi
    if r is None:
        return Component("rsi", "RSI", 0.0, max_points, "RSI not computable")
    if r > EXIT_RSI_OVERBOUGHT:
        return Component("rsi", "RSI", float(max_points), max_points, f"Daily RSI {r:.1f} (>75)")
    if weekly.bearish_divergence:
        return Component("rsi", "RSI", float(max_points), max_points, "Weekly bearish RSI divergence")
    return Component("rsi", "RSI", 0.0, max_points, f"Daily RSI {r:.1f}")


def _exit_volume(snap: IndicatorSnapshot) -> Component:
    """§5 exit: "distribution volume (5)".

    Distribution = elevated volume on a down close. Volume alone is direction-
    less, so the down-close condition is what makes it distribution rather than
    accumulation.
    """
    max_points = EXIT_MAX["volume"]
    rv = snap.rvol
    if rv is None:
        return Component("volume", "Volume", 0.0, max_points, "No volume data for this instrument")
    down_close = snap.ema21 is not None and snap.close < snap.ema21
    if rv >= RVOL_HIGH and down_close:
        return Component("volume", "Volume", float(RVOL_HIGH_POINTS), max_points,
                         f"Distribution volume (RVOL {rv:.2f} below 21 EMA)")
    if rv >= RVOL_ELEVATED and down_close:
        return Component("volume", "Volume", float(RVOL_ELEVATED_POINTS), max_points,
                         f"Elevated volume (RVOL {rv:.2f} below 21 EMA)")
    return Component("volume", "Volume", 0.0, max_points, f"RVOL {rv:.2f}")


# ── Public API ───────────────────────────────────────────────────────────────


def compute_entry_score(
    daily_snap: IndicatorSnapshot,
    weekly_snap: IndicatorSnapshot,
    daily_td: TDState,
    weekly_td: TDState,
) -> Score:
    """Full §5 entry score for one symbol, including the regime gate."""
    components = [
        _entry_td(daily_td, weekly_td),
        _entry_regime(daily_snap),
        _entry_stretch(daily_snap),
        _entry_trend(daily_snap),
        _entry_rsi(daily_snap, weekly_snap),
        _entry_volume(daily_snap),
    ]
    raw = int(round(sum(c.points for c in components)))
    raw = max(0, min(100, raw))

    gated, counter_trend = _apply_regime_gate(raw, daily_snap)
    return Score(
        symbol=daily_snap.symbol,
        side="entry",
        score=gated,
        band=band_for_score(gated),
        raw_score=raw,
        counter_trend=counter_trend,
        badge=COUNTER_TREND_BADGE if counter_trend else None,
        components=components,
        drivers=_drivers(components, daily_td, weekly_td),
    )


def compute_exit_score(
    daily_snap: IndicatorSnapshot,
    weekly_snap: IndicatorSnapshot,
    daily_td: TDState,
    weekly_td: TDState,
) -> Score:
    """Full §5 exit score for one symbol.

    The regime gate is **not** applied to exits. §5 scopes it to entries only
    ("below a falling 200D, entry score capped at 60"), and capping an exit
    score in a downtrend would suppress exactly the warnings that matter most.
    """
    components = [
        _exit_td(daily_td, weekly_td),
        _exit_regime(daily_snap),
        _exit_stretch(daily_snap),
        _exit_structure(daily_snap),
        _exit_rsi(daily_snap, weekly_snap),
        _exit_volume(daily_snap),
    ]
    raw = int(round(sum(c.points for c in components)))
    raw = max(0, min(100, raw))
    return Score(
        symbol=daily_snap.symbol,
        side="exit",
        score=raw,
        band=band_for_score(raw),
        raw_score=raw,
        components=components,
        drivers=_drivers(components, daily_td, weekly_td),
    )


def _apply_regime_gate(raw: int, snap: IndicatorSnapshot) -> tuple[int, bool]:
    """§5 regime gate: "below a falling 200D, entry score capped at 60".

    Both conditions must hold — price below the 200 SMA *and* that 200 SMA
    falling. Price below a still-rising 200D is an ordinary pullback, not a
    counter-trend entry.

    When the 200 SMA is not yet computable the gate does **not** apply. That is
    the deliberate choice: the gate is an assertion that we know the trend is
    against us, and an unknown trend is not evidence of one. The trend
    component has already awarded zero points for the same unknown, so a short
    -history symbol is penalised once, not twice.
    """
    if snap.sma200 is None or snap.sma200_rising is None:
        return raw, False
    below_falling_200 = snap.close < snap.sma200 and not snap.sma200_rising
    if below_falling_200 and raw > COUNTER_TREND_CAP:
        return COUNTER_TREND_CAP, True
    return raw, below_falling_200


def _drivers(components: list[Component], daily_td: TDState, weekly_td: TDState) -> list[str]:
    """The §6.3 "Firing now" list, in plain terms.

    §6.3 asks for phrasing like "TD buy setup on bar 8 of 9 · Daily" and
    "Z-score -1.7 vs 50D". Only components that actually scored appear —
    a zero-point component is not "firing".
    """
    out = [c.detail for c in components if c.points > 0]
    out.extend(daily_td.describe())
    out.extend(weekly_td.describe())
    # Preserve order while removing duplicates (a TD line can arrive from both
    # the component detail and the TDState description).
    seen: set[str] = set()
    deduped = []
    for line in out:
        if line not in seen:
            seen.add(line)
            deduped.append(line)
    return deduped
