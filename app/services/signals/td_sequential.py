"""Technical Signals Layer — native TD Sequential (DeMark).

Implements `docs/technical-signals-layer-scope.md` §3.1:

    "TD Sequential → replicated natively. DeMark's methodology is public:
     Buy/Sell Setup (9 consecutive closes vs the close 4 bars earlier),
     Countdown to 13, setup perfection, TDST levels — all computable from
     OHLCV. Daily + weekly counts for every ticker, every run, and for any
     searched ticker on demand."

═══════════════════════════════════════════════════════════════════════════════
READ THIS BEFORE CHANGING ANYTHING IN THIS FILE
═══════════════════════════════════════════════════════════════════════════════

§3.1 sets a hard acceptance test:

    "native counts must match the paid TradingView indicator on 10 reference
     charts (Fred supplies screenshots) before Phase 1 signs off."

That test exists because DeMark's *core* rules are public and unambiguous, but
several *edge* rules are not — they vary between DeMark's own books, between
TradingView implementations, and between vendors. Where a rule is ambiguous
this module:

  1. implements the most widely documented reading,
  2. exposes it as a flag on `TDConfig` rather than hardcoding it,
  3. and marks it below with **[AMBIGUOUS]** plus what the alternatives are.

Every **[AMBIGUOUS]** decision is a candidate explanation if a reference chart
disagrees. Work that list before assuming the core counting logic is wrong.

Rules implemented (unambiguous, from DeMark's published methodology):

  * **Price flip.** A Buy Setup begins on a bearish price flip: the current
    close is below the close 4 bars earlier, and the previous close was above
    *its* close 4 bars earlier. Mirrored for Sell Setup.
  * **Setup.** 9 consecutive closes below (buy) / above (sell) the close 4 bars
    earlier. Any interruption resets the count to zero.
  * **Setup perfection.** A Buy Setup is perfected when the low of bar 8 or
    bar 9 is at or below the lows of bars 6 and 7. Mirrored (highs) for sell.
  * **TDST.** A completed Buy Setup projects TDST *resistance* at the highest
    high of bars 1-9; a completed Sell Setup projects TDST *support* at the
    lowest low of bars 1-9.
  * **Countdown.** After a completed Buy Setup, bars qualify when the close is
    at or below the low 2 bars earlier. Qualifying bars need not be
    consecutive. Counts to 13. Mirrored (close >= high 2 bars earlier) for sell.
  * **Bar 13 deferral.** The 13th countdown bar must additionally have its low
    at or below the *close of countdown bar 8* (buy). If the close qualifies
    but that condition fails, the count stays at 12 and waits — the standard
    "deferred 13" behaviour.

Terminology note: "bar 8" in the deferral rule means the 8th *countdown* bar,
not the 8th setup bar. Conflating the two is the single most common
implementation error in public TD Sequential code and produces counts that look
right most of the time and diverge exactly at 13s.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from app.services.signals.bars import BarSeries


# Number of bars back used by the setup comparison. DeMark's constant: the
# close is compared to the close 4 bars earlier.
SETUP_LOOKBACK = 4

# Number of bars back used by the countdown comparison — close vs the low/high
# 2 bars earlier.
COUNTDOWN_LOOKBACK = 2

SETUP_LENGTH = 9
COUNTDOWN_LENGTH = 13


@dataclass(frozen=True)
class TDConfig:
    """Toggles for the **[AMBIGUOUS]** rules. Defaults are the most common reading.

    If a §3.1 reference chart disagrees with our counts, flip these one at a
    time before suspecting the core logic — that is exactly what they are for.
    """

    # **[AMBIGUOUS] — countdown recycling.**
    # DeMark describes "recycling": if a *new* setup completes in the same
    # direction while a countdown is already running, the countdown may restart.
    # The qualifying condition (the new setup must be of equal or greater
    # "magnitude" than the one that spawned the running countdown) is described
    # qualitatively and implemented inconsistently across vendors. Many
    # TradingView scripts — including the most widely used ones — simply do not
    # recycle at all.
    # Default False = do not recycle; a running countdown survives a new
    # same-direction setup. This matches the common TradingView behaviour.
    recycle_countdown_on_new_setup: bool = False

    # **[AMBIGUOUS] — TDST cancellation.**
    # Some readings cancel a running countdown when price closes back through
    # the TDST level projected by the setup that started it. Others treat TDST
    # purely as a support/resistance study with no effect on the count.
    # Default False = TDST is informational only (the §4 indicator table lists
    # "TDST levels" alongside the counts, not as a gate on them).
    cancel_countdown_on_tdst_break: bool = False

    # **[UNAMBIGUOUS but worth a flag]** — a completed *opposite*-direction
    # setup cancels a running countdown. This is consistently documented and
    # defaults to on; the flag exists so a reference-chart mismatch can isolate it.
    cancel_countdown_on_opposite_setup: bool = True


DEFAULT_TD_CONFIG = TDConfig()


@dataclass(frozen=True)
class TDEvent:
    """A completed TD milestone — a setup 9 or a countdown 13.

    Feeds two things: the §5 scoring components ("Daily 9 complete: 18",
    "Daily 13 countdown: 25"), and §5's event-freshness decay, which needs to
    know how many bars ago the event fired.
    """

    bar_index: int
    bar_date: date
    event_type: str          # 'setup_9' | 'countdown_13'
    direction: str           # 'buy' | 'sell'
    perfected: bool | None = None   # setup_9 only; None for countdown_13
    tdst: float | None = None       # setup_9 only — the level this setup projected

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bar_date"] = self.bar_date.isoformat()
        return d


@dataclass
class TDState:
    """TD Sequential state as of the final bar of a series.

    This is what the §5 scoring model reads and what §6.3's "Firing now" list
    renders in plain terms ("TD buy setup on bar 8 of 9 · Daily").
    """

    symbol: str
    timeframe: str                       # 'daily' | 'weekly'

    # In-progress setup. `setup_count` is 0 when no setup is running, else 1-9.
    setup_direction: str | None = None   # 'buy' | 'sell'
    setup_count: int = 0
    setup_complete: bool = False         # True on the bar a 9 completes
    setup_perfected: bool | None = None

    # In-progress countdown. `countdown_count` is 0-13.
    countdown_direction: str | None = None
    countdown_count: int = 0
    countdown_complete: bool = False     # True on the bar a 13 completes
    countdown_deferred: bool = False     # at 12, a close qualified but bar-13 low/high test failed

    # TDST levels projected by the most recent completed setups (§3.1, §4).
    tdst_resistance: float | None = None  # from the last completed *buy* setup
    tdst_support: float | None = None     # from the last completed *sell* setup

    events: list[TDEvent] = field(default_factory=list)

    # Bars elapsed since the most recent completion of each kind, per direction.
    # None = never happened in the loaded history. Drives §5 freshness decay.
    bars_since_buy_setup_9: int | None = None
    bars_since_sell_setup_9: int | None = None
    bars_since_buy_countdown_13: int | None = None
    bars_since_sell_countdown_13: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["events"] = [e.to_dict() for e in self.events]
        return d

    def describe(self) -> list[str]:
        """Plain-language state for the §6.3 "Firing now" list.

        §6.3 asks for phrasing like "TD buy setup on bar 8 of 9 · Daily".
        """
        tf = self.timeframe.capitalize()
        out: list[str] = []
        if self.setup_count and self.setup_direction:
            if self.setup_complete:
                perfect = "perfected" if self.setup_perfected else "unperfected"
                out.append(f"TD {self.setup_direction} setup 9 complete ({perfect}) · {tf}")
            else:
                out.append(f"TD {self.setup_direction} setup on bar {self.setup_count} of 9 · {tf}")
        if self.countdown_count and self.countdown_direction:
            if self.countdown_complete:
                out.append(f"TD {self.countdown_direction} countdown 13 complete · {tf}")
            elif self.countdown_deferred:
                out.append(f"TD {self.countdown_direction} countdown 12, bar 13 deferred · {tf}")
            else:
                out.append(f"TD {self.countdown_direction} countdown at {self.countdown_count} of 13 · {tf}")
        return out


def compute(series: BarSeries, config: TDConfig = DEFAULT_TD_CONFIG) -> TDState:
    """Walk `series` oldest→newest and return TD state as of the final bar.

    A forward walk with explicit state — rather than a vectorised
    implementation — is deliberate. DeMark is a state machine: countdowns
    depend on which setup spawned them, bar 13 depends on the close of
    countdown bar 8, and cancellations depend on events in the other direction.
    Expressing that as a readable loop is what makes it checkable against the
    §3.1 reference charts, which is the whole acceptance criterion.

    Returns a TDState with zeroed counts for a series too short to evaluate;
    it never raises on short input, because §6's on-demand lookup must degrade
    gracefully for a newly listed ticker rather than 500.
    """
    state = TDState(symbol=series.symbol, timeframe=series.timeframe)
    bars = series.bars
    n = len(bars)

    # Need at least SETUP_LOOKBACK + 1 bars to evaluate a single setup
    # comparison, and one more to evaluate the price flip that precedes it.
    if n < SETUP_LOOKBACK + 2:
        return state

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]

    # ── Running setup state ──────────────────────────────────────────────────
    setup_dir: str | None = None
    setup_count = 0
    setup_start: int | None = None      # index of setup bar 1

    # ── Running countdown state ──────────────────────────────────────────────
    cd_dir: str | None = None
    cd_count = 0
    cd_bar8_close: float | None = None  # close of *countdown* bar 8 — see module docstring
    cd_deferred = False
    cd_tdst: float | None = None        # TDST of the setup that spawned this countdown

    # Latest completion bookkeeping, converted to "bars since" at the end.
    last_idx: dict[str, int] = {}

    def _buy_cond(i: int) -> bool:
        """Setup comparison for a buy setup: close below the close 4 bars back."""
        return closes[i] < closes[i - SETUP_LOOKBACK]

    def _sell_cond(i: int) -> bool:
        return closes[i] > closes[i - SETUP_LOOKBACK]

    def _bearish_flip(i: int) -> bool:
        """Bar `i` is Buy Setup bar 1: buy condition now, sell condition previously.

        Strict inequality on both sides, per DeMark. An exactly-equal close
        (closes[i-1] == closes[i-5]) starts no setup. This is rare in equities
        but common in low-precision crypto and thinly traded instruments, which
        §1 includes — hence the explicit note rather than a silent `>=`.
        """
        return _buy_cond(i) and closes[i - 1] > closes[i - 1 - SETUP_LOOKBACK]

    def _bullish_flip(i: int) -> bool:
        return _sell_cond(i) and closes[i - 1] < closes[i - 1 - SETUP_LOOKBACK]

    # Start at the first index where both bar i and bar i-1 have a valid
    # 4-bars-back comparison.
    for i in range(SETUP_LOOKBACK + 1, n):

        # ── 1. Setup counting ────────────────────────────────────────────────
        if setup_dir == "buy" and _buy_cond(i):
            setup_count += 1
        elif setup_dir == "sell" and _sell_cond(i):
            setup_count += 1
        else:
            # The run broke (or none was running). A new setup may begin on
            # this very bar if it is a price flip.
            setup_dir, setup_count, setup_start = None, 0, None
            if _bearish_flip(i):
                setup_dir, setup_count, setup_start = "buy", 1, i
            elif _bullish_flip(i):
                setup_dir, setup_count, setup_start = "sell", 1, i

        setup_completed_this_bar = False
        if setup_count == SETUP_LENGTH and setup_dir and setup_start is not None:
            setup_completed_this_bar = True
            perfected = _is_perfected(setup_dir, setup_start, highs, lows)
            tdst = _tdst_level(setup_dir, setup_start, highs, lows)

            state.events.append(
                TDEvent(
                    bar_index=i,
                    bar_date=bars[i].bar_date,
                    event_type="setup_9",
                    direction=setup_dir,
                    perfected=perfected,
                    tdst=tdst,
                )
            )
            last_idx[f"{setup_dir}_setup_9"] = i

            if setup_dir == "buy":
                state.tdst_resistance = tdst
            else:
                state.tdst_support = tdst

            # ── 2. Countdown lifecycle on setup completion ───────────────────
            if cd_dir is not None and cd_dir != setup_dir and config.cancel_countdown_on_opposite_setup:
                # An opposite-direction setup completed — cancel the countdown.
                cd_dir, cd_count, cd_bar8_close, cd_deferred, cd_tdst = None, 0, None, False, None

            if cd_dir == setup_dir and config.recycle_countdown_on_new_setup:
                # **[AMBIGUOUS]** recycle — see TDConfig. Off by default.
                # Restart the count from zero and re-anchor TDST to the new setup.
                cd_count, cd_bar8_close, cd_deferred, cd_tdst = 0, None, False, tdst

            if cd_dir is None:
                # A completed setup arms a countdown in the same direction.
                cd_dir, cd_count, cd_bar8_close, cd_deferred = setup_dir, 0, None, False
                cd_tdst = tdst

            # A completed setup resets the setup counter; the next bar starts fresh.
            setup_dir, setup_count, setup_start = None, 0, None

        # ── 3. Countdown counting ────────────────────────────────────────────
        # Evaluated on the setup-9 bar too: DeMark allows bar 9 itself to be
        # countdown bar 1 if it qualifies.
        if cd_dir is not None and i >= COUNTDOWN_LOOKBACK:

            if config.cancel_countdown_on_tdst_break and cd_tdst is not None:
                # **[AMBIGUOUS]** — see TDConfig. Off by default.
                broke = closes[i] > cd_tdst if cd_dir == "buy" else closes[i] < cd_tdst
                if broke:
                    cd_dir, cd_count, cd_bar8_close, cd_deferred, cd_tdst = None, 0, None, False, None

        if cd_dir is not None and i >= COUNTDOWN_LOOKBACK:
            qualifies = (
                closes[i] <= lows[i - COUNTDOWN_LOOKBACK]
                if cd_dir == "buy"
                else closes[i] >= highs[i - COUNTDOWN_LOOKBACK]
            )

            if qualifies:
                if cd_count < COUNTDOWN_LENGTH - 1:
                    cd_count += 1
                    cd_deferred = False
                    if cd_count == 8:
                        # Remember countdown bar 8's close — the bar-13 deferral
                        # test compares against this, NOT setup bar 8.
                        cd_bar8_close = closes[i]
                else:
                    # We are at 12 and this bar's close qualifies for 13.
                    # Apply the deferral test.
                    if cd_bar8_close is None:
                        # Countdown bar 8 was never recorded — only reachable if
                        # history begins mid-countdown. Treat the deferral test
                        # as unsatisfiable rather than silently passing it: a
                        # false 13 is the most consequential error this module
                        # can make (§5 scores it 25/25 and §8.4 pushes it in
                        # real time).
                        cd_deferred = True
                    else:
                        thirteen_ok = (
                            lows[i] <= cd_bar8_close
                            if cd_dir == "buy"
                            else highs[i] >= cd_bar8_close
                        )
                        if thirteen_ok:
                            cd_count = COUNTDOWN_LENGTH
                            cd_deferred = False
                            state.events.append(
                                TDEvent(
                                    bar_index=i,
                                    bar_date=bars[i].bar_date,
                                    event_type="countdown_13",
                                    direction=cd_dir,
                                )
                            )
                            last_idx[f"{cd_dir}_countdown_13"] = i
                            # A completed countdown clears itself; the next
                            # completed setup must re-arm one.
                            cd_dir, cd_count, cd_bar8_close, cd_tdst = None, 0, None, None
                        else:
                            # Standard deferred 13: hold at 12 and keep looking.
                            cd_deferred = True

        # Track whether the *final* bar is the one that completed a setup, so
        # `setup_complete` reflects "completed on the last bar", not "ever".
        if i == n - 1:
            state.setup_complete = setup_completed_this_bar

    # ── Final state ──────────────────────────────────────────────────────────
    state.setup_direction = setup_dir
    state.setup_count = setup_count
    if setup_dir and setup_start is not None:
        state.setup_perfected = _is_perfected(setup_dir, setup_start, highs, lows) if setup_count >= SETUP_LENGTH else None

    # If a setup completed exactly on the last bar, the loop cleared the
    # counters. Restore the display values from the event so the dashboard and
    # §5 scoring see "9 complete" rather than "0".
    if state.setup_complete and state.events:
        last_event = state.events[-1]
        if last_event.event_type == "setup_9" and last_event.bar_index == n - 1:
            state.setup_direction = last_event.direction
            state.setup_count = SETUP_LENGTH
            state.setup_perfected = last_event.perfected

    state.countdown_direction = cd_dir
    state.countdown_count = cd_count
    state.countdown_deferred = cd_deferred
    state.countdown_complete = bool(
        state.events and state.events[-1].event_type == "countdown_13" and state.events[-1].bar_index == n - 1
    )
    if state.countdown_complete:
        state.countdown_direction = state.events[-1].direction
        state.countdown_count = COUNTDOWN_LENGTH

    last_i = n - 1
    state.bars_since_buy_setup_9 = _bars_since(last_idx.get("buy_setup_9"), last_i)
    state.bars_since_sell_setup_9 = _bars_since(last_idx.get("sell_setup_9"), last_i)
    state.bars_since_buy_countdown_13 = _bars_since(last_idx.get("buy_countdown_13"), last_i)
    state.bars_since_sell_countdown_13 = _bars_since(last_idx.get("sell_countdown_13"), last_i)

    return state


def _bars_since(event_index: int | None, last_index: int) -> int | None:
    """Bars elapsed between an event and the final bar. None if it never fired."""
    return None if event_index is None else last_index - event_index


def _is_perfected(direction: str, setup_start: int, highs: list[float], lows: list[float]) -> bool:
    """Setup perfection test — scope doc §3.1 ("setup perfection").

    Buy Setup is perfected when the low of bar 8 *or* bar 9 is at or below the
    lows of bars 6 and 7. Sell Setup mirrors on highs.

    Indices: `setup_start` is bar 1, so bar k is at `setup_start + k - 1`.
    """
    b6 = setup_start + 5
    b7 = setup_start + 6
    b8 = setup_start + 7
    b9 = setup_start + 8
    if b9 >= len(lows):
        return False

    if direction == "buy":
        threshold = min(lows[b6], lows[b7])
        return lows[b8] <= threshold or lows[b9] <= threshold
    threshold = max(highs[b6], highs[b7])
    return highs[b8] >= threshold or highs[b9] >= threshold


def _tdst_level(direction: str, setup_start: int, highs: list[float], lows: list[float]) -> float:
    """TDST level projected by a completed setup — scope doc §3.1, §4.

    A completed **buy** setup projects TDST *resistance* at the highest high of
    setup bars 1-9. A completed **sell** setup projects TDST *support* at the
    lowest low of bars 1-9.

    **[AMBIGUOUS]** — some readings extend the range to include the bar
    immediately *before* setup bar 1 (the flip bar). We use bars 1-9 only, which
    is the more common implementation. If a §3.1 reference chart shows a TDST
    line one bar higher/lower than ours, this is the first thing to check.
    """
    end = setup_start + SETUP_LENGTH
    if direction == "buy":
        return max(highs[setup_start:end])
    return min(lows[setup_start:end])


def compute_both_timeframes(
    daily: BarSeries,
    weekly: BarSeries,
    config: TDConfig = DEFAULT_TD_CONFIG,
) -> tuple[TDState, TDState]:
    """Daily + weekly TD state — scope doc §3.1 ("Daily + weekly counts").

    §5 scores "Weekly 9/13 confluence: +5", so both are always computed together.
    """
    return compute(daily, config), compute(weekly, config)
