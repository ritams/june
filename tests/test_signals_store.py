"""Tests for the signals SQLite persistence layer (scope doc §10).

Every test runs against a temporary database via the `db_path` fixture, which
monkeypatches `SIGNALS_DB_PATH` so nothing touches `runtime/signals/signals.db`.

The emphasis is on idempotency. §10's nightly job is re-runnable by design (a
restart or a manual /api/signals/run can fire it twice in one day), so every
write path is tested for "running it twice produces the same state as running it
once".
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.services.signals import db as signals_db
from app.services.signals import store
from app.services.signals.bars import Bar, BarSeries
from app.services.signals.scoring import Component, Score


@pytest.fixture(autouse=True)
def db_path(tmp_path, monkeypatch):
    """Point every store operation at a fresh temporary database."""
    path = tmp_path / "signals_test.db"
    monkeypatch.setattr(signals_db, "SIGNALS_DB_PATH", path)
    monkeypatch.setattr(store, "connect", lambda: signals_db.connect(path))
    signals_db.init_db(path)
    return path


def _bars(symbol="TEST", n=10, start=date(2026, 1, 1), volume=1000.0) -> BarSeries:
    from datetime import timedelta

    return BarSeries(
        symbol=symbol,
        timeframe="daily",
        bars=[
            Bar(bar_date=start + timedelta(days=i), open=100.0 + i, high=101.0 + i,
                low=99.0 + i, close=100.5 + i, volume=volume)
            for i in range(n)
        ],
        fetched_at=datetime.now(timezone.utc),
    )


def _score(symbol="TEST", side="entry", value=75) -> Score:
    return Score(
        symbol=symbol,
        side=side,
        score=value,
        band="Strong",
        raw_score=value,
        components=[Component("td_sequential", "TD Sequential", 18.0, 25, "Daily buy setup 9")],
        drivers=["Daily buy setup 9 complete"],
    )


# ── Schema ───────────────────────────────────────────────────────────────────


def test_init_db_creates_every_table_from_scope_doc_section_10():
    """§10 lists seven tables; all must exist."""
    expected = {"watchlist", "sources", "intake_events", "ohlcv_daily",
                "scores_daily", "signal_events", "alerts_log"}
    with signals_db.connect(signals_db.SIGNALS_DB_PATH) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert expected.issubset({r["name"] for r in rows})


def test_init_db_is_idempotent():
    """Called on every process start — must never fail or duplicate seed rows."""
    signals_db.init_db(signals_db.SIGNALS_DB_PATH)
    signals_db.init_db(signals_db.SIGNALS_DB_PATH)
    with signals_db.connect(signals_db.SIGNALS_DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM sources").fetchone()["c"]
    assert count == len(signals_db.SEED_SOURCES)


def test_seed_sources_cover_the_scope_doc_provenance_tags():
    """§7: "source tag (STENO / RV / COWAN / OWN)"."""
    with signals_db.connect(signals_db.SIGNALS_DB_PATH) as conn:
        tags = {r["tag"] for r in conn.execute("SELECT tag FROM sources").fetchall()}
    assert {"STENO", "RV", "COWAN", "OWN"} == tags


# ── Watchlist (§1, §7) ───────────────────────────────────────────────────────


def test_add_and_list_watchlist():
    store.add_to_watchlist("aapl", asset_class="equity", source_tag="steno")
    rows = store.list_watchlist()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"          # normalised to upper
    assert rows[0]["source_tag"] == "STENO"     # normalised to upper
    assert rows[0]["active"] == 1


def test_add_rejects_an_unknown_source_tag():
    """§7 provenance is an audit trail — a bad tag must fail, not coerce."""
    with pytest.raises(ValueError, match="Unknown source tag"):
        store.add_to_watchlist("AAPL", source_tag="BLOOMBERG")


def test_add_rejects_an_empty_symbol():
    with pytest.raises(ValueError):
        store.add_to_watchlist("   ")


def test_re_adding_is_idempotent_and_does_not_duplicate():
    store.add_to_watchlist("AAPL", source_tag="OWN")
    store.add_to_watchlist("AAPL", source_tag="RV")
    rows = store.list_watchlist()
    assert len(rows) == 1
    assert rows[0]["source_tag"] == "RV"     # provenance updated by the re-add


def test_remove_is_a_soft_delete_that_preserves_history():
    """§9 shows signal history and §5's backtest replays scores — both need the past."""
    store.add_to_watchlist("AAPL")
    store.record_signal_event("AAPL", date(2026, 6, 1), "td_setup_9", "buy", "daily")
    assert store.remove_from_watchlist("AAPL") is True
    assert store.list_watchlist() == []
    assert store.list_watchlist(active_only=False)[0]["active"] == 0
    # The event survives the removal.
    assert len(store.signal_history("AAPL")) == 1


def test_remove_a_missing_symbol_returns_false():
    assert store.remove_from_watchlist("NOPE") is False


def test_re_adding_a_removed_symbol_reactivates_it():
    """§7.4: a note can re-recommend a name that was previously removed."""
    store.add_to_watchlist("AAPL", source_tag="OWN")
    store.remove_from_watchlist("AAPL")
    store.add_to_watchlist("AAPL", source_tag="STENO")
    rows = store.list_watchlist()
    assert len(rows) == 1 and rows[0]["source_tag"] == "STENO"


def test_set_held_toggles_and_reports_missing():
    """§5: "Exit alerts only on names toggled held"."""
    store.add_to_watchlist("AAPL")
    assert store.set_held("AAPL", True) is True
    assert store.list_watchlist()[0]["held"] == 1
    assert store.set_held("AAPL", False) is True
    assert store.list_watchlist()[0]["held"] == 0
    assert store.set_held("MISSING", True) is False


# ── OHLCV (§3.3) ─────────────────────────────────────────────────────────────


def test_upsert_bars_then_load_round_trips():
    written = store.upsert_bars(_bars(n=5))
    assert written == 5
    loaded = store.load_bars("TEST")
    assert len(loaded) == 5
    assert loaded.bars[0].bar_date == date(2026, 1, 1)


def test_upsert_bars_is_idempotent():
    """The nightly job is re-runnable; bars must not duplicate."""
    store.upsert_bars(_bars(n=5))
    store.upsert_bars(_bars(n=5))
    assert len(store.load_bars("TEST")) == 5


def test_upsert_bars_updates_revised_prices():
    """Providers do revise recent bars — the newer value must win."""
    store.upsert_bars(_bars(n=3))
    revised = _bars(n=3)
    revised.bars[-1] = Bar(bar_date=revised.bars[-1].bar_date, open=1.0, high=2.0,
                           low=0.5, close=1.5, volume=42.0)
    store.upsert_bars(revised)
    loaded = store.load_bars("TEST")
    assert loaded.bars[-1].close == pytest.approx(1.5)
    assert len(loaded) == 3


def test_load_bars_returns_oldest_to_newest():
    """The ordering contract every indicator depends on."""
    store.upsert_bars(_bars(n=20))
    loaded = store.load_bars("TEST")
    dates = [b.bar_date for b in loaded.bars]
    assert dates == sorted(dates)


def test_load_bars_limit_keeps_the_most_recent():
    store.upsert_bars(_bars(n=20))
    loaded = store.load_bars("TEST", limit=5)
    assert len(loaded) == 5
    assert loaded.bars[-1].bar_date == date(2026, 1, 20)


def test_load_bars_for_unknown_symbol_is_empty_not_an_error():
    loaded = store.load_bars("NOPE")
    assert len(loaded) == 0
    assert loaded.is_stale() is True     # empty is stale by definition


def test_upsert_empty_series_writes_nothing():
    assert store.upsert_bars(BarSeries("TEST", "daily", [])) == 0


def test_null_volume_survives_the_round_trip():
    """Missing volume must stay None, not become 0 — see Bar.volume."""
    series = _bars(n=3, volume=None)
    store.upsert_bars(series)
    assert all(b.volume is None for b in store.load_bars("TEST").bars)


# ── Scores (§10) ─────────────────────────────────────────────────────────────


def test_save_and_read_latest_score():
    store.add_to_watchlist("TEST")
    store.save_score("TEST", date(2026, 6, 1), _score(value=80),
                     _score(side="exit", value=30), regime="Bullish", stale=False)
    latest = store.latest_score("TEST")
    assert latest["entry_score"] == 80
    assert latest["exit_score"] == 30
    assert latest["regime"] == "Bullish"
    assert latest["components"]["entry"][0]["key"] == "td_sequential"


def test_save_score_is_idempotent_on_symbol_and_date():
    store.save_score("TEST", date(2026, 6, 1), _score(value=50), _score(side="exit", value=10),
                     regime="Neutral", stale=False)
    store.save_score("TEST", date(2026, 6, 1), _score(value=90), _score(side="exit", value=20),
                     regime="Bullish", stale=False)
    assert store.latest_score("TEST")["entry_score"] == 90
    assert len(store.score_history("TEST")) == 1


def test_latest_score_for_unknown_symbol_is_none():
    assert store.latest_score("NOPE") is None


def test_score_history_is_oldest_to_newest():
    for day in (3, 1, 2):
        store.save_score("TEST", date(2026, 6, day), _score(value=day * 10),
                         _score(side="exit"), regime="Neutral", stale=False)
    history = store.score_history("TEST")
    assert [h["bar_date"] for h in history] == ["2026-06-01", "2026-06-02", "2026-06-03"]


def test_previous_band_returns_the_band_before_the_latest():
    """§8.1 alerting compares today's band to yesterday's."""
    store.save_score("TEST", date(2026, 6, 1), _score(value=55), _score(side="exit"),
                     regime="Neutral", stale=False)
    s2 = _score(value=90)
    s2.band = "Prime"
    store.save_score("TEST", date(2026, 6, 2), s2, _score(side="exit"), regime="Bullish", stale=False)
    assert store.previous_band("TEST") == "Strong"     # the first score's band


def test_previous_band_is_none_on_a_first_ever_score():
    """A first score is not a transition — alerting on it would ping every new name."""
    store.save_score("TEST", date(2026, 6, 1), _score(), _score(side="exit"),
                     regime="Neutral", stale=False)
    assert store.previous_band("TEST") is None


def test_stale_flag_persists():
    """§3.3: the dashboard must be able to badge a stale score."""
    store.save_score("TEST", date(2026, 6, 1), _score(), _score(side="exit"),
                     regime=None, stale=True)
    assert store.latest_score("TEST")["stale"] is True


# ── Signal events (§9, §8.1) ─────────────────────────────────────────────────


def test_record_signal_event_deduplicates_on_the_natural_key():
    """The nightly job is re-runnable; the same TD 9 must not log twice."""
    for _ in range(3):
        store.record_signal_event("TEST", date(2026, 6, 1), "td_setup_9", "buy", "daily",
                                  {"perfected": True})
    assert len(store.signal_history("TEST")) == 1


def test_different_timeframes_are_distinct_events():
    """A daily 9 and a weekly 9 on the same date are two real events."""
    store.record_signal_event("TEST", date(2026, 6, 1), "td_setup_9", "buy", "daily")
    store.record_signal_event("TEST", date(2026, 6, 1), "td_setup_9", "buy", "weekly")
    assert len(store.signal_history("TEST")) == 2


def test_signal_history_parses_the_detail_blob():
    store.record_signal_event("TEST", date(2026, 6, 1), "td_setup_9", "buy", "daily",
                              {"perfected": True, "tdst": 123.5})
    event = store.signal_history("TEST")[0]
    assert event["detail"]["perfected"] is True
    assert event["detail"]["tdst"] == 123.5


# ── The §9 ranked table ──────────────────────────────────────────────────────


def test_watchlist_with_scores_ranks_by_entry_score_descending():
    for sym, val in (("LOW", 20), ("HIGH", 90), ("MID", 55)):
        store.add_to_watchlist(sym)
        store.save_score(sym, date(2026, 6, 1), _score(symbol=sym, value=val),
                         _score(symbol=sym, side="exit"), regime="Neutral", stale=False)
    rows = store.watchlist_with_scores()
    assert [r["symbol"] for r in rows] == ["HIGH", "MID", "LOW"]


def test_unscored_names_sort_last_but_are_not_dropped():
    """A name added today must still appear, or the add looks like it failed."""
    store.add_to_watchlist("SCORED")
    store.save_score("SCORED", date(2026, 6, 1), _score(symbol="SCORED", value=10),
                     _score(symbol="SCORED", side="exit"), regime="Neutral", stale=False)
    store.add_to_watchlist("UNSCORED")
    rows = store.watchlist_with_scores()
    assert [r["symbol"] for r in rows] == ["SCORED", "UNSCORED"]
    assert rows[-1]["entry_score"] is None


def test_watchlist_with_scores_includes_the_section_9_row_fields():
    """§9 lists the columns the ranked table renders — all must be present."""
    store.add_to_watchlist("TEST", asset_class="crypto", source_tag="STENO", held=True)
    store.save_score("TEST", date(2026, 6, 1), _score(), _score(side="exit"),
                     regime="Bullish", stale=False)
    row = store.watchlist_with_scores()[0]
    for key in ("symbol", "asset_class", "source_tag", "entry_score", "entry_band",
                "exit_score", "held", "sparkline", "days_since_last_signal",
                "drivers", "tradingview_url", "counter_trend", "stale"):
        assert key in row, f"§9 row is missing {key}"
    assert row["held"] is True
    assert "TEST" in row["tradingview_url"]


def test_days_since_last_signal_is_none_when_never_signalled():
    store.add_to_watchlist("TEST")
    assert store.watchlist_with_scores()[0]["days_since_last_signal"] is None


def test_removed_names_do_not_appear_in_the_ranked_table():
    store.add_to_watchlist("TEST")
    store.remove_from_watchlist("TEST")
    assert store.watchlist_with_scores() == []


def test_corrupt_json_column_degrades_gracefully():
    """A bad blob costs one row's detail, not the whole dashboard response."""
    store.add_to_watchlist("TEST")
    store.save_score("TEST", date(2026, 6, 1), _score(), _score(side="exit"),
                     regime="Neutral", stale=False)
    with signals_db.connect(signals_db.SIGNALS_DB_PATH) as conn:
        conn.execute("UPDATE scores_daily SET drivers_json = 'not json' WHERE symbol = 'TEST'")
    row = store.watchlist_with_scores()[0]
    assert row["drivers"] == []
    assert row["entry_score"] is not None      # the real columns still work
