"""Technical Signals Layer — historical replay (scope doc §5, Phase 4).

§5: "Phase 4 (optional): because the stack is fully native, replay the engine
over historical OHLCV and report hit-rates per band per asset class, then tune."

The replay walks a symbol's history bar by bar, scoring each bar using **only the
bars up to and including it**, then measures what happened next. Because
`engine._evaluate()` is a pure function of a `BarSeries`, that is literally
`_evaluate(series.tail(i))` — the backtest runs the identical scoring code as the
nightly job and the §6 lookup, which is what makes its output meaningful.

═══════════════════════════════════════════════════════════════════════════════
NO LOOKAHEAD
═══════════════════════════════════════════════════════════════════════════════

The single way a backtest lies is by letting the scorer see the future. Two
guards, both structural rather than conventional:

  * Each bar is scored on `bars[:i+1]` — a fresh slice ending at that bar. There
    is no path by which bar i+1 can influence bar i's score, because it is not in
    the object being scored.
  * Forward returns are computed from `bars[i+1:]` — strictly after the scored
    bar. Entry is at the **next bar's open**, never the signal bar's close, since
    a daily-close signal cannot be acted on at that same close.

`test_no_lookahead_in_replay` pins the first; the entry convention is asserted in
`test_entry_is_the_next_bar_open`.

═══════════════════════════════════════════════════════════════════════════════
TWO DECISIONS THE SCOPE DOC LEAVES OPEN
═══════════════════════════════════════════════════════════════════════════════

§5 says "hit-rates" without defining what counts as a hit. Both choices below are
defaults, both are parameters, and both change the answer — so they are stated
here rather than buried.

  1. **Holding period.** Default 20 trading days (~1 month). Rationale: §5's own
     freshness curve values a TD/regime event for at most 13 bars, so a horizon
     materially longer than that would be measuring something the score was never
     claiming to predict. Configurable via `horizons`, and the default run
     reports 5/10/20/60 so the sensitivity is visible rather than assumed.

  2. **What counts as a hit.** Default: a positive return over the horizon,
     measured next-open to horizon-close. Deliberately *not* benchmarked against
     the asset's own drift — in a bull market a coin flip clears 50%, so raw
     hit-rate flatters every band equally. That is why `edge_vs_baseline` is
     reported alongside: the same statistic computed over **all** bars of the same
     symbol, so a band is judged against "what would holding at random have
     given me", which is the only comparison that can justify acting on a signal.

An exit rule driven by §5's exit score is deliberately not modelled. Doing it
properly means position state, and a fixed horizon answers the actual Phase 4
question ("is a Prime entry better than a Watch entry?") without that machinery.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from app.services.signals import store
from app.services.signals.bars import BarSeries
from app.services.signals.config import BAND_ORDER, HISTORY_BARS
from app.services.signals.engine import MIN_BARS_TO_SCORE, InsufficientHistory, _evaluate
from app.services.signals.providers import ProviderError, classify_asset, get_provider


logger = logging.getLogger(__name__)


# §5 Phase 4 defaults — see the module docstring for why these and not others.
DEFAULT_HORIZONS = (5, 10, 20, 60)
PRIMARY_HORIZON = 20

# Replaying every bar of a 300-bar series means ~240 full indicator passes per
# symbol. Stepping lets a broad sweep trade resolution for wall-clock; step=1 is
# the honest default and what a tuning run should use.
DEFAULT_STEP = 1


@dataclass
class Observation:
    """One scored historical bar and what happened after it."""

    symbol: str
    asset_class: str
    bar_date: date
    entry_score: int
    entry_band: str
    exit_score: int
    counter_trend: bool
    regime: str | None
    # horizon (bars) → forward return in percent, next-open to horizon-close.
    forward_returns: dict[int, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bar_date"] = self.bar_date.isoformat()
        d["forward_returns"] = {str(k): round(v, 4) for k, v in self.forward_returns.items()}
        return d


@dataclass
class BandStats:
    """Aggregated performance for one band (optionally within one asset class)."""

    band: str
    asset_class: str | None
    count: int
    hit_rate: float                 # % of observations with a positive return
    mean_return: float              # %
    median_return: float            # %
    baseline_hit_rate: float        # same statistic over ALL bars of the same symbols
    baseline_mean_return: float
    horizon: int

    @property
    def edge_vs_baseline(self) -> float:
        """Percentage points of hit-rate over holding at random.

        The number that actually matters. A 62% hit-rate means nothing if the
        universe returned 60% of the time over the same window.
        """
        return self.hit_rate - self.baseline_hit_rate

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["edge_vs_baseline"] = round(self.edge_vs_baseline, 2)
        for k in ("hit_rate", "mean_return", "median_return", "baseline_hit_rate", "baseline_mean_return"):
            d[k] = round(d[k], 2)
        return d


def replay_symbol(
    series: BarSeries,
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    step: int = DEFAULT_STEP,
) -> list[Observation]:
    """Score every bar of `series` using only prior data, and measure what followed.

    Returns one Observation per scoreable bar that has enough forward history for
    at least the shortest horizon. Bars too close to the end of the series are
    dropped rather than measured over a truncated window — a partial horizon
    would silently mix 3-day and 20-day outcomes into one statistic.
    """
    bars = series.bars
    n = len(bars)
    asset_class = classify_asset(series.symbol)
    max_horizon = max(horizons)

    observations: list[Observation] = []
    # Start once there is enough history to score, stop once there is not enough
    # future left to measure the shortest horizon.
    last_scoreable = n - min(horizons) - 1
    for i in range(MIN_BARS_TO_SCORE, last_scoreable + 1, step):
        window = BarSeries(series.symbol, "daily", bars[: i + 1], series.fetched_at)
        try:
            # `as_of` pins staleness to the bar being scored — otherwise every
            # historical bar would evaluate as stale against today's date and
            # the whole replay would be flagged.
            evaluation = _evaluate(window, as_of=bars[i].bar_date)
        except InsufficientHistory:
            continue
        except Exception as exc:
            logger.warning("Replay: scoring failed for %s at %s: %s", series.symbol, bars[i].bar_date, exc)
            continue

        forward = _forward_returns(bars, i, horizons)
        if not forward:
            continue

        observations.append(
            Observation(
                symbol=series.symbol,
                asset_class=asset_class,
                bar_date=bars[i].bar_date,
                entry_score=evaluation.entry.score,
                entry_band=evaluation.entry.band,
                exit_score=evaluation.exit.score,
                counter_trend=evaluation.entry.counter_trend,
                regime=evaluation.regime,
                forward_returns=forward,
            )
        )
    return observations


def _forward_returns(bars: list, i: int, horizons: tuple[int, ...]) -> dict[int, float]:
    """Percent return from the NEXT bar's open to each horizon's close.

    Next-open entry, not signal-close: the engine scores on the daily close
    (§3.3), so the earliest a human could act is the following session. Entering
    at the signal bar's close would book a free gap and inflate every result.
    """
    entry_index = i + 1
    if entry_index >= len(bars):
        return {}
    entry_price = bars[entry_index].open
    if entry_price <= 0:
        return {}

    out: dict[int, float] = {}
    for h in horizons:
        exit_index = entry_index + h - 1
        if exit_index >= len(bars):
            continue      # not enough future — drop rather than truncate
        out[h] = (bars[exit_index].close - entry_price) / entry_price * 100.0
    return out


def aggregate(
    observations: list[Observation],
    *,
    horizon: int = PRIMARY_HORIZON,
    by_asset_class: bool = True,
) -> list[BandStats]:
    """§5's "hit-rates per band per asset class".

    The baseline is computed within the same grouping, so a band is compared
    against its own universe — a Prime signal on crypto is judged against crypto's
    unconditional behaviour, not against equities'.
    """
    usable = [o for o in observations if horizon in o.forward_returns]
    if not usable:
        return []

    groups: dict[tuple[str, str | None], list[Observation]] = defaultdict(list)
    baselines: dict[str | None, list[float]] = defaultdict(list)

    for obs in usable:
        key_class = obs.asset_class if by_asset_class else None
        groups[(obs.entry_band, key_class)].append(obs)
        baselines[key_class].append(obs.forward_returns[horizon])

    stats: list[BandStats] = []
    for (band, key_class), items in groups.items():
        returns = [o.forward_returns[horizon] for o in items]
        base = baselines[key_class]
        stats.append(
            BandStats(
                band=band,
                asset_class=key_class,
                count=len(items),
                hit_rate=_pct_positive(returns),
                mean_return=sum(returns) / len(returns),
                median_return=_median(returns),
                baseline_hit_rate=_pct_positive(base),
                baseline_mean_return=sum(base) / len(base),
                horizon=horizon,
            )
        )

    # Strongest band first, then asset class, so the report reads top-down.
    stats.sort(key=lambda s: (BAND_ORDER.index(s.band) if s.band in BAND_ORDER else -1,
                              s.asset_class or ""), reverse=True)
    return stats


def _pct_positive(values: list[float]) -> float:
    return 100.0 * sum(1 for v in values if v > 0) / len(values) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def run(
    symbols: list[str] | None = None,
    *,
    bars: int = HISTORY_BARS,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    horizon: int = PRIMARY_HORIZON,
    step: int = DEFAULT_STEP,
) -> dict[str, Any]:
    """Full §5 Phase 4 replay over the watchlist (or a given symbol list).

    **On history depth:** the default `bars` is HISTORY_BARS (300, ~14 months),
    which is what §3.3 specifies for live scoring but is thin for tuning — Prime
    is rare by design, so 14 months across a small watchlist may yield too few
    Prime observations to conclude anything. Pass a larger `bars` (the yfinance
    period ladder reaches `max`) for a real tuning run. `sample_warning` in the
    result flags bands whose sample is too small to act on.
    """
    targets = symbols or [w["symbol"] for w in store.list_watchlist()]
    if not targets:
        return {"error": "no symbols — watchlist is empty"}

    provider = get_provider()
    all_obs: list[Observation] = []
    failed: dict[str, str] = {}

    for symbol in targets:
        try:
            series = provider.daily_bars(symbol, bars)
        except ProviderError as exc:
            failed[symbol] = str(exc)
            continue
        if len(series) < MIN_BARS_TO_SCORE + min(horizons) + 1:
            failed[symbol] = f"only {len(series)} bars — too short to replay"
            continue
        logger.info("Replaying %s over %d bars", symbol, len(series))
        all_obs.extend(replay_symbol(series, horizons=horizons, step=step))

    by_class = aggregate(all_obs, horizon=horizon, by_asset_class=True)
    overall = aggregate(all_obs, horizon=horizon, by_asset_class=False)

    # §5's tuning question is "does a higher band actually predict better
    # outcomes?" — surfaced directly rather than left for the reader to compute.
    monotonic = _is_monotonic(overall)

    return {
        "symbols": targets,
        "failed": failed,
        "observations": len(all_obs),
        "horizon": horizon,
        "horizons_measured": list(horizons),
        "entry_convention": "next bar open → horizon close",
        "hit_definition": "forward return > 0, compared against the same universe's unconditional rate",
        "by_band": [s.to_dict() for s in overall],
        "by_band_and_asset_class": [s.to_dict() for s in by_class],
        "bands_are_monotonic": monotonic,
        "sample_warnings": [
            f"{s.band}{'/' + s.asset_class if s.asset_class else ''}: only {s.count} observations"
            for s in by_class + overall if s.count < 30
        ],
        "sensitivity": {
            str(h): [s.to_dict() for s in aggregate(all_obs, horizon=h, by_asset_class=False)]
            for h in horizons
        },
    }


def _is_monotonic(overall: list[BandStats]) -> bool | None:
    """Does hit-rate increase with band strength? The core §5 tuning question.

    Returns None when fewer than two bands have a usable sample — "we cannot
    tell" rather than a misleading True.
    """
    usable = [s for s in overall if s.count >= 30 and s.band in BAND_ORDER]
    if len(usable) < 2:
        return None
    ordered = sorted(usable, key=lambda s: BAND_ORDER.index(s.band))
    rates = [s.hit_rate for s in ordered]
    return all(a <= b for a, b in zip(rates, rates[1:]))
