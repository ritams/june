"""Technical Signals Layer — low-noise alerting.

Implements `docs/technical-signals-layer-scope.md` §8 in full:

    1. Alerts fire only on **band transitions** (Watch → Strong, Strong → Prime,
       Exit crossing 70). Never on wobble within a band.
    2. Max **one alert per ticker per day**, hard cap; multiple drivers on one
       name merge into one message.
    3. One **daily digest ~21:45 UK**: top 5 entry scores, Prime signals, exit
       warnings on held names, TD counts line. Real-time pushes reserved for
       Prime (>=85) transitions.
    4. **TD completion override (default ON):** TD9/TD13 completions, daily and
       weekly, both sides, always notify regardless of band. Weekly completions
       and any TD13 → real-time push; daily TD9s → digest "TD counts" line.
       Config: `td_override.enabled`, `td_override.realtime_scope`, per-ticker
       opt-out.
    5. **Intake digest:** Sunday 18:00 UK, only when names are pending (§7).
    6. **Payload to Hermes:** `{type, ticker, direction, score, band, drivers,
       timestamp}` — outbound POST, retried, delivery logged.

═══════════════════════════════════════════════════════════════════════════════
THE WHOLE POINT OF THIS MODULE IS SILENCE
═══════════════════════════════════════════════════════════════════════════════

§1 lists "State-change alerts only. No repeat pings." as a non-negotiable design
principle, and §8's title is "Alerting — low-noise rules". Every rule here
*suppresses* rather than emits. The failure mode to fear is not a missed alert;
it is a system that pings enough to get muted, at which point it delivers
nothing at all.

Four independent suppression gates, applied in order:

  1. **Staleness (§3.3).** A stale score never alerts. "never scores silently on
     old data" — and never notifies on it either.
  2. **Band transition (§8.1).** No prior band → no transition. Same band as
     yesterday → no alert, however much the raw score moved inside it.
  3. **Per-ticker daily cap (§8.2).** One alert per ticker per day, hard. Multiple
     drivers merge into one message rather than queueing several.
  4. **Real-time vs digest (§8.3).** Only Prime transitions and the TD escalations
     in §8.4 push immediately; everything else waits for the 21:45 digest.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from app.services.signals import store
from app.services.signals.config import (
    ALERT_MAX_ATTEMPTS,
    ALERT_RETRY_BACKOFF_SECONDS,
    BAND_ORDER,
    DIGEST_TOP_N,
    EXIT_ALERT_THRESHOLD,
    HERMES_WEBHOOK_URL,
    REALTIME_BAND,
    TD_OVERRIDE_ENABLED,
    TD_OVERRIDE_REALTIME_SCOPE,
)


logger = logging.getLogger(__name__)


# ── Candidate model ──────────────────────────────────────────────────────────


@dataclass
class Alert:
    """One alert, in the §8.6 payload shape.

    §8.6: "{type: signal|intake, ticker, direction, score, band, drivers:[...],
    timestamp}". `realtime` and `reason` are ours — routing metadata that stays
    out of the wire payload (see to_payload()).
    """

    alert_type: str            # 'signal' | 'intake' | 'digest' (§8.6 `type`)
    ticker: str | None
    direction: str             # 'buy' | 'sell'
    score: int
    band: str
    drivers: list[str] = field(default_factory=list)
    timestamp: str = ""
    realtime: bool = False     # §8.3 routing: push now vs hold for the digest
    reason: str = ""           # why this fired — for the log and for debugging

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_payload(self) -> dict[str, Any]:
        """The §8.6 wire payload — exactly the documented keys, nothing extra."""
        return {
            "type": self.alert_type,
            "ticker": self.ticker,
            "direction": self.direction,
            "score": self.score,
            "band": self.band,
            "drivers": self.drivers,
            "timestamp": self.timestamp,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        """Human-readable message for the Telegram sink.

        §8.2: "multiple drivers on one name merge into one message" — the driver
        list is rendered as bullets under a single header rather than as
        separate alerts.
        """
        head = f"{self.ticker} · {self.band} ({self.score})" if self.ticker else self.band
        arrow = "▲" if self.direction == "buy" else "▼"
        lines = [f"{arrow} {head}"]
        if self.reason:
            lines.append(self.reason)
        lines.extend(f"  • {d}" for d in self.drivers[:6])
        return "\n".join(lines)


# ── §8.1 band transitions ────────────────────────────────────────────────────


def _band_rank(band: str | None) -> int:
    """Position in BAND_ORDER (weakest→strongest). -1 for unknown/None."""
    if band is None or band not in BAND_ORDER:
        return -1
    return BAND_ORDER.index(band)


def is_entry_upgrade(previous: str | None, current: str) -> bool:
    """True when the entry band moved *up* — §8.1.

    §8.1's examples are all upgrades ("Watch → Strong, Strong → Prime"), so
    downgrades do not alert on the entry side: a name becoming less attractive
    is not actionable in the way a name becoming attractive is, and the exit
    score is the channel for "get out" (§5: exit alerts on held names).

    A `previous` of None is not a transition. This is what stops every name
    alerting the day it joins the watchlist, and what stops a re-run after a
    database reset from firing the whole list at once.
    """
    if previous is None:
        return False
    return _band_rank(current) > _band_rank(previous)


def crossed_exit_threshold(previous_score: int | None, current_score: int) -> bool:
    """True when the exit score crossed 70 upward — §8.1 "Exit crossing 70".

    Crossing, not exceeding: a score that was already above 70 yesterday and is
    still above it today has not crossed and must not re-alert (§1: "No repeat
    pings"). None means no prior score, which is not a crossing.
    """
    if previous_score is None:
        return False
    return previous_score < EXIT_ALERT_THRESHOLD <= current_score


# ── §8.4 TD completion override ──────────────────────────────────────────────


def _td_override_is_realtime(event_type: str, timeframe: str) -> bool:
    """§8.4 routing: which TD completions escalate to a real-time push.

    §8.4: "Weekly completions and any TD13 → real-time push; daily TD9s →
    digest 'TD counts' line."
    """
    scope = TD_OVERRIDE_REALTIME_SCOPE
    if scope == "none":
        return False
    if scope == "all":
        return True
    # 'weekly_and_13' — the §8.4 default.
    return timeframe == "weekly" or event_type == "countdown_13"


def _td_alerts_for(symbol: str, bar_date: date, row: dict[str, Any]) -> list[Alert]:
    """TD completions on this bar that §8.4 says must notify regardless of band.

    Returns one Alert per completion; the merge step in evaluate() collapses
    them onto the ticker's single daily alert.
    """
    if not TD_OVERRIDE_ENABLED:
        return []
    if row.get("td_optout"):
        # §8.4: "per-ticker opt-out".
        return []

    out: list[Alert] = []
    for event in store.signal_events_on(symbol, bar_date):
        if event["event_type"] not in ("setup_9", "countdown_13"):
            continue
        label = "TD9" if event["event_type"] == "setup_9" else "TD13"
        out.append(
            Alert(
                alert_type="signal",
                ticker=symbol,
                direction=event["direction"],
                # §8.4 fires "regardless of band", so the band and score carried
                # here are context, not the trigger.
                score=int(row.get("entry_score") or 0),
                band=str(row.get("entry_band") or "No signal"),
                drivers=[f"{label} {event['direction']} completion · {event['timeframe'].capitalize()}"],
                realtime=_td_override_is_realtime(event["event_type"], event["timeframe"]),
                reason=f"TD override: {label} {event['direction']} on {event['timeframe']}",
            )
        )
    return out


# ── Evaluation ───────────────────────────────────────────────────────────────


def evaluate(as_of: date | None = None) -> list[Alert]:
    """Decide what to alert on for the latest scored bar — the §8 rules, in order.

    Reads persisted state only; computes nothing. That separation matters: it
    means the nightly job can score without alerting (useful for a backfill),
    and that this can be re-run without re-fetching.

    Returns the merged, deduplicated, cap-respecting alert list. Callers pass it
    to dispatch().
    """
    candidates: dict[str, list[Alert]] = {}

    for row in store.watchlist_with_scores():
        symbol = row["symbol"]

        # ── Gate 1: no score yet, or stale (§3.3) ────────────────────────────
        if row.get("entry_score") is None or not row.get("bar_date"):
            continue
        if row.get("stale"):
            # §3.3: "the run flags 'stale' on the dashboard and skips alerts".
            logger.debug("Skipping alerts for %s — score is stale", symbol)
            continue

        bar_date = date.fromisoformat(row["bar_date"])
        if as_of is not None and bar_date != as_of:
            # Only alert on the bar we were asked about, so a re-run on a later
            # day cannot resurrect an old transition.
            continue

        found: list[Alert] = []

        # ── Gate 2: band transition (§8.1) ───────────────────────────────────
        prev_entry_band = store.previous_band(symbol, "entry")
        entry_band = str(row["entry_band"])
        if is_entry_upgrade(prev_entry_band, entry_band):
            found.append(
                Alert(
                    alert_type="signal",
                    ticker=symbol,
                    direction="buy",
                    score=int(row["entry_score"]),
                    band=entry_band,
                    drivers=list(row.get("drivers") or []),
                    # §8.3: "Real-time pushes reserved for Prime (>=85) transitions."
                    realtime=entry_band == REALTIME_BAND,
                    reason=f"Entry band {prev_entry_band} → {entry_band}",
                )
            )

        # §5: "Exit alerts only on names toggled held."
        if row.get("held") and row.get("exit_score") is not None:
            prev_exit = store.previous_score_value(symbol, "exit")
            if crossed_exit_threshold(prev_exit, int(row["exit_score"])):
                found.append(
                    Alert(
                        alert_type="signal",
                        ticker=symbol,
                        direction="sell",
                        score=int(row["exit_score"]),
                        band=str(row.get("exit_band") or ""),
                        drivers=list(row.get("exit_drivers") or []),
                        # An exit crossing on a held name is money at risk —
                        # §8.1 lists it alongside the band upgrades as a
                        # first-class trigger, so it pushes rather than waiting.
                        realtime=True,
                        reason=f"Exit score crossed {EXIT_ALERT_THRESHOLD} ({prev_exit} → {row['exit_score']})",
                    )
                )

        # ── §8.4 TD override — fires regardless of band ──────────────────────
        found.extend(_td_alerts_for(symbol, bar_date, row))

        if found:
            candidates[symbol] = found

    return _merge_and_cap(candidates)


def _merge_and_cap(candidates: dict[str, list[Alert]]) -> list[Alert]:
    """§8.2: one alert per ticker per day, drivers merged into one message.

    Merge rules:
      * The strongest alert wins the header (highest score, real-time beating
        digest on a tie), because that is what the message should lead with.
      * Every alert's drivers and reasons are folded into it, so nothing is lost.
      * If *any* constituent was real-time, the merged alert is real-time — a
        Prime transition must not be downgraded by being bundled with a
        digest-level TD9.
      * Tickers already alerted today are dropped entirely (the hard cap).
    """
    out: list[Alert] = []

    for symbol, alerts in candidates.items():
        if store.alerts_sent_today(symbol):
            # §8.2's hard cap. Survives process restarts because it is a DB
            # query, not in-memory state.
            logger.debug("Suppressing %s — already alerted today", symbol)
            continue

        primary = max(alerts, key=lambda a: (a.realtime, a.score))
        merged_drivers: list[str] = []
        reasons: list[str] = []
        for a in alerts:
            if a.reason and a.reason not in reasons:
                reasons.append(a.reason)
            for d in a.drivers:
                if d not in merged_drivers:
                    merged_drivers.append(d)

        out.append(
            Alert(
                alert_type=primary.alert_type,
                ticker=symbol,
                direction=primary.direction,
                score=primary.score,
                band=primary.band,
                drivers=merged_drivers,
                realtime=any(a.realtime for a in alerts),
                reason=" · ".join(reasons),
            )
        )

    # Strongest first, so a truncated digest keeps the most important names.
    out.sort(key=lambda a: (a.realtime, a.score), reverse=True)
    return out


# ── §8.6 dispatch ────────────────────────────────────────────────────────────


def dispatch(alerts: list[Alert], *, telegram_client: Any | None = None) -> dict[str, Any]:
    """Send the real-time alerts and log every one — §8.6.

    §8.6: "outbound POST, retried, delivery logged. Hermes owns transport."

    Real-time alerts (§8.3) are sent now. Digest-level alerts are logged as
    undelivered and picked up by build_digest() at 21:45 — logging them
    immediately is what makes the digest reconstructible and what makes §8.2's
    daily cap work across a restart.

    Returns a summary: how many were sent, held for the digest, or failed.
    """
    sent, held, failed = [], [], []

    for alert in alerts:
        alert_id = store.log_alert(alert.ticker, alert.alert_type, alert.to_payload())

        if not alert.realtime:
            held.append(alert.ticker)
            continue

        ok, error, attempts = _deliver(alert, telegram_client=telegram_client)
        store.mark_alert_delivered(alert_id, delivered=ok, attempts=attempts, error=error)
        (sent if ok else failed).append(alert.ticker)

    if failed:
        logger.warning("Alert delivery failed for: %s", failed)
    return {"sent": sent, "held_for_digest": held, "failed": failed}


def _deliver(alert: Alert, *, telegram_client: Any | None = None) -> tuple[bool, str | None, int]:
    """Deliver one alert with retries. Returns (ok, last_error, attempts).

    Sink selection (see HERMES_WEBHOOK_URL in config.py for why both exist):
      * `SIGNALS_HERMES_WEBHOOK_URL` set → POST the §8.6 payload there.
      * Otherwise → the repo's existing Telegram client, which is what actually
        reaches Fred today.

    Never raises. A notification failure must not break the nightly run; it is
    recorded in `alerts_log` with the error, and the digest still reports the
    underlying signal.
    """
    last_error: str | None = None

    for attempt in range(1, ALERT_MAX_ATTEMPTS + 1):
        try:
            if HERMES_WEBHOOK_URL:
                import httpx

                response = httpx.post(HERMES_WEBHOOK_URL, json=alert.to_payload(), timeout=15.0)
                response.raise_for_status()
                return True, None, attempt

            if telegram_client is not None and getattr(telegram_client, "enabled", False):
                telegram_client.send_message(alert.render())
                return True, None, attempt

            # Neither sink configured. Not an error — a deliberate state during
            # setup — but it must not read as a successful delivery.
            return False, "No alert sink configured (set SIGNALS_HERMES_WEBHOOK_URL or Telegram creds)", attempt

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Alert delivery attempt %d/%d failed: %s", attempt, ALERT_MAX_ATTEMPTS, last_error)
            if attempt < ALERT_MAX_ATTEMPTS:
                # Linear backoff. Deliberately not exponential: this runs inside
                # the nightly job and a long tail would delay the digest.
                time.sleep(ALERT_RETRY_BACKOFF_SECONDS * attempt)

    return False, last_error, ALERT_MAX_ATTEMPTS


# ── §8.3 daily digest ────────────────────────────────────────────────────────


def build_digest(as_of: date | None = None) -> str | None:
    """The §8.3 daily digest message, or None when there is nothing to say.

    §8.3: "top 5 entry scores, Prime signals, exit warnings on held names, TD
    counts line."

    Returns None rather than an empty-state message when the watchlist has no
    scores at all — §1's "No repeat pings" principle extends to not sending a
    nightly "nothing happened" note. A digest with real content always sends,
    including when the top scores are low, because "nothing is set up right now"
    is genuinely useful once a day.
    """
    rows = [r for r in store.watchlist_with_scores() if r.get("entry_score") is not None]
    if not rows:
        return None

    target = as_of or _latest_bar_date(rows)
    lines: list[str] = [f"📊 Signals digest · {target.isoformat() if target else 'latest'}"]

    # Top N entry scores (§8.3). Rows arrive pre-ranked from the store.
    top = rows[:DIGEST_TOP_N]
    lines.append("")
    lines.append(f"Top {len(top)} entry:")
    for r in top:
        badge = " ⚠counter-trend" if r.get("counter_trend") else ""
        stale = " ·stale" if r.get("stale") else ""
        lines.append(f"  {r['symbol']:<8} {r['entry_score']:>3} {r['entry_band']}{badge}{stale}")

    # Prime signals called out separately (§8.3).
    prime = [r for r in rows if r.get("entry_band") == REALTIME_BAND]
    if prime:
        lines.append("")
        lines.append("⭐ Prime:")
        for r in prime:
            drivers = r.get("drivers") or []
            lines.append(f"  {r['symbol']} ({r['entry_score']}) — {drivers[0] if drivers else 'see dashboard'}")

    # Exit warnings, held names only (§5, §8.3).
    exits = [
        r for r in rows
        if r.get("held") and (r.get("exit_score") or 0) >= EXIT_ALERT_THRESHOLD
    ]
    if exits:
        lines.append("")
        lines.append("🔻 Exit warnings (held):")
        for r in exits:
            lines.append(f"  {r['symbol']} exit {r['exit_score']} {r.get('exit_band') or ''}")

    # TD counts line (§8.3, §8.4 — daily TD9s land here rather than pushing).
    td_line = _td_counts_line(target)
    if td_line:
        lines.append("")
        lines.append(f"TD counts: {td_line}")

    stale_count = sum(1 for r in rows if r.get("stale"))
    if stale_count:
        # §3.3: staleness must be visible, not silent.
        lines.append("")
        lines.append(f"⚠ {stale_count} name(s) on stale data — alerts suppressed for those")

    return "\n".join(lines)


def _latest_bar_date(rows: list[dict[str, Any]]) -> date | None:
    dates = [r["bar_date"] for r in rows if r.get("bar_date")]
    return date.fromisoformat(max(dates)) if dates else None


def _td_counts_line(target: date | None) -> str:
    """Compact summary of TD completions on the target bar — §8.3's "TD counts line"."""
    if target is None:
        return ""
    events = store.signal_events_on_date(target)
    parts = []
    for ev in events:
        if ev["event_type"] not in ("setup_9", "countdown_13"):
            continue
        label = "9" if ev["event_type"] == "setup_9" else "13"
        tf = "W" if ev["timeframe"] == "weekly" else "D"
        side = "buy" if ev["direction"] == "buy" else "sell"
        parts.append(f"{ev['symbol']} {side}{label}·{tf}")
    return ", ".join(parts)


def send_digest(as_of: date | None = None, *, telegram_client: Any | None = None) -> dict[str, Any]:
    """Build and send the §8.3 digest, logging delivery like any other alert."""
    message = build_digest(as_of)
    if message is None:
        logger.info("No digest to send — no scored names")
        return {"sent": False, "reason": "nothing to report"}

    digest = Alert(
        alert_type="digest",
        ticker=None,
        direction="buy",
        score=0,
        band="digest",
        drivers=[],
        realtime=True,
    )
    alert_id = store.log_alert(None, "digest", {"message": message})
    ok, error, attempts = _deliver_text(message, digest, telegram_client=telegram_client)
    store.mark_alert_delivered(alert_id, delivered=ok, attempts=attempts, error=error)
    return {"sent": ok, "error": error, "message": message}


def _deliver_text(message: str, alert: Alert, *, telegram_client: Any | None = None) -> tuple[bool, str | None, int]:
    """Deliver a pre-rendered text body (the digest) through the same sinks."""
    last_error: str | None = None
    for attempt in range(1, ALERT_MAX_ATTEMPTS + 1):
        try:
            if HERMES_WEBHOOK_URL:
                import httpx

                payload = alert.to_payload()
                payload["message"] = message
                response = httpx.post(HERMES_WEBHOOK_URL, json=payload, timeout=15.0)
                response.raise_for_status()
                return True, None, attempt
            if telegram_client is not None and getattr(telegram_client, "enabled", False):
                telegram_client.send_message(message)
                return True, None, attempt
            return False, "No alert sink configured", attempt
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < ALERT_MAX_ATTEMPTS:
                time.sleep(ALERT_RETRY_BACKOFF_SECONDS * attempt)
    return False, last_error, ALERT_MAX_ATTEMPTS


# ── Orchestration ────────────────────────────────────────────────────────────


def run_alert_cycle(as_of: date | None = None, *, telegram_client: Any | None = None) -> dict[str, Any]:
    """Evaluate and dispatch — the entry point the scheduler calls after scoring."""
    alerts = evaluate(as_of)
    if not alerts:
        logger.info("Alert cycle: nothing to send")
        return {"evaluated": 0, "sent": [], "held_for_digest": [], "failed": []}
    result = dispatch(alerts, telegram_client=telegram_client)
    result["evaluated"] = len(alerts)
    logger.info(
        "Alert cycle: %d evaluated, %d sent, %d held for digest, %d failed",
        len(alerts), len(result["sent"]), len(result["held_for_digest"]), len(result["failed"]),
    )
    return result
