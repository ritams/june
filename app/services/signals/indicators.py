"""Technical Signals Layer — native indicator stack.

Implements the non-DeMark half of the `docs/technical-signals-layer-scope.md`
§4 indicator table:

    | Regime Composite | Trend/regime state: price vs 21 EMA / 50 / 200 SMA
    |                    structure, 50>200 slope, 20-day ROC, ATR trend →
    |                    Bullish / Neutral / Bearish + flip events
    | EMA/SMA structure (21 EMA, 50 & 200 SMA) | Trend context and pullback zones
    | ATR-normalised stretch + z-score vs 50D  | Mean-reversion deviation
    | RSI(14), daily + weekly confluence       | Pullback entries; divergence for exits
    | Relative volume (RVOL)                   | Capitulation/confirmation filter
    | 200-day regime gate (weekly confirmed)   | Not scored — caps counter-trend at 60

§4 closes with "Nothing else in v1." — adding an indicator means editing the
scope doc first, then config.py, then here.

═══════════════════════════════════════════════════════════════════════════════
THE WARMUP CONTRACT — read before using any function here
═══════════════════════════════════════════════════════════════════════════════

Every series function returns a list the same length as its input, with `None`
in the warmup region where the indicator is not yet defined (the first 13 bars
of an RSI(14), the first 199 of a 200 SMA, and so on).

`None` means **"not computable"**, never "zero" and never "neutral". This is
load-bearing. The 2026-06-21 audit of this codebase found a recurring bug where
missing data was silently converted into a confident neutral reading — a
scoring engine that treats "I don't know" as "nothing is happening" produces
signals it has no basis for. Every consumer in scoring.py must branch on None
and award no points rather than defaulting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.signals.bars import BarSeries
from app.services.signals.config import (
    ATR_PERIOD,
    ATR_TREND_WINDOW,
    EMA_FAST,
    ROC_PERIOD,
    RSI_PERIOD,
    RVOL_LOOKBACK,
    SMA_MID,
    SMA_SLOPE_WINDOW,
    SMA_SLOW,
    ZSCORE_WINDOW,
)


# ── Moving averages ──────────────────────────────────────────────────────────


def sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average. None for the first `period - 1` bars.

    Computed with a running sum rather than re-summing each window: 300 bars ×
    50 symbols × several periods is enough arithmetic that O(n) beats O(n·p)
    noticeably on the nightly run, and the on-demand lookup (§6.2) has a hard
    sub-2-second budget.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with the SMA of the first `period` bars.

    SMA seeding (rather than starting from the first value) is the standard
    convention and is what charting platforms use, which matters because §3.1
    requires our output to reconcile against TradingView. Starting from
    `values[0]` would converge to the same place but leave a visible offset for
    the first ~3 periods.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    multiplier = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * multiplier + prev
        out[i] = prev
    return out


# ── RSI ──────────────────────────────────────────────────────────────────────


def rsi(values: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder's RSI — §4: "RSI(14), daily + weekly confluence".

    Wilder's smoothing (not a simple average of gains/losses) is what every
    charting platform means by "RSI", so it is what §3.1's reconciliation
    requires. First value lands at index `period`.

    Edge case: a window with no losses gives a zero average loss and an
    infinite RS. RSI is defined as exactly 100 there (and 0 for no gains);
    returning inf or NaN would poison every downstream comparison.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if n <= period:
        return out

    gains, losses = [], []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, n):
        change = values[i] - values[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        # Wilder smoothing: previous average carries (period-1)/period weight.
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)

    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0   # flat series → neutral, not 100
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── ATR ──────────────────────────────────────────────────────────────────────


def true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[float | None]:
    """True Range. None on the first bar, which has no previous close."""
    out: list[float | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        out[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    return out


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = ATR_PERIOD,
) -> list[float | None]:
    """Wilder's ATR — §4: "ATR-normalised stretch".

    Same smoothing rationale as RSI: Wilder's is what platforms plot.
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    tr = true_range(highs, lows, closes)
    # TR is None at index 0, so the first full window spans indices 1..period.
    if n < period + 1:
        return out

    window = [t for t in tr[1 : period + 1] if t is not None]
    if len(window) < period:
        return out
    prev = sum(window) / period
    out[period] = prev
    for i in range(period + 1, n):
        t = tr[i]
        if t is None:
            continue
        prev = (prev * (period - 1) + t) / period
        out[i] = prev
    return out


# ── Stretch: z-score and ATR distance ────────────────────────────────────────


def rolling_zscore(values: list[float], window: int = ZSCORE_WINDOW) -> list[float | None]:
    """Z-score of each value against the trailing `window` — §4: "z-score vs 50D".

    Population standard deviation (divide by N, not N-1): the window is the
    reference distribution itself, not a sample drawn from a larger one. With
    N=50 the difference is under 1%, but it is a fixed convention worth naming
    so a future reconciliation against another tool has something to check.

    A zero-variance window (a perfectly flat 50 days — possible in a halted or
    pegged instrument) returns None rather than dividing by zero. "Unmeasurable
    deviation" is not the same as "no deviation".
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if window <= 1:
        return out
    for i in range(window - 1, n):
        win = values[i - window + 1 : i + 1]
        mean = sum(win) / window
        variance = sum((v - mean) ** 2 for v in win) / window
        if variance <= 0.0:
            continue
        out[i] = (values[i] - mean) / (variance ** 0.5)
    return out


def atr_distance(
    closes: list[float],
    anchor: list[float | None],
    atr_values: list[float | None],
) -> list[float | None]:
    """Distance from `anchor` in ATR units — §4: "ATR-normalised stretch".

    §5 scores "≥ 2.5 ATR below 21 EMA" for entries and "+2.5 ATR above 21 EMA"
    for exits, so `anchor` is the 21 EMA in practice.

    Sign convention: **positive means price is above the anchor**, negative
    below. §5's entry language ("2.5 ATR below") therefore tests <= -2.5, and
    the exit language ("+2.5 ATR above") tests >= +2.5. Normalising by ATR is
    what makes the measure comparable across asset classes (§4), which matters
    because the same watchlist holds equities and crypto.
    """
    out: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        a, atr_v = anchor[i], atr_values[i]
        if a is None or atr_v is None or atr_v <= 0.0:
            continue
        out[i] = (closes[i] - a) / atr_v
    return out


# ── Rate of change and relative volume ───────────────────────────────────────


def roc(values: list[float], period: int = ROC_PERIOD) -> list[float | None]:
    """Percent rate of change over `period` bars — §4 Regime Composite input."""
    out: list[float | None] = [None] * len(values)
    for i in range(period, len(values)):
        base = values[i - period]
        if base == 0:
            continue
        out[i] = (values[i] - base) / base * 100.0
    return out


def rvol(volumes: list[float | None], lookback: int = RVOL_LOOKBACK) -> list[float | None]:
    """Relative volume vs its own trailing average — §4: "Relative volume (RVOL)".

    §5 scores "RVOL >= 2 on signal day: 5 · RVOL >= 1.5: 3".

    Missing volumes (see Bar.volume — commodities and some crypto feeds report
    none) propagate as None for any window containing one. Substituting zero
    would depress the average and manufacture a high RVOL reading on the next
    real bar, which §5 would then score as capitulation volume that never
    happened.
    """
    n = len(volumes)
    out: list[float | None] = [None] * n
    for i in range(lookback, n):
        window = volumes[i - lookback : i]
        if any(v is None for v in window) or volumes[i] is None:
            continue
        avg = sum(v for v in window if v is not None) / lookback
        if avg <= 0.0:
            continue
        out[i] = volumes[i] / avg
    return out


# ── Divergence (§5: "Bullish divergence: 8", exit "weekly bearish divergence") ─


def _pivot_lows(values: list[float], left: int = 2, right: int = 2) -> list[int]:
    """Indices of local minima with `left`/`right` bars higher on each side."""
    out = []
    for i in range(left, len(values) - right):
        window = values[i - left : i + right + 1]
        if values[i] == min(window) and window.count(values[i]) == 1:
            out.append(i)
    return out


def _pivot_highs(values: list[float], left: int = 2, right: int = 2) -> list[int]:
    out = []
    for i in range(left, len(values) - right):
        window = values[i - left : i + right + 1]
        if values[i] == max(window) and window.count(values[i]) == 1:
            out.append(i)
    return out


def bullish_divergence(
    lows: list[float],
    rsi_values: list[float | None],
    lookback: int = 60,
) -> bool:
    """Price makes a lower low while RSI makes a higher low — §5 entry component.

    Compares the two most recent pivot lows within `lookback` bars. Requires a
    confirmed pivot (2 bars on each side), so the signal appears 2 bars after
    the low itself — the standard cost of confirmation, and preferable to
    firing on a low that is still forming.

    Returns False rather than None when undecidable: divergence is a
    binary "is this pattern present", and §5 awards points only on True, so an
    unknown correctly awards nothing.
    """
    n = len(lows)
    if n < 10:
        return False
    start = max(0, n - lookback)
    window_lows = lows[start:]
    window_rsi = rsi_values[start:]

    pivots = _pivot_lows(window_lows)
    if len(pivots) < 2:
        return False
    prev_i, last_i = pivots[-2], pivots[-1]
    prev_rsi, last_rsi = window_rsi[prev_i], window_rsi[last_i]
    if prev_rsi is None or last_rsi is None:
        return False
    return window_lows[last_i] < window_lows[prev_i] and last_rsi > prev_rsi


def bearish_divergence(
    highs: list[float],
    rsi_values: list[float | None],
    lookback: int = 60,
) -> bool:
    """Price makes a higher high while RSI makes a lower high — §5 exit component."""
    n = len(highs)
    if n < 10:
        return False
    start = max(0, n - lookback)
    window_highs = highs[start:]
    window_rsi = rsi_values[start:]

    pivots = _pivot_highs(window_highs)
    if len(pivots) < 2:
        return False
    prev_i, last_i = pivots[-2], pivots[-1]
    prev_rsi, last_rsi = window_rsi[prev_i], window_rsi[last_i]
    if prev_rsi is None or last_rsi is None:
        return False
    return window_highs[last_i] > window_highs[prev_i] and last_rsi < prev_rsi


# ── Regime Composite (§4) ────────────────────────────────────────────────────

# §3.1: the Regime Composite replaces Chameleon, which "cannot be replicated
# (proprietary)", at the same weight in §5's scoring table (25 points).
#
# §4 names six inputs: price vs 21 EMA, price vs 50 SMA, price vs 200 SMA,
# 50>200 slope, 20-day ROC, and ATR trend. We score each as +1/0/-1 and sum to
# a composite in [-6, +6], then band it.
#
# Thresholds: |composite| >= 3 is a regime call, otherwise Neutral. Requiring
# half the inputs to agree before declaring a regime is what keeps §5's flip
# events (worth up to 25 points) from firing on one input wobbling.
REGIME_BULLISH_THRESHOLD = 3
REGIME_BEARISH_THRESHOLD = -3

REGIME_BULLISH = "Bullish"
REGIME_NEUTRAL = "Neutral"
REGIME_BEARISH = "Bearish"


@dataclass
class RegimeState:
    """Regime Composite as of one bar, plus flip bookkeeping for §5 freshness."""

    regime: str | None                        # None = not computable (warmup)
    composite: int | None                     # -6..+6
    inputs: dict[str, int | None] = field(default_factory=dict)
    previous_regime: str | None = None        # regime immediately before the last flip
    bars_since_flip: int | None = None        # None = no flip in loaded history

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def flip_label(self) -> str | None:
        """e.g. "Bearish→Neutral" — the form §5's scoring table uses."""
        if self.previous_regime is None or self.regime is None:
            return None
        return f"{self.previous_regime}→{self.regime}"


def regime_series(series: BarSeries) -> list[RegimeState]:
    """Regime Composite for every bar — §4.

    Returns one RegimeState per input bar, with `regime=None` through the
    warmup region (the 200 SMA needs 200 bars, so a 300-bar series yields ~100
    scored bars). Consumers must treat None as "unknown", per the module's
    warmup contract.

    Computing the whole series rather than just the last bar is what makes
    flip detection possible, and flips are where most of §5's regime points
    live ("Bearish→Neutral flip: 12 · Neutral→Bullish flip: 25").
    """
    closes = series.closes
    highs = series.highs
    lows = series.lows
    n = len(closes)

    ema21 = ema(closes, EMA_FAST)
    sma50 = sma(closes, SMA_MID)
    sma200 = sma(closes, SMA_SLOW)
    roc20 = roc(closes, ROC_PERIOD)
    atr14 = atr(highs, lows, closes, ATR_PERIOD)

    states: list[RegimeState] = []
    for i in range(n):
        inputs: dict[str, int | None] = {
            "price_vs_ema21": _sign_vs(closes[i], ema21[i]),
            "price_vs_sma50": _sign_vs(closes[i], sma50[i]),
            "price_vs_sma200": _sign_vs(closes[i], sma200[i]),
            "sma50_vs_sma200_slope": _slope_signal(sma50, sma200, i),
            "roc20": _sign_of(roc20[i]),
            "atr_trend": _atr_trend_signal(atr14, i),
        }
        known = [v for v in inputs.values() if v is not None]

        # Require every input to be available before calling a regime. A
        # composite built from 3 of 6 inputs is not comparable to one built
        # from 6, and §5 pays up to 25 points for a flip — too much to award on
        # a partially-warmed measure.
        if len(known) < len(inputs):
            states.append(RegimeState(regime=None, composite=None, inputs=inputs))
            continue

        composite = sum(known)
        if composite >= REGIME_BULLISH_THRESHOLD:
            regime = REGIME_BULLISH
        elif composite <= REGIME_BEARISH_THRESHOLD:
            regime = REGIME_BEARISH
        else:
            regime = REGIME_NEUTRAL
        states.append(RegimeState(regime=regime, composite=composite, inputs=inputs))

    _annotate_flips(states)
    return states


def _annotate_flips(states: list[RegimeState]) -> None:
    """Fill in previous_regime / bars_since_flip on each state, in place.

    A "flip" is a change in the banded regime label, not a change in the
    underlying composite — the composite moving 4→5 is not an event, but
    3→2 (Bullish→Neutral) is. §5 scores flips, not composite drift.
    """
    last_regime: str | None = None
    last_flip_index: int | None = None
    prior_regime: str | None = None

    for i, st in enumerate(states):
        if st.regime is None:
            continue
        if last_regime is None:
            # First computable bar establishes a baseline; it is not a flip,
            # because we have no idea what the regime was before it.
            last_regime = st.regime
        elif st.regime != last_regime:
            prior_regime = last_regime
            last_regime = st.regime
            last_flip_index = i
        st.previous_regime = prior_regime
        st.bars_since_flip = None if last_flip_index is None else i - last_flip_index


def _sign_vs(price: float, level: float | None) -> int | None:
    """+1 if price is above the level, -1 if below, 0 if exactly equal."""
    if level is None:
        return None
    if price > level:
        return 1
    if price < level:
        return -1
    return 0


def _sign_of(value: float | None) -> int | None:
    if value is None:
        return None
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _slope_signal(sma50: list[float | None], sma200: list[float | None], i: int) -> int | None:
    """§4's "50>200 slope" input.

    Combines two things the phrase implies: the 50 being above the 200 (a
    golden-cross state) and that relationship *improving* over
    SMA_SLOPE_WINDOW bars. Both must agree for a ±1; a golden cross that is
    narrowing scores 0, which is the point of reading it as a slope rather
    than a level.
    """
    j = i - SMA_SLOPE_WINDOW
    if j < 0:
        return None
    now_50, now_200 = sma50[i], sma200[i]
    then_50, then_200 = sma50[j], sma200[j]
    if None in (now_50, now_200, then_50, then_200):
        return None
    spread_now = now_50 - now_200
    spread_then = then_50 - then_200
    if spread_now > 0 and spread_now > spread_then:
        return 1
    if spread_now < 0 and spread_now < spread_then:
        return -1
    return 0


def _atr_trend_signal(atr_values: list[float | None], i: int) -> int | None:
    """§4's "ATR trend" input — is volatility expanding or contracting?

    Sign convention: **contracting volatility scores +1**, expanding scores -1.
    Rationale: this composite measures regime health, and sustained uptrends
    are characteristically low-and-falling volatility while breakdowns are
    volatility expansions. Reading rising ATR as bullish would make a crash
    look like a strengthening regime.

    A 5% dead band around flat avoids flip-flopping the composite on noise.
    """
    j = i - ATR_TREND_WINDOW
    if j < 0:
        return None
    now, then = atr_values[i], atr_values[j]
    if now is None or then is None or then <= 0:
        return None
    change = (now - then) / then
    if change < -0.05:
        return 1
    if change > 0.05:
        return -1
    return 0


# ── Bundled snapshot ─────────────────────────────────────────────────────────


@dataclass
class IndicatorSnapshot:
    """Every §4 indicator evaluated at the final bar of a series.

    This is the object §5's scoring model consumes. Any field may be None per
    the warmup contract — scoring must branch, never default.
    """

    symbol: str
    timeframe: str
    close: float
    ema21: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    sma200_rising: bool | None = None
    rsi: float | None = None
    rsi_prev: float | None = None       # for §5's "RSI < 30 turning up"
    atr: float | None = None
    zscore: float | None = None
    atr_stretch: float | None = None    # +ve above the 21 EMA, -ve below
    rvol: float | None = None
    roc20: float | None = None
    regime: RegimeState | None = None
    bullish_divergence: bool = False
    bearish_divergence: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["regime"] = self.regime.to_dict() if self.regime else None
        return d


def snapshot(series: BarSeries) -> IndicatorSnapshot:
    """Evaluate the full §4 stack at the last bar of `series`.

    Raises ValueError on an empty series — an empty series is a caller bug
    (engine.py checks staleness first), not a runtime condition to absorb.
    """
    if not series.bars:
        raise ValueError(f"cannot compute indicators on an empty series for {series.symbol!r}")

    closes, highs, lows = series.closes, series.highs, series.lows
    i = len(closes) - 1

    ema21 = ema(closes, EMA_FAST)
    sma50 = sma(closes, SMA_MID)
    sma200 = sma(closes, SMA_SLOW)
    rsi_vals = rsi(closes, RSI_PERIOD)
    atr_vals = atr(highs, lows, closes, ATR_PERIOD)
    z_vals = rolling_zscore(closes, ZSCORE_WINDOW)
    stretch_vals = atr_distance(closes, ema21, atr_vals)
    rvol_vals = rvol(series.volumes, RVOL_LOOKBACK)
    roc_vals = roc(closes, ROC_PERIOD)
    regimes = regime_series(series)

    return IndicatorSnapshot(
        symbol=series.symbol,
        timeframe=series.timeframe,
        close=closes[i],
        ema21=ema21[i],
        sma50=sma50[i],
        sma200=sma200[i],
        sma200_rising=_is_rising(sma200, i),
        rsi=rsi_vals[i],
        rsi_prev=rsi_vals[i - 1] if i >= 1 else None,
        atr=atr_vals[i],
        zscore=z_vals[i],
        atr_stretch=stretch_vals[i],
        rvol=rvol_vals[i],
        roc20=roc_vals[i],
        regime=regimes[i] if regimes else None,
        bullish_divergence=bullish_divergence(lows, rsi_vals),
        bearish_divergence=bearish_divergence(highs, rsi_vals),
    )


def _is_rising(values: list[float | None], i: int, window: int = SMA_SLOPE_WINDOW) -> bool | None:
    """Is `values` higher now than `window` bars ago? — §5's "rising 200D".

    §5 uses this twice and they pull in opposite directions: "Above rising
    200D: 8" awards entry points, and the regime gate caps entries "below a
    falling 200D". None (not yet computable) must therefore award no points AND
    apply no cap — see scoring.py, which treats an unknown 200D as
    ungated rather than guessing.
    """
    j = i - window
    if j < 0 or i >= len(values):
        return None
    now, then = values[i], values[j]
    if now is None or then is None:
        return None
    return now > then
