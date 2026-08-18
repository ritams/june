"""Tests for engine orchestration and the HTTP API (scope doc §6, §9, §10).

All network access is replaced by `FakeProvider`, so these run offline and
deterministically. Two scope-doc rules get the most attention because they are
the ones a passing-looking engine could silently violate:

  * **§6.4 "Lookups never trigger alerts."** Tested by asserting a lookup writes
    nothing to `scores_daily` or `signal_events` — the tables the alerting layer
    reads from. This is a structural guarantee, not a policy the alerting code
    has to remember.

  * **§3.3 staleness.** "the run flags 'stale' ... never scores silently on old
    data." Tested from both directions: a stale series must be flagged, and a
    provider outage must fall back to cached bars rather than dropping the
    symbol.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.signals import db as signals_db
from app.services.signals import engine, store
from app.services.signals.api import router as signals_router
from app.services.signals.bars import Bar, BarSeries
from app.services.signals.config import HISTORY_BARS
from app.services.signals.providers import ProviderError, SymbolRef


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _synthetic_bars(
    symbol: str,
    n: int = 320,
    *,
    end: date | None = None,
    trend: float = 0.0004,
    volume: float | None = 1_000_000.0,
) -> BarSeries:
    """A deterministic bar series with a mild trend and a repeating wiggle.

    The wiggle matters: a perfectly monotonic series never produces a TD price
    flip (see test_buy_setup_requires_a_bearish_price_flip), so a smooth ramp
    would exercise none of the TD code paths.
    """
    end_date = end or datetime.now(timezone.utc).date()
    start = end_date - timedelta(days=n - 1)
    bars = []
    price = 100.0
    for i in range(n):
        price *= 1.0 + trend
        wiggle = ((i * 7919) % 23 - 11) * 0.12
        close = price + wiggle
        bars.append(
            Bar(
                bar_date=start + timedelta(days=i),
                open=close - 0.1,
                high=close + 0.6,
                low=close - 0.6,
                close=close,
                volume=volume,
            )
        )
    return BarSeries(symbol=symbol, timeframe="daily", bars=bars, fetched_at=datetime.now(timezone.utc))


class FakeProvider:
    """An OHLCVProvider that serves synthetic bars and can be told to fail."""

    name = "fake"

    def __init__(self, *, bars: int = 320, fail_for: set[str] | None = None,
                 end: date | None = None, volume: float | None = 1_000_000.0):
        self.bars = bars
        self.fail_for = fail_for or set()
        self.end = end
        self.volume = volume
        self.calls: list[str] = []

    def daily_bars(self, symbol: str, bars: int = 300) -> BarSeries:
        self.calls.append(symbol)
        if symbol in self.fail_for:
            raise ProviderError(f"simulated outage for {symbol}")
        return _synthetic_bars(symbol, min(self.bars, bars) if self.bars else bars,
                               end=self.end, volume=self.volume)

    def search(self, query: str, limit: int = 10) -> list[SymbolRef]:
        return [SymbolRef(symbol=query.upper(), name=query.upper(), asset_class="equity")]


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own database; nothing touches runtime/signals/."""
    path = tmp_path / "engine_test.db"
    monkeypatch.setattr(signals_db, "SIGNALS_DB_PATH", path)
    monkeypatch.setattr(store, "connect", lambda: signals_db.connect(path))
    signals_db.init_db(path)
    return path


@pytest.fixture
def provider(monkeypatch):
    """Install a FakeProvider into both the engine and the API layer."""
    fake = FakeProvider()
    monkeypatch.setattr(engine, "get_provider", lambda name=None: fake)
    import app.services.signals.api as signals_api

    monkeypatch.setattr(signals_api, "get_provider", lambda name=None: fake)
    return fake


@pytest.fixture
def client(provider):
    """A TestClient over just the signals router — no auth, no scheduler.

    Mounting the router on a bare FastAPI app rather than importing app.main
    keeps these tests independent of OAuth config, the MCP server and the
    APScheduler startup hooks.
    """
    app = FastAPI()
    app.include_router(signals_router)
    return TestClient(app)


def _row_count(table: str) -> int:
    with signals_db.connect(signals_db.SIGNALS_DB_PATH) as conn:
        return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


# ── Evaluation core (§6.2 "identical scoring code") ──────────────────────────


def test_lookup_returns_a_full_evaluation(provider):
    ev = engine.lookup("AAPL")
    assert ev.symbol == "AAPL"
    assert 0 <= ev.entry.score <= 100
    assert 0 <= ev.exit.score <= 100
    assert ev.entry.band in ("Prime", "Strong", "Watch", "No signal")
    # The fixture can serve 320 bars but the engine asks for HISTORY_BARS (§3.3's
    # "~300 bars history per ticker"), so 300 is the expected load.
    assert ev.bars_loaded == HISTORY_BARS
    assert ev.thin_history is False


def test_lookup_below_the_absolute_floor_raises(monkeypatch):
    """Fewer than MIN_BARS_TO_SCORE bars is noise, not a score."""
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(bars=30))
    with pytest.raises(engine.InsufficientHistory):
        engine.lookup("NEW")


def test_thin_history_is_flagged_not_hidden(monkeypatch):
    """§9 must be able to badge a partial score rather than present it as complete."""
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(bars=120))
    ev = engine.lookup("NEWISH")
    assert ev.thin_history is True
    assert any("200D" in w for w in ev.warnings)
    # The score still computes from what IS available.
    assert ev.entry.score >= 0


def test_evaluation_to_dict_is_json_safe(provider):
    import json

    json.dumps(engine.lookup("AAPL").to_dict())


def test_evaluation_exposes_a_tradingview_link(provider):
    """§3.1/§6.4: Chameleon is manual, so every result links out to the chart."""
    payload = engine.lookup("AAPL").to_dict()
    assert "tradingview.com" in payload["tradingview_url"]
    assert "AAPL" in payload["tradingview_url"]


# ── §6.4: lookups never trigger alerts ───────────────────────────────────────


def test_lookup_does_not_write_scores_or_signal_events(provider):
    """§6.4 "Lookups never trigger alerts" — enforced structurally.

    The alerting layer (§8) reads from scores_daily and signal_events. If a
    lookup wrote to either, a user typing a symbol into the §6 search bar could
    trigger a push notification. Asserting the tables stay empty makes that
    impossible regardless of what the alerting code later does.
    """
    engine.lookup("AAPL")
    assert _row_count("scores_daily") == 0
    assert _row_count("signal_events") == 0


def test_lookup_does_cache_bars_for_the_next_lookup(provider):
    """§6.2's 2-second budget depends on the second lookup hitting SQLite."""
    engine.lookup("AAPL")
    assert _row_count("ohlcv_daily") > 0
    assert len(store.load_bars("AAPL")) == HISTORY_BARS


def test_lookup_with_persist_bars_false_writes_nothing(provider):
    engine.lookup("AAPL", persist_bars=False)
    assert _row_count("ohlcv_daily") == 0


def test_lookup_survives_a_cache_write_failure(provider, monkeypatch):
    """The user asked for a score; a cache miss must not deny them one."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "upsert_bars", boom)
    ev = engine.lookup("AAPL")
    assert ev.entry.score >= 0


# ── §3.3 staleness ───────────────────────────────────────────────────────────


def test_fresh_data_is_not_stale(provider):
    assert engine.lookup("AAPL").stale is False


def test_old_data_is_flagged_stale_and_warns(monkeypatch):
    """§3.3: "never scores silently on old data"."""
    old_end = datetime.now(timezone.utc).date() - timedelta(days=30)
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(end=old_end))
    ev = engine.lookup("STALE")
    assert ev.stale is True
    assert any("stale" in w.lower() for w in ev.warnings)
    # Still scored — §3.3 flags and suppresses alerts, it does not blank the row.
    assert ev.entry.score >= 0


# ── Nightly run (§10) ────────────────────────────────────────────────────────


def test_run_daily_on_an_empty_watchlist_is_a_noop(provider):
    result = engine.run_daily()
    assert result.scored == []
    assert result.failed == {}


def test_run_daily_scores_and_persists_the_watchlist(provider):
    for sym in ("AAA", "BBB", "CCC"):
        store.add_to_watchlist(sym)
    result = engine.run_daily()
    assert sorted(result.scored) == ["AAA", "BBB", "CCC"]
    assert _row_count("scores_daily") == 3
    assert store.latest_score("AAA") is not None


def test_run_daily_is_idempotent(provider):
    store.add_to_watchlist("AAA")
    engine.run_daily()
    engine.run_daily()
    assert _row_count("scores_daily") == 1


def test_run_daily_isolates_a_single_symbol_failure(monkeypatch):
    """One bad ticker must not abort the run for the other 49."""
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(fail_for={"BAD"}))
    for sym in ("GOOD1", "BAD", "GOOD2"):
        store.add_to_watchlist(sym)
    result = engine.run_daily()
    assert sorted(result.scored) == ["GOOD1", "GOOD2"]
    assert "BAD" in result.failed


def test_run_daily_falls_back_to_cached_bars_on_a_fetch_failure(provider, monkeypatch):
    """§3.3: an outage shows the last known state as stale, not an empty row."""
    store.add_to_watchlist("AAA")
    engine.run_daily()                       # populates the bar cache
    assert _row_count("scores_daily") == 1

    # Now simulate the provider going down entirely.
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(fail_for={"AAA"}))
    result = engine.run_daily()
    # Scored from cache rather than failed outright.
    assert "AAA" in result.scored
    assert "AAA" not in result.failed


def test_run_daily_skips_symbols_with_too_little_history(monkeypatch):
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(bars=20))
    store.add_to_watchlist("NEW")
    result = engine.run_daily()
    assert "NEW" in result.skipped
    assert result.scored == []


def test_run_daily_can_target_one_symbol(provider):
    for sym in ("AAA", "BBB"):
        store.add_to_watchlist(sym)
    result = engine.run_daily(["AAA"])
    assert result.scored == ["AAA"]


def test_run_daily_records_td_events_it_finds(provider):
    """§9's signal history is populated by the run, deduplicated across re-runs."""
    store.add_to_watchlist("AAA")
    engine.run_daily()
    first = _row_count("signal_events")
    engine.run_daily()
    assert _row_count("signal_events") == first     # dedup held


def test_run_result_partitions_every_symbol(monkeypatch):
    """scored / stale / failed / skipped must together account for the input."""
    class Mixed(FakeProvider):
        def daily_bars(self, symbol, bars=300):
            if symbol == "FAIL":
                raise ProviderError("down")
            if symbol == "SHORT":
                return _synthetic_bars(symbol, 20)
            return _synthetic_bars(symbol, 320)

    monkeypatch.setattr(engine, "get_provider", lambda name=None: Mixed())
    for sym in ("OK", "FAIL", "SHORT"):
        store.add_to_watchlist(sym)
    r = engine.run_daily()
    assert r.scored == ["OK"]
    assert "FAIL" in r.failed
    assert "SHORT" in r.skipped


# ── Missing volume (cross-asset, §1) ─────────────────────────────────────────


def test_a_symbol_with_no_volume_still_scores(monkeypatch):
    """§1 includes commodities; those feeds report no volume. §5's volume row
    must score 0 without breaking the other five components."""
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(volume=None))
    ev = engine.lookup("GC=F")
    volume_component = next(c for c in ev.entry.components if c.key == "volume")
    assert volume_component.points == 0
    assert "no volume data" in volume_component.detail.lower()
    assert ev.entry.score >= 0


# ── HTTP API (§9, §10) ───────────────────────────────────────────────────────


def test_get_watchlist_empty(client):
    body = client.get("/api/signals/watchlist").json()
    assert body["count"] == 0
    assert body["watchlist"] == []
    assert body["last_scored_bar"] is None


def test_post_and_get_watchlist(client):
    r = client.post("/api/signals/watchlist", params={"symbol": "AAPL", "source_tag": "STENO"})
    assert r.status_code == 200
    assert r.json()["entry"]["symbol"] == "AAPL"
    body = client.get("/api/signals/watchlist").json()
    assert body["count"] == 1
    assert body["watchlist"][0]["source_tag"] == "STENO"


def test_post_watchlist_rejects_a_bad_source_tag(client):
    r = client.post("/api/signals/watchlist", params={"symbol": "AAPL", "source_tag": "NOPE"})
    assert r.status_code == 400
    assert "Unknown source tag" in r.json()["detail"]


def test_delete_watchlist(client):
    client.post("/api/signals/watchlist", params={"symbol": "AAPL"})
    assert client.delete("/api/signals/watchlist/AAPL").status_code == 200
    assert client.get("/api/signals/watchlist").json()["count"] == 0


def test_delete_missing_symbol_is_404(client):
    assert client.delete("/api/signals/watchlist/NOPE").status_code == 404


def test_held_toggle(client):
    client.post("/api/signals/watchlist", params={"symbol": "AAPL"})
    r = client.post("/api/signals/watchlist/AAPL/held", params={"held": True})
    assert r.json()["held"] is True
    assert client.get("/api/signals/watchlist").json()["watchlist"][0]["held"] is True


def test_held_toggle_missing_symbol_is_404(client):
    assert client.post("/api/signals/watchlist/NOPE/held").status_code == 404


def test_lookup_endpoint_returns_the_section_6_3_card(client):
    """§6.3: score + band, exit score, "Firing now" list, component breakdown."""
    body = client.get("/api/signals/lookup/AAPL").json()
    assert body["symbol"] == "AAPL"
    assert "entry" in body and "exit" in body
    assert isinstance(body["entry"]["drivers"], list)
    assert len(body["entry"]["components"]) == 6      # §5 has six entry rows
    assert "indicators" in body and "td" in body


def test_lookup_endpoint_404s_on_insufficient_history(client, monkeypatch):
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(bars=20))
    assert client.get("/api/signals/lookup/NEW").status_code == 404


def test_lookup_endpoint_503s_on_a_provider_outage(client, monkeypatch):
    """503 not 404: yfinance cannot distinguish a bad symbol from rate limiting."""
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(fail_for={"X"}))
    assert client.get("/api/signals/lookup/X").status_code == 503


def test_search_endpoint_reports_whether_autocomplete_is_available(client):
    """The §6.1 gap must be advertised, not silently degraded."""
    body = client.get("/api/signals/search", params={"q": "AAPL"}).json()
    assert body["results"][0]["symbol"] == "AAPL"
    assert "autocomplete" in body


def test_run_endpoint_scores_the_watchlist(client):
    client.post("/api/signals/watchlist", params={"symbol": "AAPL"})
    body = client.post("/api/signals/run").json()
    assert body["result"]["scored"] == ["AAPL"]
    assert client.get("/api/signals/watchlist").json()["watchlist"][0]["entry_score"] is not None


def test_history_endpoint(client):
    client.post("/api/signals/watchlist", params={"symbol": "AAPL"})
    client.post("/api/signals/run")
    body = client.get("/api/signals/history/AAPL").json()
    assert body["symbol"] == "AAPL"
    assert len(body["scores"]) == 1
    assert body["latest"] is not None


def test_status_endpoint_separates_stale_from_unscored(client):
    """Two different failures; collapsing them would hide which occurred."""
    client.post("/api/signals/watchlist", params={"symbol": "AAPL"})
    body = client.get("/api/signals/status").json()
    assert body["watchlist_count"] == 1
    assert body["unscored"] == ["AAPL"]
    assert body["stale_symbols"] == []
    assert "provider" in body


def test_sources_endpoint_lists_the_provenance_tags(client):
    assert set(client.get("/api/signals/sources").json()["sources"]) == {"STENO", "RV", "COWAN", "OWN"}


# ── /bars endpoint (§6.3 mini chart) ─────────────────────────────────────────


def test_bars_endpoint_returns_aligned_ma_series(client):
    """§6.3's "mini chart with 21E/50/200" needs the SERIES, not just latest values."""
    client.post("/api/signals/watchlist", params={"symbol": "AAPL"})
    client.post("/api/signals/run")
    body = client.get("/api/signals/bars/AAPL", params={"limit": 120}).json()
    assert body["symbol"] == "AAPL"
    assert body["source"] == "persisted"
    assert body["count"] == 120
    for key in ("date", "close", "ema21", "sma50", "sma200"):
        assert key in body["bars"][0]


def test_bars_slow_average_is_populated_at_the_window_edge(client):
    """Averages are computed over full history then sliced.

    Computing them over the slice instead would leave the 200 SMA null for the
    first 200 of the 120 bars shown — i.e. null everywhere — and the chart would
    silently lose its most important line.
    """
    client.post("/api/signals/watchlist", params={"symbol": "AAPL"})
    client.post("/api/signals/run")
    bars = client.get("/api/signals/bars/AAPL", params={"limit": 60}).json()["bars"]
    assert bars[0]["sma200"] is not None
    assert bars[0]["ema21"] is not None


def test_bars_endpoint_falls_back_to_a_live_fetch_for_an_unscored_symbol(client):
    """A §6 lookup on a name that is not on the watchlist must still chart."""
    body = client.get("/api/signals/bars/TSLA").json()
    assert body["source"] == "live"
    assert body["count"] > 0


def test_bars_endpoint_404s_when_history_is_too_short(client, monkeypatch):
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(bars=20))
    assert client.get("/api/signals/bars/NEW").status_code == 404


def test_bars_endpoint_503s_on_a_provider_outage(client, monkeypatch):
    monkeypatch.setattr(engine, "get_provider", lambda name=None: FakeProvider(fail_for={"X"}))
    assert client.get("/api/signals/bars/X").status_code == 503


# ── §3.3 session cache for lookups ───────────────────────────────────────────


def test_lookup_serves_from_cache_on_a_repeat_call(provider):
    """§3.3: "on-demand fetch for lookups (~1-2s), session-cached."

    Without this the bar cache was write-only and every lookup re-fetched,
    burning provider quota and making §6.2's sub-2s budget fragile under rate
    limiting.
    """
    engine.lookup("AAPL")
    assert provider.calls == ["AAPL"]
    engine.lookup("AAPL")
    assert provider.calls == ["AAPL"], "second lookup should have hit the cache"


def test_lookup_refetches_once_the_cache_ttl_expires(provider, monkeypatch):
    engine.lookup("AAPL")
    monkeypatch.setattr(engine, "LOOKUP_CACHE_TTL_SECONDS", -1)   # everything is stale
    engine.lookup("AAPL")
    assert provider.calls == ["AAPL", "AAPL"]


def test_use_cache_false_forces_a_live_fetch(provider):
    """The widget's "Live re-score" button — a button labelled live must be live."""
    engine.lookup("AAPL")
    engine.lookup("AAPL", use_cache=False)
    assert provider.calls == ["AAPL", "AAPL"]


def test_lookup_endpoint_fresh_param_bypasses_the_cache(client, provider):
    client.get("/api/signals/lookup/AAPL")
    client.get("/api/signals/lookup/AAPL")
    assert provider.calls == ["AAPL"]
    client.get("/api/signals/lookup/AAPL", params={"fresh": True})
    assert provider.calls == ["AAPL", "AAPL"]


def test_cache_miss_on_a_thin_cache(provider):
    """A cache holding fewer bars than we can score must not be served."""
    store.upsert_bars(_synthetic_bars("THIN", 10))
    engine.lookup("THIN")
    assert "THIN" in provider.calls


def test_corrupt_cache_falls_through_to_a_live_fetch(provider, monkeypatch):
    """The cache is an optimisation; a bad one must never fail the lookup."""
    def boom(*a, **k):
        raise RuntimeError("db corrupt")

    monkeypatch.setattr(store, "load_bars", boom)
    ev = engine.lookup("AAPL")
    assert ev.entry.score >= 0


def test_nightly_run_always_fetches_fresh(provider):
    """run_daily must not serve yesterday's cache — it is the thing that
    refreshes the cache in the first place."""
    store.add_to_watchlist("AAA")
    engine.run_daily()
    engine.run_daily()
    assert provider.calls.count("AAA") == 2


# ── Sync-run guard (Twelve Data rate limits) ─────────────────────────────────


def test_run_endpoint_refuses_a_run_that_would_outlive_the_proxy(client, monkeypatch):
    """Twelve Data's 8 req/min makes a 50-name run ~6 minutes — past nginx's 60s.

    Returning a 504 while the run continues invisibly server-side is the worst
    outcome; refuse and point at what does work.
    """
    import app.services.signals.api as signals_api

    monkeypatch.setattr(signals_api, "OHLCV_PROVIDER", "twelvedata", raising=False)
    monkeypatch.setattr("app.services.signals.config.OHLCV_PROVIDER", "twelvedata")
    for i in range(30):
        store.add_to_watchlist(f"SYM{i}")
    r = client.post("/api/signals/run")
    assert r.status_code == 413
    assert "nightly job" in r.json()["detail"]


def test_run_endpoint_allows_a_scoped_single_symbol_run(client, monkeypatch):
    monkeypatch.setattr("app.services.signals.config.OHLCV_PROVIDER", "twelvedata")
    for i in range(30):
        store.add_to_watchlist(f"SYM{i}")
    assert client.post("/api/signals/run", params={"symbol": "SYM1"}).status_code == 200


def test_run_endpoint_force_overrides_the_refusal(client, monkeypatch):
    monkeypatch.setattr("app.services.signals.config.OHLCV_PROVIDER", "twelvedata")
    for i in range(30):
        store.add_to_watchlist(f"SYM{i}")
    assert client.post("/api/signals/run", params={"force": True}).status_code == 200


def test_run_endpoint_unrestricted_on_yfinance(client):
    """The guard must not fire on the default provider, which has no hard cap."""
    for i in range(30):
        store.add_to_watchlist(f"SYM{i}")
    assert client.post("/api/signals/run").status_code == 200


# ── §1 "daily-close basis": partial intraday bars ────────────────────────────


def _with_partial_last_bar(symbol="AAPL", last_volume=500_000.0):
    """A series whose newest bar is dated today with a fraction of usual volume."""
    series = _synthetic_bars(symbol, 320, volume=20_000_000.0)
    last = series.bars[-1]
    series.bars[-1] = Bar(bar_date=last.bar_date, open=last.open, high=last.high,
                          low=last.low, close=last.close, volume=last_volume)
    return series


def test_partial_intraday_bar_is_flagged(monkeypatch):
    """Verified live: mid-session AAPL returned 2.3% of average volume from
    Twelve Data. RVOL is understated, so §5's volume row under-scores."""
    series = _with_partial_last_bar()
    monkeypatch.setattr(engine, "get_provider", lambda name=None: _Static(series))
    ev = engine.lookup("AAPL", use_cache=False)
    assert any("still forming" in w for w in ev.warnings)


def test_complete_bar_is_not_flagged(monkeypatch):
    """A normal close must not raise the warning, or it becomes noise."""
    series = _with_partial_last_bar(last_volume=20_000_000.0)
    monkeypatch.setattr(engine, "get_provider", lambda name=None: _Static(series))
    ev = engine.lookup("AAPL", use_cache=False)
    assert not any("still forming" in w for w in ev.warnings)


def test_yesterdays_bar_is_never_partial(monkeypatch):
    """A completed prior session is complete however thin its volume."""
    series = _synthetic_bars("AAPL", 320,
                             end=datetime.now(timezone.utc).date() - timedelta(days=1),
                             volume=20_000_000.0)
    last = series.bars[-1]
    series.bars[-1] = Bar(bar_date=last.bar_date, open=last.open, high=last.high,
                          low=last.low, close=last.close, volume=1.0)
    assert engine._looks_partial(series) is False


def test_no_volume_instrument_is_never_flagged_partial():
    """Commodities and FX report no volume — nothing to compare."""
    series = _synthetic_bars("GC=F", 320, volume=None)
    assert engine._looks_partial(series) is False


def test_partial_detection_needs_enough_history():
    series = _synthetic_bars("NEW", 4, volume=1.0)
    assert engine._looks_partial(series) is False


class _Static:
    """Provider that always returns one prepared series."""

    name = "static"

    def __init__(self, series):
        self.series = series
        self.calls = []

    def daily_bars(self, symbol, bars=300):
        self.calls.append(symbol)
        return self.series

    def search(self, q, limit=10):
        return []


def test_watchlist_infers_asset_class_from_the_symbol(client):
    """§9's asset-class filter lies if GC=F and BTC-USD land labelled "equity"."""
    for sym, expected in (("GC=F", "commodity"), ("BTC-USD", "crypto"),
                          ("EURUSD=X", "fx"), ("AAPL", "equity")):
        assert client.post("/api/signals/watchlist", params={"symbol": sym}).json()["entry"]["asset_class"] == expected


def test_explicit_asset_class_still_wins(client):
    r = client.post("/api/signals/watchlist", params={"symbol": "SPY", "asset_class": "etf"})
    assert r.json()["entry"]["asset_class"] == "etf"
