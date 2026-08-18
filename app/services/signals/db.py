"""Technical Signals Layer — SQLite persistence.

Implements the tables listed in `docs/technical-signals-layer-scope.md` §10:

    watchlist, sources, intake_events, ohlcv_daily, scores_daily,
    signal_events, alerts_log

The scope doc names Postgres/Supabase (§10); we use SQLite instead — see the
rationale on SIGNALS_DB_PATH in config.py. The schema below is deliberately
plain SQL with no SQLite-only types, so a later port is a dialect change rather
than a redesign.

Why a real database here when the rest of the repo uses JSON files
(app/services/steno/store.py):

  * `ohlcv_daily` and `scores_daily` are unbounded time-series. At 50 tickers ×
    300 bars the bar table alone is 15k rows on day one and grows daily; the
    JSON-blob pattern would rewrite the entire file on every nightly run.
  * §8.1 alerting needs "yesterday's band for this ticker" — a keyed lookup,
    not a full-file scan.
  * The 2026-06-21 audit flagged the JSON stores for non-atomic writes
    (read-modify-write with no lock, torn files on crash). SQLite gives us
    transactional writes for free rather than re-implementing them.

Connection policy: SQLite connections are not thread-safe to share, and this
process is multi-threaded (FastAPI worker threads + the APScheduler job from
app/main.py). We therefore open a fresh connection per operation via the
`connect()` context manager rather than caching one globally. WAL mode lets the
nightly writer and dashboard readers proceed concurrently instead of blocking
on a single writer lock.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.services.signals.config import SIGNALS_DB_PATH


logger = logging.getLogger(__name__)


# ── Schema ───────────────────────────────────────────────────────────────────
# Each statement is idempotent (IF NOT EXISTS) so init_db() is safe to call on
# every process start, which is what app/main.py does.

SCHEMA = [
    # §7: research sources that feed the intake queue (STENO / RV / COWAN / OWN).
    # Kept as a table rather than an enum so §7.5's "paste a note from any
    # source" path can register new ones without a migration.
    """
    CREATE TABLE IF NOT EXISTS sources (
        tag           TEXT PRIMARY KEY,      -- 'STENO' | 'RV' | 'COWAN' | 'OWN'
        label         TEXT NOT NULL,
        last_seen_at  TEXT,                  -- ISO8601 UTC; drives §7's 21-day silence notice
        created_at    TEXT NOT NULL
    )
    """,
    # §1/§2: the watchlist itself, ~30-50 tickers, with §7 provenance.
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        symbol        TEXT PRIMARY KEY,
        asset_class   TEXT NOT NULL,          -- 'equity' | 'etf' | 'commodity' | 'crypto' (§1)
        source_tag    TEXT NOT NULL,          -- §7 provenance; FK to sources.tag
        added_at      TEXT NOT NULL,          -- ISO8601 UTC; §7 "date added, filterable"
        held          INTEGER NOT NULL DEFAULT 0,  -- §5: "Exit alerts only on names toggled held"
        td_optout     INTEGER NOT NULL DEFAULT 0,  -- §8.4: per-ticker TD-override opt-out
        notes         TEXT,
        active        INTEGER NOT NULL DEFAULT 1,  -- soft delete, preserves signal history
        FOREIGN KEY (source_tag) REFERENCES sources(tag)
    )
    """,
    # §3.3: daily OHLCV, ~300 bars per ticker. Composite PK makes the nightly
    # upsert idempotent — re-running the job never duplicates bars.
    """
    CREATE TABLE IF NOT EXISTS ohlcv_daily (
        symbol        TEXT NOT NULL,
        bar_date      TEXT NOT NULL,          -- ISO date 'YYYY-MM-DD' (exchange local trading day)
        open          REAL NOT NULL,
        high          REAL NOT NULL,
        low           REAL NOT NULL,
        close         REAL NOT NULL,
        volume        REAL,                   -- nullable: some FX/index feeds report no volume
        fetched_at    TEXT NOT NULL,          -- ISO8601 UTC; drives the §3.3 staleness check
        PRIMARY KEY (symbol, bar_date)
    )
    """,
    # §10: "compute natives → score → persist". One row per symbol per run.
    # `components_json` stores the per-component breakdown the §9 row-expand
    # card renders; `drivers_json` the plain-language "Firing now" list (§6.3).
    """
    CREATE TABLE IF NOT EXISTS scores_daily (
        symbol          TEXT NOT NULL,
        bar_date        TEXT NOT NULL,
        entry_score     INTEGER NOT NULL,
        entry_band      TEXT NOT NULL,
        exit_score      INTEGER NOT NULL,
        exit_band       TEXT NOT NULL,
        counter_trend   INTEGER NOT NULL DEFAULT 0,  -- §5 regime gate applied
        regime          TEXT,                        -- 'Bullish' | 'Neutral' | 'Bearish' (§4)
        components_json TEXT NOT NULL,
        drivers_json    TEXT NOT NULL,
        stale           INTEGER NOT NULL DEFAULT 0,  -- §3.3: scored on old data → no alerts
        computed_at     TEXT NOT NULL,
        PRIMARY KEY (symbol, bar_date)
    )
    """,
    # §9: "signal history" on the row-expand card, and the audit trail behind
    # §8.1 band transitions. Append-only.
    """
    CREATE TABLE IF NOT EXISTS signal_events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT NOT NULL,
        bar_date      TEXT NOT NULL,
        event_type    TEXT NOT NULL,   -- 'td_setup_9' | 'td_countdown_13' | 'regime_flip' | 'band_change'
        direction     TEXT NOT NULL,   -- 'buy' | 'sell'
        timeframe     TEXT NOT NULL,   -- 'daily' | 'weekly'
        detail_json   TEXT,
        created_at    TEXT NOT NULL
    )
    """,
    # §8.6: "outbound POST, retried, delivery logged."
    """
    CREATE TABLE IF NOT EXISTS alerts_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT,
        alert_type    TEXT NOT NULL,   -- 'signal' | 'intake' | 'digest' (§8.6 payload `type`)
        payload_json  TEXT NOT NULL,
        delivered     INTEGER NOT NULL DEFAULT 0,
        attempts      INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT,
        sent_at       TEXT,
        created_at    TEXT NOT NULL
    )
    """,
    # §7.4: the review queue. Nothing auto-enters the watchlist — rows sit here
    # in 'pending' until Fred approves or dismisses.
    """
    CREATE TABLE IF NOT EXISTS intake_events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        source_tag     TEXT NOT NULL,
        published_date TEXT,
        subject        TEXT,
        raw_ref        TEXT,            -- pointer to the stored email/PDF, not the body itself
        actions_json   TEXT NOT NULL,   -- §7.3 extraction result: [{ticker, action, instrument_note}]
        status         TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'dismissed'
        resolved_at    TEXT,
        created_at     TEXT NOT NULL
    )
    """,
]

# Indexes for the read paths the dashboard actually uses. Created separately so
# the SCHEMA list stays readable as a table inventory.
INDEXES = [
    # §3.3 staleness check + indicator load: "most recent N bars for this symbol".
    "CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_date ON ohlcv_daily(symbol, bar_date DESC)",
    # §9 ranked watchlist table: "every symbol's latest score".
    "CREATE INDEX IF NOT EXISTS idx_scores_date ON scores_daily(bar_date DESC)",
    # §9 row-expand "signal history" for one symbol.
    "CREATE INDEX IF NOT EXISTS idx_events_symbol ON signal_events(symbol, bar_date DESC)",
    # §8.2 "max one alert per ticker per day" needs today's alerts for a symbol.
    "CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts_log(symbol, created_at DESC)",
    # §7.4 the pending strip is the only hot read on intake.
    "CREATE INDEX IF NOT EXISTS idx_intake_status ON intake_events(status, created_at DESC)",
]

# §7: seeded so the watchlist FK is satisfiable from a cold start. `sources` is
# a table rather than an enum specifically so §7.5's paste-a-note path can add
# rows later without a migration.
SEED_SOURCES = [
    ("STENO", "Steno Research"),
    ("RV",    "Real Vision"),
    ("COWAN", "Cowan"),
    ("OWN",   "Own idea"),
]


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection, commit on clean exit, roll back on exception.

    A fresh connection per operation is deliberate — see the module docstring
    on connection policy. The cost is a sub-millisecond open on a local file,
    which is irrelevant next to the network fetch that dominates every code
    path that touches this database.
    """
    path = db_path or SIGNALS_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    # Rows behave like dicts, so callers read row["close"] rather than row[4].
    conn.row_factory = sqlite3.Row
    # WAL: readers (dashboard requests) don't block the writer (nightly job).
    conn.execute("PRAGMA journal_mode=WAL")
    # FKs are off by default in SQLite and must be enabled per connection.
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the initial schema shipped. `CREATE TABLE IF NOT EXISTS`
# is a no-op on an existing table, so a new column in SCHEMA above would never
# reach a database created by an earlier version — it has to be ALTERed in.
#
# Format: (table, column, full column definition).
# Keep entries here forever once added; they are cheap and removing one would
# break any deployment that skipped a version.
MIGRATIONS: list[tuple[str, str, str]] = [
    # §8.4: "per-ticker opt-out" from the TD completion override.
    ("watchlist", "td_optout", "INTEGER NOT NULL DEFAULT 0"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add any columns in MIGRATIONS that the live schema is missing.

    Uses PRAGMA table_info rather than catching the ALTER error, so a genuine
    failure (locked DB, disk full) still surfaces instead of being swallowed as
    "column already exists".
    """
    for table, column, definition in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            # Table doesn't exist yet — SCHEMA will have just created it with the
            # column already present, so there is nothing to migrate.
            continue
        if column not in existing:
            logger.info("Migrating signals DB: adding %s.%s", table, column)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(db_path: Path | None = None) -> None:
    """Create tables, indexes and seed rows. Idempotent — safe on every start."""
    with connect(db_path) as conn:
        for statement in SCHEMA:
            conn.execute(statement)
        _apply_migrations(conn)
        for statement in INDEXES:
            conn.execute(statement)
        now = _utc_now()
        for tag, label in SEED_SOURCES:
            # INSERT OR IGNORE preserves an existing row's last_seen_at, which
            # §7's 21-day silence notice depends on across restarts.
            conn.execute(
                "INSERT OR IGNORE INTO sources (tag, label, last_seen_at, created_at) VALUES (?, ?, NULL, ?)",
                (tag, label, now),
            )
    logger.info("Signals DB ready at %s", db_path or SIGNALS_DB_PATH)


def _utc_now() -> str:
    """ISO8601 UTC timestamp. Imported lazily-ish to keep the module header thin."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """sqlite3.Row → plain dicts, for JSON serialisation at the API boundary."""
    return [dict(row) for row in rows]
