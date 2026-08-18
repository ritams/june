"""Technical Signals Layer — persistence operations.

Read/write helpers over the SQLite schema in db.py, implementing the data
access the `docs/technical-signals-layer-scope.md` §10 endpoints need:

    /api/watchlist   (scores, history, sources)   → watchlist_with_scores()
    daily job                                      → upsert_bars(), save_score()
    §9 row expand "signal history"                 → signal_history()

Every write is idempotent on its natural key so re-running the nightly job
(§10) never duplicates rows — important because the job can legitimately run
twice in a day after a restart or a manual refresh.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from app.services.signals.bars import Bar, BarSeries
from app.services.signals.config import (
    DEFAULT_SOURCE_TAG,
    HISTORY_BARS,
    SOURCE_TAGS,
    TRADINGVIEW_URL_TEMPLATE,
)
from app.services.signals.db import connect


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Watchlist (§1, §7) ───────────────────────────────────────────────────────


def add_to_watchlist(
    symbol: str,
    *,
    asset_class: str | None = None,
    source_tag: str = DEFAULT_SOURCE_TAG,
    notes: str | None = None,
    held: bool = False,
) -> dict[str, Any]:
    """Add or reactivate a watchlist entry — §7 provenance required.

    §7: "every ticker carries a source tag (STENO / RV / COWAN / OWN) and date
    added". An unrecognised tag is rejected rather than silently coerced,
    because provenance is the audit trail behind §7.4's approval flow — a
    mislabelled source would misattribute a name to a research provider that
    never recommended it.

    Re-adding a soft-deleted symbol reactivates it and refreshes its source and
    added_at, which is the behaviour §7.4 needs when a note re-recommends a name
    that was previously removed.

    `asset_class` is inferred from the symbol's shape when omitted. The inference
    lives here rather than in the API layer because this function has three
    callers — the API, §7.4's intake approval, and the nightly job's fixtures —
    and putting it upstream meant `BTC-USD` entered the watchlist tagged
    "equity" from every path except one. Caught on the production box.
    """
    tag = source_tag.strip().upper()
    if tag not in SOURCE_TAGS:
        raise ValueError(f"Unknown source tag {source_tag!r}. Expected one of {', '.join(SOURCE_TAGS)}")

    sym = symbol.strip().upper()
    if not sym:
        raise ValueError("symbol is required")

    if not asset_class:
        from app.services.signals.providers import classify_asset

        asset_class = classify_asset(sym)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO watchlist (symbol, asset_class, source_tag, added_at, held, notes, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(symbol) DO UPDATE SET
                active      = 1,
                asset_class = excluded.asset_class,
                source_tag  = excluded.source_tag,
                added_at    = excluded.added_at,
                notes       = COALESCE(excluded.notes, watchlist.notes)
            """,
            (sym, asset_class, tag, _now(), int(held), notes),
        )
        row = conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (sym,)).fetchone()
    return dict(row)


def remove_from_watchlist(symbol: str) -> bool:
    """Soft-delete a watchlist entry, preserving its score and signal history.

    Soft rather than hard delete because §9's row-expand card shows signal
    history, and §5's Phase 4 backtest replays historical scores — both would
    lose data if removing a name deleted its past. Returns False if the symbol
    was not on the list.
    """
    sym = symbol.strip().upper()
    with connect() as conn:
        cur = conn.execute("UPDATE watchlist SET active = 0 WHERE symbol = ? AND active = 1", (sym,))
        return cur.rowcount > 0


def set_held(symbol: str, held: bool) -> bool:
    """Toggle the held flag — §5: "Exit alerts only on names toggled held"."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE watchlist SET held = ? WHERE symbol = ? AND active = 1",
            (int(held), symbol.strip().upper()),
        )
        return cur.rowcount > 0


def list_watchlist(*, active_only: bool = True) -> list[dict[str, Any]]:
    """All watchlist entries, newest first."""
    sql = "SELECT * FROM watchlist"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY added_at DESC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


# ── OHLCV (§3.3) ─────────────────────────────────────────────────────────────


def upsert_bars(series: BarSeries) -> int:
    """Persist a daily bar series. Idempotent on (symbol, bar_date).

    Returns the number of rows written. Uses ON CONFLICT rather than
    INSERT OR REPLACE so the update is explicit about which columns it
    overwrites — a re-fetch should refresh prices and fetched_at, and nothing
    else.

    Existing bars are updated, not skipped: providers do revise recent bars
    (split/dividend adjustments, late volume prints), and scoring on a stale
    copy of yesterday's close would be wrong in a way nothing else would catch.
    """
    if not series.bars:
        return 0
    fetched = (series.fetched_at or datetime.now(timezone.utc)).isoformat()
    rows = [
        (series.symbol, b.bar_date.isoformat(), b.open, b.high, b.low, b.close, b.volume, fetched)
        for b in series.bars
    ]
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO ohlcv_daily (symbol, bar_date, open, high, low, close, volume, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, bar_date) DO UPDATE SET
                open = excluded.open, high = excluded.high, low = excluded.low,
                close = excluded.close, volume = excluded.volume, fetched_at = excluded.fetched_at
            """,
            rows,
        )
    return len(rows)


def load_bars(symbol: str, limit: int = HISTORY_BARS) -> BarSeries:
    """Load the most recent `limit` daily bars from the DB, oldest→newest.

    The SQL pulls newest-first (to use the index and apply LIMIT correctly) and
    BarSeries.__post_init__ re-sorts to the oldest→newest contract every
    indicator depends on.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM ohlcv_daily WHERE symbol = ? ORDER BY bar_date DESC LIMIT ?",
            (symbol.strip().upper(), limit),
        ).fetchall()

    bars = [
        Bar(
            bar_date=date.fromisoformat(r["bar_date"]),
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
        )
        for r in rows
    ]
    fetched_at = None
    if rows:
        try:
            fetched_at = datetime.fromisoformat(rows[0]["fetched_at"])
        except (TypeError, ValueError):
            # A malformed timestamp must not sink the load — the bars are still
            # usable and is_stale() falls back to the newest bar_date.
            logger.warning("Unparseable fetched_at for %s; ignoring", symbol)
    return BarSeries(symbol=symbol.strip().upper(), timeframe="daily", bars=bars, fetched_at=fetched_at)


# ── Scores (§10) ─────────────────────────────────────────────────────────────


def save_score(
    symbol: str,
    bar_date: date,
    entry: Any,
    exit_: Any,
    *,
    regime: str | None,
    stale: bool,
) -> None:
    """Persist one day's entry+exit score. Idempotent on (symbol, bar_date).

    `entry` and `exit_` are scoring.Score objects; typed as Any to avoid a
    circular import between store and scoring.

    Components and drivers are stored as JSON blobs rather than normalised
    tables: they are read as a unit by §9's row-expand card, never queried
    across symbols, so a blob is the right shape. If §5's Phase 4 backtest ever
    needs per-component aggregation, that is the point to normalise.
    """
    payload = (
        symbol.strip().upper(),
        bar_date.isoformat(),
        entry.score,
        entry.band,
        exit_.score,
        exit_.band,
        int(entry.counter_trend),
        regime,
        json.dumps({"entry": [c.to_dict() for c in entry.components],
                    "exit": [c.to_dict() for c in exit_.components]}),
        json.dumps({"entry": entry.drivers, "exit": exit_.drivers}),
        int(stale),
        _now(),
    )
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO scores_daily
                (symbol, bar_date, entry_score, entry_band, exit_score, exit_band,
                 counter_trend, regime, components_json, drivers_json, stale, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, bar_date) DO UPDATE SET
                entry_score = excluded.entry_score, entry_band = excluded.entry_band,
                exit_score  = excluded.exit_score,  exit_band  = excluded.exit_band,
                counter_trend = excluded.counter_trend, regime = excluded.regime,
                components_json = excluded.components_json, drivers_json = excluded.drivers_json,
                stale = excluded.stale, computed_at = excluded.computed_at
            """,
            payload,
        )


def latest_score(symbol: str) -> dict[str, Any] | None:
    """The most recent persisted score for one symbol, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM scores_daily WHERE symbol = ? ORDER BY bar_date DESC LIMIT 1",
            (symbol.strip().upper(),),
        ).fetchone()
    return _hydrate_score(row) if row else None


def previous_band(symbol: str, side: str = "entry") -> str | None:
    """The band from the score *before* the latest one — feeds §8.1.

    §8.1: "Alerts fire only on band transitions (Watch → Strong, Strong →
    Prime, Exit crossing 70). Never on wobble within a band." That rule needs
    yesterday's band, which is what this returns.

    Returns None when there is no prior score — a symbol's first-ever score is
    not a transition, and treating it as one would alert on every name the
    moment it joins the watchlist.
    """
    column = "entry_band" if side == "entry" else "exit_band"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {column} AS band FROM scores_daily WHERE symbol = ? ORDER BY bar_date DESC LIMIT 2",
            (symbol.strip().upper(),),
        ).fetchall()
    return rows[1]["band"] if len(rows) >= 2 else None


def score_history(symbol: str, limit: int = 90) -> list[dict[str, Any]]:
    """Score history for one symbol, oldest→newest.

    Default 90 to match §9's "90-day sparkline".
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT bar_date, entry_score, exit_score, entry_band, stale FROM scores_daily "
            "WHERE symbol = ? ORDER BY bar_date DESC LIMIT ?",
            (symbol.strip().upper(), limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def watchlist_with_scores() -> list[dict[str, Any]]:
    """The §9 ranked watchlist table, in one query per row set.

    §9: "ticker · asset-class + source tag · entry score ... · band badge ·
    signal badges · exit score (held) · 90-day sparkline · days since last
    signal."

    Ranked by entry score descending, which is what "ranked watchlist" means
    and what §8.3's digest ("top 5 entry scores") reads. Names with no score
    yet sort last rather than being dropped — a symbol added today but not yet
    scored must still appear on the dashboard, or it looks like the add failed.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT w.symbol, w.asset_class, w.source_tag, w.added_at, w.held, w.notes,
                   w.td_optout,
                   s.bar_date, s.entry_score, s.entry_band, s.exit_score, s.exit_band,
                   s.counter_trend, s.regime, s.drivers_json, s.components_json, s.stale
            FROM watchlist w
            LEFT JOIN scores_daily s
              ON s.symbol = w.symbol
             AND s.bar_date = (SELECT MAX(bar_date) FROM scores_daily WHERE symbol = w.symbol)
            WHERE w.active = 1
            ORDER BY COALESCE(s.entry_score, -1) DESC, w.symbol ASC
            """
        ).fetchall()

    out = []
    for r in rows:
        item = dict(r)
        item["held"] = bool(item.get("held"))
        item["td_optout"] = bool(item.get("td_optout"))
        item["counter_trend"] = bool(item.get("counter_trend"))
        item["stale"] = bool(item.get("stale"))
        drivers = _safe_json(item.pop("drivers_json", None)) or {}
        item["drivers"] = drivers.get("entry", [])
        item["exit_drivers"] = drivers.get("exit", [])

        # §9's "entry score as segmented component bar" needs per-component points
        # on every row, not just an expanded one. Flattened to {key: points} so
        # the frontend can fill each segment immediately from the ranked-table
        # payload — otherwise the bars sit empty until a row is expanded, which
        # reads as missing data rather than as un-fetched detail.
        components = _safe_json(item.pop("components_json", None)) or {}
        item["components"] = {
            c["key"]: c["points"] for c in components.get("entry", []) if isinstance(c, dict) and "key" in c
        }
        item["exit_components"] = {
            c["key"]: c["points"] for c in components.get("exit", []) if isinstance(c, dict) and "key" in c
        }
        item["component_detail"] = components.get("entry", [])
        item["sparkline"] = [h["entry_score"] for h in score_history(item["symbol"], 90)]
        item["days_since_last_signal"] = _days_since_last_signal(item["symbol"])
        item["tradingview_url"] = TRADINGVIEW_URL_TEMPLATE.format(symbol=item["symbol"])
        out.append(item)
    return out


def _days_since_last_signal(symbol: str) -> int | None:
    """§9: "days since last signal". None when the symbol has never signalled."""
    with connect() as conn:
        row = conn.execute(
            "SELECT bar_date FROM signal_events WHERE symbol = ? ORDER BY bar_date DESC LIMIT 1",
            (symbol.strip().upper(),),
        ).fetchone()
    if not row:
        return None
    try:
        return (datetime.now(timezone.utc).date() - date.fromisoformat(row["bar_date"])).days
    except ValueError:
        return None


# ── Signal events (§9 history, §8.1 alerting) ────────────────────────────────


def record_signal_event(
    symbol: str,
    bar_date: date,
    event_type: str,
    direction: str,
    timeframe: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append a signal event. Deduplicated on the natural key.

    The nightly job (§10) is re-runnable, so the same TD 9 must not be logged
    twice. There is no unique constraint on signal_events (it is append-only by
    design and carries an autoincrement id), so dedup happens here with an
    explicit existence check.
    """
    sym = symbol.strip().upper()
    with connect() as conn:
        exists = conn.execute(
            """
            SELECT 1 FROM signal_events
            WHERE symbol = ? AND bar_date = ? AND event_type = ? AND direction = ? AND timeframe = ?
            """,
            (sym, bar_date.isoformat(), event_type, direction, timeframe),
        ).fetchone()
        if exists:
            return
        conn.execute(
            """
            INSERT INTO signal_events (symbol, bar_date, event_type, direction, timeframe, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sym, bar_date.isoformat(), event_type, direction, timeframe,
             json.dumps(detail or {}), _now()),
        )


def signal_events_on(symbol: str, bar_date: date) -> list[dict[str, Any]]:
    """Signal events for one symbol on one bar — feeds the §8.4 TD override."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signal_events WHERE symbol = ? AND bar_date = ? ORDER BY id",
            (symbol.strip().upper(), bar_date.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def signal_events_on_date(bar_date: date) -> list[dict[str, Any]]:
    """Every symbol's signal events on one bar — feeds §8.3's "TD counts line"."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signal_events WHERE bar_date = ? ORDER BY symbol, id",
            (bar_date.isoformat(),),
        ).fetchall()
    return [dict(r) for r in rows]


def signal_history(symbol: str, limit: int = 50) -> list[dict[str, Any]]:
    """§9 row-expand "signal history", newest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signal_events WHERE symbol = ? ORDER BY bar_date DESC, id DESC LIMIT ?",
            (symbol.strip().upper(), limit),
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["detail"] = _safe_json(item.pop("detail_json", None)) or {}
        out.append(item)
    return out


def previous_score_value(symbol: str, side: str = "exit") -> int | None:
    """The numeric score from the score *before* the latest — feeds §8.1.

    §8.1's "Exit crossing 70" is a crossing, not a level: it needs yesterday's
    number to tell a fresh cross from a score that has been above 70 all week.
    Returns None when there is no prior score, which is not a crossing.
    """
    column = "entry_score" if side == "entry" else "exit_score"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {column} AS v FROM scores_daily WHERE symbol = ? ORDER BY bar_date DESC LIMIT 2",
            (symbol.strip().upper(),),
        ).fetchall()
    return int(rows[1]["v"]) if len(rows) >= 2 and rows[1]["v"] is not None else None


# ── Alerts log (§8.6) ────────────────────────────────────────────────────────


def log_alert(symbol: str | None, alert_type: str, payload: dict[str, Any]) -> int:
    """Record an alert before attempting delivery — §8.6 "delivery logged".

    Logging *before* the send is deliberate: an alert that was decided on but
    failed to deliver must still be visible, and §8.2's daily cap must hold even
    if the notification never landed. Otherwise a flapping transport would
    re-alert the same ticker on every retry cycle.

    Returns the row id, for mark_alert_delivered().
    """
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO alerts_log (symbol, alert_type, payload_json, delivered, attempts, created_at)
            VALUES (?, ?, ?, 0, 0, ?)
            """,
            (symbol.strip().upper() if symbol else None, alert_type, json.dumps(payload), _now()),
        )
        return int(cur.lastrowid)


def mark_alert_delivered(alert_id: int, *, delivered: bool, attempts: int, error: str | None) -> None:
    """Close out an alert's delivery attempt — §8.6."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE alerts_log
               SET delivered = ?, attempts = ?, last_error = ?, sent_at = ?
             WHERE id = ?
            """,
            (int(delivered), attempts, error, _now() if delivered else None, alert_id),
        )


def alerts_sent_today(symbol: str, *, as_of: date | None = None) -> bool:
    """Has this ticker already produced an alert today? — §8.2's hard cap.

    Counts *logged* alerts, not delivered ones, for the reason in log_alert():
    the cap is about how often Fred is pinged about a name, and a failed
    delivery has already consumed that day's slot rather than earning a retry
    that would double-notify if the transport recovers.

    Digest rows (symbol IS NULL) are excluded by the symbol filter, so the daily
    digest never counts against a ticker's cap.
    """
    day = (as_of or datetime.now(timezone.utc).date()).isoformat()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM alerts_log
             WHERE symbol = ? AND alert_type = 'signal' AND substr(created_at, 1, 10) = ?
            """,
            (symbol.strip().upper(), day),
        ).fetchone()
    return int(row["c"]) > 0


def recent_alerts(limit: int = 50) -> list[dict[str, Any]]:
    """Alert history, newest first — for the §9 header and for debugging."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["payload"] = _safe_json(item.pop("payload_json", None)) or {}
        item["delivered"] = bool(item["delivered"])
        out.append(item)
    return out


def set_td_optout(symbol: str, optout: bool) -> bool:
    """§8.4: "per-ticker opt-out" from the TD completion override."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE watchlist SET td_optout = ? WHERE symbol = ? AND active = 1",
            (int(optout), symbol.strip().upper()),
        )
        return cur.rowcount > 0


# ── Helpers ──────────────────────────────────────────────────────────────────


def _hydrate_score(row: Any) -> dict[str, Any]:
    """DB row → API-shaped dict, parsing the JSON blob columns."""
    item = dict(row)
    item["components"] = _safe_json(item.pop("components_json", None)) or {}
    item["drivers"] = _safe_json(item.pop("drivers_json", None)) or {}
    item["counter_trend"] = bool(item.get("counter_trend"))
    item["stale"] = bool(item.get("stale"))
    return item


def _safe_json(raw: str | None) -> Any:
    """Parse a JSON column, tolerating corruption.

    A malformed blob costs a row's breakdown detail, not the whole dashboard
    response — the scores themselves live in real columns.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Corrupt JSON column in signals DB; ignoring")
        return None
