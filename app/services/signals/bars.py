"""Technical Signals Layer — bar containers, weekly aggregation, staleness.

Implements the data-shape half of `docs/technical-signals-layer-scope.md` §3.3:

  * "Daily OHLCV, ~300 bars history per ticker"
  * "weekly bars aggregated locally"        → to_weekly()
  * "the run flags 'stale' ... never scores silently on old data" → is_stale()

Design note — why plain dataclasses and lists rather than a pandas DataFrame:
the indicator and TD Sequential engines are bar-indexed and order-sensitive
(DeMark counts walk forward one bar at a time comparing bar[i] to bar[i-4]).
Expressing that over a list of immutable bars is far easier to read and to test
against hand-computed fixtures than the equivalent vectorised pandas. pandas is
still used at the provider boundary, where yfinance hands us a DataFrame.

Ordering contract, relied on by every downstream module: `BarSeries.bars` is
always sorted **oldest → newest**, so `bars[-1]` is the most recent bar and
`bars[i-4]` is four bars earlier. Constructors enforce this.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.services.signals.config import STALE_AFTER_DAYS


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. Frozen — indicators must never mutate their input.

    `volume` is nullable because some instruments in scope (§1 mentions
    commodities and crypto alongside equities and ETFs) arrive from feeds with
    no meaningful volume. Every volume-dependent path — RVOL (§4), the §5
    volume component — must degrade to "no signal" rather than assume zero.
    Zero volume would read as a valid observation and drag an RVOL average
    down; None correctly reads as "unknown".
    """

    bar_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bar_date"] = self.bar_date.isoformat()
        return d


@dataclass
class BarSeries:
    """An ordered series of bars for one symbol on one timeframe."""

    symbol: str
    timeframe: str          # 'daily' | 'weekly'
    bars: list[Bar]
    fetched_at: datetime | None = None

    def __post_init__(self) -> None:
        # Enforce the ordering contract at construction rather than trusting
        # callers. Providers, the DB, and weekly aggregation all build series,
        # and a silently mis-ordered series would produce plausible-looking
        # but wrong DeMark counts — the exact failure mode §3.1's acceptance
        # test exists to catch.
        self.bars = sorted(self.bars, key=lambda b: b.bar_date)

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def last(self) -> Bar | None:
        return self.bars[-1] if self.bars else None

    @property
    def closes(self) -> list[float]:
        return [b.close for b in self.bars]

    @property
    def highs(self) -> list[float]:
        return [b.high for b in self.bars]

    @property
    def lows(self) -> list[float]:
        return [b.low for b in self.bars]

    @property
    def volumes(self) -> list[float | None]:
        return [b.volume for b in self.bars]

    def tail(self, n: int) -> BarSeries:
        """The most recent `n` bars, preserving order."""
        return BarSeries(self.symbol, self.timeframe, self.bars[-n:] if n > 0 else [], self.fetched_at)

    def is_stale(self, *, as_of: date | None = None, max_age_days: int = STALE_AFTER_DAYS) -> bool:
        """True when the newest bar is too old to score on — scope doc §3.3.

        §3.3: "If the data API is down, the run flags 'stale' on the dashboard
        and skips alerts — never scores silently on old data."

        An empty series is stale by definition: no data is strictly worse than
        old data, and returning False here would let the engine score a symbol
        it knows nothing about. This is the "missing data = confident neutral
        signal" bug the 2026-06-21 audit found elsewhere in this codebase; it
        is not repeated here.

        Uses calendar days, not trading days, deliberately: the default of 3
        (config.STALE_AFTER_DAYS) already absorbs a Fri→Mon weekend plus one
        public holiday, and a trading-day calculation would need an exchange
        calendar per asset class — crypto trades weekends, equities don't.
        """
        if not self.bars:
            return True
        today = as_of or datetime.now(timezone.utc).date()
        age_days = (today - self.bars[-1].bar_date).days
        return age_days > max_age_days

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": [b.to_dict() for b in self.bars],
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }


def _week_key(d: date) -> date:
    """The Monday of the ISO week containing `d`.

    Weekly bars are labelled by their week-start (Monday) so the label is
    stable regardless of which weekday the series happens to end on — a partial
    current week keeps the same key as it fills in through Friday, which means
    re-running the engine mid-week updates that week's bar in place rather than
    creating a second one.
    """
    return d - timedelta(days=d.weekday())


def to_weekly(daily: BarSeries) -> BarSeries:
    """Aggregate daily bars into weekly bars — scope doc §3.3.

    §3.3: "weekly bars aggregated locally". §4 needs weekly for TD confluence
    and RSI; §5 scores "Weekly 9/13 confluence: +5".

    Standard OHLC aggregation over each ISO week (Mon-Sun):
        open   = first day's open
        high   = max of the week's highs
        low    = min of the week's lows
        close  = last day's close
        volume = sum of the week's volumes

    Two decisions worth stating explicitly:

    1. **The final week is included even when partial.** The engine runs
       nightly (§3.3, ~21:30 UK) and must produce a live weekly TD count on a
       Wednesday, not one frozen at last Friday's close. A partial week is the
       correct real-time view — it is exactly what a weekly chart shows
       intraweek. The consequence is that the newest weekly bar can change
       until the week closes, which is inherent to weekly analysis, not a bug.

    2. **Volume sums to None if any day is missing volume.** Summing a week
       with two known days and three unknowns would understate it and corrupt
       weekly RVOL. Unknown-in, unknown-out.
    """
    if not daily.bars:
        return BarSeries(daily.symbol, "weekly", [], daily.fetched_at)

    # dict preserves insertion order (Python 3.7+) and daily.bars is sorted
    # oldest→newest by the BarSeries contract, so weeks come out in order.
    buckets: dict[date, list[Bar]] = {}
    for bar in daily.bars:
        buckets.setdefault(_week_key(bar.bar_date), []).append(bar)

    weekly_bars: list[Bar] = []
    for week_start, week_bars in buckets.items():
        vols = [b.volume for b in week_bars]
        total_volume = None if any(v is None for v in vols) else sum(v for v in vols if v is not None)
        weekly_bars.append(
            Bar(
                bar_date=week_start,
                open=week_bars[0].open,
                high=max(b.high for b in week_bars),
                low=min(b.low for b in week_bars),
                close=week_bars[-1].close,
                volume=total_volume,
            )
        )

    return BarSeries(daily.symbol, "weekly", weekly_bars, daily.fetched_at)
