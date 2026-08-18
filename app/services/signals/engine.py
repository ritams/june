"""Technical Signals Layer — orchestration.

Implements the two run paths in `docs/technical-signals-layer-scope.md` §10:

    daily job 21:30 UK: fetch OHLCV → compute natives (TD counts, Regime
        Composite, stretch, RSI, RVOL) → score → persist
                                                        → run_daily()

    /api/lookup/{symbol}  (on-demand scoring)          → lookup()

Both paths run **the identical scoring code**, which §6.2 requires explicitly:
"Engine fetches 300 daily bars live, runs the identical scoring code (< 2s)."
`_evaluate()` is that shared core; the two entry points differ only in where
bars come from and what they persist.

Two rules from the scope doc that this module is the sole enforcer of:

  * **§3.3 staleness.** "If the data API is down, the run flags 'stale' on the
    dashboard and skips alerts — never scores silently on old data." A stale
    symbol is still scored and still persisted (so the dashboard can show the
    last known state with a staleness badge) but is marked `stale=True`, and the
    alerting layer must refuse to dispatch on a stale score.

  * **§6.4 lookups never alert.** "Lookups never trigger alerts." lookup() does
    not write to scores_daily or signal_events at all, so there is no path from
    a lookup to a notification even by accident.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from app.services.signals import store
from app.services.signals.bars import BarSeries, to_weekly
from app.services.signals.config import (
    HISTORY_BARS,
    LOOKUP_CACHE_TTL_SECONDS,
    MIN_WEEKLY_BARS,
    TRADINGVIEW_URL_TEMPLATE,
)
from app.services.signals.indicators import IndicatorSnapshot, snapshot
from app.services.signals.providers import ProviderError, get_provider
from app.services.signals.scoring import Score, compute_entry_score, compute_exit_score
from app.services.signals.td_sequential import TDState, compute_both_timeframes


logger = logging.getLogger(__name__)

# §4: the 200-day regime gate needs 200 bars, and the Regime Composite needs
# all six of its inputs before it reports anything (see indicators.regime_series).
# Below this we still score — the components that *are* computable contribute,
# and the rest correctly award zero — but the result is flagged `thin_history`
# so §9 can badge it rather than presenting a partial score as a complete one.
MIN_BARS_FOR_FULL_SCORE = 220

# Absolute floor. Below this even RSI(14) and a 50-bar z-score are undefined and
# a "score" would be pure noise, so we refuse rather than return a number.
MIN_BARS_TO_SCORE = 60


@dataclass
class Evaluation:
    """Everything the engine computed for one symbol on one run.

    Carries both scores plus the raw indicator/TD state, because §6.3's result
    card and §9's row-expand card both need the breakdown, not just the totals.
    """

    symbol: str
    bar_date: date
    entry: Score
    exit: Score
    daily_snapshot: IndicatorSnapshot
    weekly_snapshot: IndicatorSnapshot
    daily_td: TDState
    weekly_td: TDState
    stale: bool = False
    thin_history: bool = False
    bars_loaded: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def regime(self) -> str | None:
        r = self.daily_snapshot.regime
        return r.regime if r else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bar_date": self.bar_date.isoformat(),
            "entry": self.entry.to_dict(),
            "exit": self.exit.to_dict(),
            "regime": self.regime,
            "stale": self.stale,
            "thin_history": self.thin_history,
            "bars_loaded": self.bars_loaded,
            "warnings": self.warnings,
            "indicators": {
                "daily": self.daily_snapshot.to_dict(),
                "weekly": self.weekly_snapshot.to_dict(),
            },
            "td": {
                "daily": self.daily_td.to_dict(),
                "weekly": self.weekly_td.to_dict(),
            },
            # §3.1/§6.4: Chameleon cannot be replicated, so every result links
            # out for Fred's manual confirmation on the TradingView chart.
            "tradingview_url": TRADINGVIEW_URL_TEMPLATE.format(symbol=self.symbol),
        }


class InsufficientHistory(RuntimeError):
    """Fewer than MIN_BARS_TO_SCORE bars — scoring would be meaningless."""


def _evaluate(daily: BarSeries, *, as_of: date | None = None) -> Evaluation:
    """The shared scoring core — §6.2's "identical scoring code".

    Called by both run_daily() and lookup(). Contains no I/O and no persistence
    so that the two paths cannot drift apart, which is the failure §6.2's
    wording is guarding against.
    """
    if len(daily) < MIN_BARS_TO_SCORE:
        raise InsufficientHistory(
            f"{daily.symbol}: {len(daily)} bars loaded, need at least {MIN_BARS_TO_SCORE} to score"
        )

    warnings: list[str] = []
    weekly = to_weekly(daily)

    if len(weekly) < MIN_WEEKLY_BARS:
        # §5 pays +5 for weekly TD confluence and 7 for the daily/weekly RSI
        # combination. With too few weekly bars those components correctly score
        # zero; we surface it so a persistently low score is explicable.
        warnings.append(
            f"Only {len(weekly)} weekly bars (need {MIN_WEEKLY_BARS}) — weekly confluence unavailable"
        )

    thin = len(daily) < MIN_BARS_FOR_FULL_SCORE
    if thin:
        warnings.append(
            f"{len(daily)} daily bars (need {MIN_BARS_FOR_FULL_SCORE}) — "
            "200D trend and Regime Composite not yet computable"
        )

    daily_snap = snapshot(daily)
    weekly_snap = snapshot(weekly)
    daily_td, weekly_td = compute_both_timeframes(daily, weekly)

    entry = compute_entry_score(daily_snap, weekly_snap, daily_td, weekly_td)
    exit_ = compute_exit_score(daily_snap, weekly_snap, daily_td, weekly_td)

    # §1 scores "on a daily-close basis", but an intraday fetch returns today's
    # bar still forming: its close is a live price and its volume is a fraction
    # of the session's. Verified live — mid-session AAPL came back with 46% of
    # average volume from yfinance and 2.3% from Twelve Data.
    #
    # The nightly run (§3.3, after the close) is unaffected. On-demand lookups
    # (§6) during market hours are, and the distortion is one-sided: RVOL is
    # always understated, so §5's volume row under-scores rather than over-scores.
    # Flagged rather than dropped — §6 explicitly wants a live view, and silently
    # discarding today's bar would make a lookup disagree with the chart Fred is
    # looking at.
    if _looks_partial(daily, as_of=as_of):
        warnings.append(
            "Today's bar is still forming — close and RVOL are provisional; "
            "§5's volume component may under-score until the session closes"
        )

    stale = daily.is_stale(as_of=as_of)
    if stale:
        # §3.3: flagged, not suppressed. The dashboard shows the last known
        # state with a badge; the alerting layer refuses to dispatch on it.
        warnings.append(
            f"Data is stale — newest bar is {daily.bars[-1].bar_date.isoformat()}; alerts suppressed"
        )

    return Evaluation(
        symbol=daily.symbol,
        bar_date=daily.bars[-1].bar_date,
        entry=entry,
        exit=exit_,
        daily_snapshot=daily_snap,
        weekly_snapshot=weekly_snap,
        daily_td=daily_td,
        weekly_td=weekly_td,
        stale=stale,
        thin_history=thin,
        bars_loaded=len(daily),
        warnings=warnings,
    )


# ── On-demand lookup (§6) ────────────────────────────────────────────────────


def lookup(
    symbol: str,
    *,
    bars: int = HISTORY_BARS,
    persist_bars: bool = True,
    use_cache: bool = True,
) -> Evaluation:
    """Score any symbol live — §6, "Ticker lookup — on-demand analysis".

    §6.2: "Engine fetches 300 daily bars live, runs the identical scoring code
    (< 2s)."

    `persist_bars` defaults to True: caching fetched bars is free (they are the
    same bars the nightly run would fetch) and makes a repeat lookup on the same
    symbol hit SQLite instead of the network, which is what keeps §6.2's 2-second
    budget realistic. It writes only to `ohlcv_daily` — never to `scores_daily`
    or `signal_events`, because §6.4 requires that "lookups never trigger
    alerts" and the alerting layer reads from those tables.

    Raises ProviderError for an unfetchable symbol and InsufficientHistory for a
    symbol too new to score. The API layer maps both to 4xx/5xx as appropriate.
    """
    sym = symbol.strip().upper()

    # §3.3: "on-demand fetch for lookups (~1-2s), session-cached." Serve from the
    # bar cache when it is fresh enough, so a burst of lookups on one symbol —
    # opening a row, re-opening it, comparing two names — costs one network
    # round trip rather than N. Without this the cache was write-only and every
    # lookup re-fetched, which burns provider quota and is what makes the §6.2
    # sub-2-second budget fragile under rate limiting.
    if use_cache:
        cached = _fresh_cached_bars(sym, bars)
        if cached is not None:
            logger.debug("Lookup %s served from bar cache", sym)
            return _evaluate(cached)

    provider = get_provider()
    series = provider.daily_bars(sym, bars)

    if persist_bars:
        try:
            store.upsert_bars(series)
        except Exception as exc:
            # A cache write failure must never fail the lookup itself — the
            # user asked for a score, and we have the bars in hand to compute one.
            logger.warning("Failed to cache bars for %s: %s", sym, exc)

    return _evaluate(series)


# Below this fraction of the trailing median, today's volume is almost certainly
# a part-session print rather than a quiet day. Deliberately generous: a genuinely
# thin trading day can run 30-40% of median, and a false "still forming" warning
# on a real close is worse than missing a marginal case.
PARTIAL_BAR_VOLUME_RATIO = 0.25
PARTIAL_BAR_LOOKBACK = 20


def _looks_partial(series: BarSeries, *, as_of: date | None = None) -> bool:
    """Is the newest bar a part-formed session? — see the §1 note in _evaluate.

    Two conditions must both hold, which is what keeps false positives down:

      1. The bar is dated **today**. A completed prior session is never partial.
      2. Its volume is far below the trailing median.

    Volume-based rather than clock-based on purpose: a clock rule needs an
    exchange calendar per asset class, and crypto never closes. The volume test
    works for any venue and measures the thing that actually distorts the score.

    Returns False when volume is unknown (commodities, FX) — there is nothing to
    compare, and §5's volume row already awards zero for those.
    """
    if not series.bars:
        return False
    today = as_of or datetime.now(timezone.utc).date()
    last = series.bars[-1]
    if last.bar_date != today or last.volume is None:
        return False

    prior = [b.volume for b in series.bars[-(PARTIAL_BAR_LOOKBACK + 1):-1] if b.volume]
    if len(prior) < 5:
        return False
    median = sorted(prior)[len(prior) // 2]
    return median > 0 and last.volume < median * PARTIAL_BAR_VOLUME_RATIO


def _fresh_cached_bars(symbol: str, bars: int) -> BarSeries | None:
    """Cached bars for `symbol`, or None if absent, thin, or too old.

    "Fresh" is LOOKUP_CACHE_TTL_SECONDS since the *fetch*, not since the last
    bar. A cache written 2 minutes ago is current even if its newest bar is
    Friday's close on a Sunday — the bar is genuinely the latest one that exists.
    Keying on bar age instead would defeat the cache every weekend.

    Returns None rather than raising on any problem: the cache is an
    optimisation, and a corrupt or unreadable one must fall through to a live
    fetch rather than fail the lookup.
    """
    try:
        cached = store.load_bars(symbol, bars)
    except Exception as exc:
        logger.warning("Bar cache read failed for %s: %s", symbol, exc)
        return None

    if len(cached) < min(bars, MIN_BARS_TO_SCORE) or cached.fetched_at is None:
        return None

    age = (datetime.now(timezone.utc) - cached.fetched_at).total_seconds()
    if age > LOOKUP_CACHE_TTL_SECONDS:
        return None
    return cached


# ── Nightly run (§10) ────────────────────────────────────────────────────────


@dataclass
class RunResult:
    """Summary of a nightly run — surfaced on the §9 header's last-run stamp."""

    started_at: str
    finished_at: str
    scored: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "scored_count": len(self.scored),
            "scored": self.scored,
            "stale": self.stale,
            "failed": self.failed,
            "skipped": self.skipped,
        }


def run_daily(symbols: list[str] | None = None, *, as_of: date | None = None) -> RunResult:
    """The §10 nightly job: fetch → compute → score → persist, for the watchlist.

    Per-symbol failures are isolated. One unfetchable ticker must not abort the
    run for the other 49 — §3.3 treats a data outage as a per-symbol staleness
    condition, not a global failure, and a half-populated dashboard with honest
    per-row badges is far more useful than an empty one.

    Returns a RunResult partitioning every symbol into scored / stale / failed /
    skipped, so the §9 header can report exactly what happened rather than just
    a timestamp.
    """
    started = datetime.now(timezone.utc).isoformat()
    result = RunResult(started_at=started, finished_at=started)

    targets = symbols or [w["symbol"] for w in store.list_watchlist()]
    if not targets:
        logger.info("Signals daily run: watchlist is empty, nothing to do")
        result.finished_at = datetime.now(timezone.utc).isoformat()
        return result

    provider = get_provider()
    logger.info("Signals daily run: %d symbols via %s", len(targets), provider.name)

    for sym in targets:
        try:
            series = provider.daily_bars(sym, HISTORY_BARS)
            store.upsert_bars(series)
        except ProviderError as exc:
            # Fetch failed. Fall back to the persisted bars so the symbol still
            # gets a score — it will almost certainly flag stale, which is
            # exactly the §3.3 behaviour: show the last known state, suppress
            # alerts, never pretend the data is current.
            logger.warning("Fetch failed for %s, falling back to cached bars: %s", sym, exc)
            series = store.load_bars(sym, HISTORY_BARS)
            if not series.bars:
                result.failed[sym] = str(exc)
                continue
        except Exception as exc:
            logger.exception("Unexpected error fetching %s", sym)
            result.failed[sym] = f"{type(exc).__name__}: {exc}"
            continue

        try:
            evaluation = _evaluate(series, as_of=as_of)
        except InsufficientHistory as exc:
            result.skipped[sym] = str(exc)
            continue
        except Exception as exc:
            logger.exception("Scoring failed for %s", sym)
            result.failed[sym] = f"{type(exc).__name__}: {exc}"
            continue

        try:
            _persist(evaluation)
        except Exception as exc:
            logger.exception("Persist failed for %s", sym)
            result.failed[sym] = f"persist: {exc}"
            continue

        result.scored.append(sym)
        if evaluation.stale:
            result.stale.append(sym)

    result.finished_at = datetime.now(timezone.utc).isoformat()
    logger.info(
        "Signals daily run complete: %d scored (%d stale), %d failed, %d skipped",
        len(result.scored), len(result.stale), len(result.failed), len(result.skipped),
    )
    return result


def _persist(ev: Evaluation) -> None:
    """Write one evaluation's score and any newly completed TD/regime events."""
    store.save_score(ev.symbol, ev.bar_date, ev.entry, ev.exit, regime=ev.regime, stale=ev.stale)

    # Log TD completions that landed on this bar. record_signal_event()
    # deduplicates, so a re-run is harmless.
    for td_state, timeframe in ((ev.daily_td, "daily"), (ev.weekly_td, "weekly")):
        for event in td_state.events:
            if event.bar_date != ev.bar_date:
                continue
            store.record_signal_event(
                ev.symbol, event.bar_date, event.event_type, event.direction, timeframe,
                {"perfected": event.perfected, "tdst": event.tdst},
            )

    # Log a regime flip that landed on this bar. bars_since_flip == 0 means the
    # flip is on the bar we just scored.
    regime = ev.daily_snapshot.regime
    if regime and regime.bars_since_flip == 0 and regime.flip_label:
        store.record_signal_event(
            ev.symbol, ev.bar_date, "regime_flip",
            # A flip into Bullish is a buy-side event; into Bearish, sell-side.
            "buy" if regime.regime == "Bullish" else "sell",
            "daily",
            {"flip": regime.flip_label, "composite": regime.composite},
        )
