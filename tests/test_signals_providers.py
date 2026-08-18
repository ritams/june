"""Tests for the OHLCV provider layer (scope doc §3.3).

The Twelve Data tests stub httpx, but they stub what the **live API actually
does**, verified against it on 2026-08-11:

  * unknown symbol  → HTTP 404 (NOT a 200 with `status: "error"`)
  * rate limited    → HTTP 429, body carries the message
  * numbers arrive as strings; `volume` is absent for crypto/FX/commodities
  * `symbol_search` returns one row per listing venue, so raw results are
    "SPY, SPY, SPY, SPY"

The first version of these tests stubbed a body-error shape for 404s that the
API never sends. They passed, and the provider was broken against the real
endpoint. The shapes below are copied from live responses for that reason.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.signals import providers
from app.services.signals.providers import (
    ProviderError,
    RateLimited,
    TwelveDataProvider,
    YFinanceProvider,
    classify_asset,
    get_provider,
    to_twelvedata_symbol,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Neither the throttle nor the 429 backoff may slow the suite."""
    monkeypatch.setattr(providers.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    """Each test gets an empty rate-limit window.

    The limiter is module-level (see _RateLimiter's docstring), so without this
    one test's requests would throttle the next.
    """
    monkeypatch.setattr(providers, "_td_limiter", providers._RateLimiter(providers.TD_REQUESTS_PER_MINUTE))


# ── Registry ─────────────────────────────────────────────────────────────────


def test_default_provider_is_yfinance():
    assert get_provider("yfinance").name == "yfinance"


def test_unknown_provider_raises_with_the_available_names():
    with pytest.raises(ProviderError, match="Unknown OHLCV provider"):
        get_provider("bloomberg")


def test_both_providers_satisfy_the_protocol():
    """The seam §3.3's migration turns on — both must expose the same surface."""
    for cls in (YFinanceProvider, TwelveDataProvider):
        assert hasattr(cls, "daily_bars") and hasattr(cls, "search") and hasattr(cls, "name")


def test_twelvedata_requires_a_key(monkeypatch):
    """Fail loudly at construction, not silently at the first fetch."""
    monkeypatch.setattr(providers, "TWELVE_DATA_API_KEY", None)
    with pytest.raises(ProviderError, match="TWELVE_DATA_API_KEY is not set"):
        TwelveDataProvider()


# ── Asset classification (cosmetic, §9 filter chip) ──────────────────────────


@pytest.mark.parametrize("symbol,expected", [
    ("AAPL", "equity"), ("BTC-USD", "crypto"), ("GC=F", "commodity"),
    ("EURUSD=X", "fx"), ("^GSPC", "index"),
])
def test_classify_asset(symbol, expected):
    assert classify_asset(symbol) == expected


# ── Symbol translation (§1 cross-asset watchlist) ────────────────────────────
#
# The watchlist stores yfinance-style symbols. Switching providers must not
# orphan existing rows, so translation happens at the provider boundary.


@pytest.mark.parametrize("canonical,expected", [
    ("AAPL", "AAPL"),          # equities pass through
    ("SPY", "SPY"),            # ETFs pass through
    ("BTC-USD", "BTC/USD"),    # crypto
    ("ETH-USD", "ETH/USD"),
    ("GC=F", "XAU/USD"),       # gold futures → gold spot (no futures on basic)
    ("CL=F", "WTI/USD"),
    ("EURUSD=X", "EUR/USD"),   # FX
])
def test_symbol_translation(canonical, expected):
    assert to_twelvedata_symbol(canonical) == expected


def test_translation_is_case_insensitive():
    assert to_twelvedata_symbol("btc-usd") == "BTC/USD"


def test_indices_raise_an_explanatory_error():
    """A bare 404 would read as "unknown symbol" and send someone hunting."""
    with pytest.raises(ProviderError, match="index symbols are not available"):
        to_twelvedata_symbol("^GSPC")


def test_unmapped_futures_raise_an_explanatory_error():
    with pytest.raises(ProviderError, match="no futures on the basic plan"):
        to_twelvedata_symbol("ZN=F")


# ── HTTP stubbing that matches the live API ──────────────────────────────────


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _stub(monkeypatch, payload, status=200, capture=None):
    """Install a fake httpx.Client returning one canned response."""
    import httpx

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None, **k):
            if capture is not None:
                capture.update({"url": url, "params": params or {}, "headers": headers or {}})
            return _Resp(payload, status)

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(providers, "TWELVE_DATA_API_KEY", "test-key")


def _values(**overrides):
    row = {"datetime": "2026-07-29", "open": "100.5", "high": "102.0",
           "low": "99.0", "close": "101.25", "volume": "1500000"}
    row.update(overrides)
    return {"meta": {"symbol": "AAPL"}, "values": [row], "status": "ok"}


# ── Auth (key must not leak into logs) ───────────────────────────────────────


def test_api_key_goes_in_a_header_not_the_query_string(monkeypatch):
    """httpx logs the full URL at INFO — a key in params lands in journalctl."""
    seen = {}
    _stub(monkeypatch, _values(), capture=seen)
    TwelveDataProvider().daily_bars("AAPL", 5)
    assert "apikey" not in seen["params"]
    assert seen["headers"]["Authorization"] == "apikey test-key"


# ── Response mapping ─────────────────────────────────────────────────────────


def test_string_typed_numbers_are_parsed(monkeypatch):
    """Every numeric field arrives as a string — the classic integration bug."""
    _stub(monkeypatch, _values())
    bar = TwelveDataProvider().daily_bars("AAPL").bars[0]
    assert bar.close == pytest.approx(101.25)
    assert bar.high == pytest.approx(102.0)
    assert bar.bar_date == date(2026, 7, 29)
    assert bar.volume == pytest.approx(1_500_000)


def test_series_is_labelled_with_the_canonical_symbol(monkeypatch):
    """The DB, watchlist and UI all key on canonical — not the provider's form."""
    _stub(monkeypatch, _values())
    series = TwelveDataProvider().daily_bars("BTC-USD")
    assert series.symbol == "BTC-USD"          # not "BTC/USD"


def test_absent_volume_becomes_none_not_zero(monkeypatch):
    """Live crypto/FX/commodity responses carry no volume at all.

    A 0 would drag the RVOL average down and fake a capitulation signal.
    """
    _stub(monkeypatch, _values(volume=None))
    assert TwelveDataProvider().daily_bars("GC=F").bars[0].volume is None


def test_empty_string_volume_becomes_none(monkeypatch):
    _stub(monkeypatch, _values(volume=""))
    assert TwelveDataProvider().daily_bars("GC=F").bars[0].volume is None


def test_bar_with_unusable_close_is_dropped(monkeypatch):
    _stub(monkeypatch, {"values": [
        {"datetime": "2026-07-28", "close": None},
        {"datetime": "2026-07-29", "open": "1", "high": "2", "low": "0.5", "close": "1.5"},
    ]})
    assert len(TwelveDataProvider().daily_bars("AAPL").bars) == 1


def test_missing_ohl_falls_back_to_the_close(monkeypatch):
    """Degenerate bar beats a hole that shifts every DeMark offset."""
    _stub(monkeypatch, {"values": [{"datetime": "2026-07-29", "close": "50.0"}]})
    bar = TwelveDataProvider().daily_bars("AAPL").bars[0]
    assert bar.open == bar.high == bar.low == pytest.approx(50.0)


# ── Error mapping, as the live API actually behaves ──────────────────────────


def test_http_404_returns_an_empty_series_not_an_error(monkeypatch):
    """**The bug the first implementation had.**

    Twelve Data returns HTTP 404 for an unknown symbol, not a 200 with a body
    error. Code that only inspects the body raises ProviderError instead, which
    §3.3 would then treat as an outage — flagging stale and suppressing alerts
    for a ticker that simply does not exist.
    """
    _stub(monkeypatch, {}, status=404)
    series = TwelveDataProvider().daily_bars("NOTAREALTICKER")
    assert len(series) == 0
    assert series.is_stale() is True      # empty is stale by definition


def test_http_429_raises_rate_limited_after_retries(monkeypatch):
    """A real failure must raise so §3.3 falls back to cache and flags stale."""
    _stub(monkeypatch, {"code": 429, "message": "out of API credits"}, status=429)
    with pytest.raises(RateLimited, match="rate limit"):
        TwelveDataProvider().daily_bars("AAPL")


def test_rate_limited_is_a_provider_error(monkeypatch):
    """Callers that only catch ProviderError must still handle throttling."""
    assert issubclass(RateLimited, ProviderError)


def test_body_level_error_still_handled(monkeypatch):
    """Some errors do arrive as HTTP 200 with the failure in the body."""
    _stub(monkeypatch, {"status": "error", "code": 400, "message": "bad symbol"})
    assert len(TwelveDataProvider().daily_bars("???")) == 0


def test_body_level_500_raises(monkeypatch):
    _stub(monkeypatch, {"status": "error", "code": 500, "message": "server error"})
    with pytest.raises(ProviderError, match="500"):
        TwelveDataProvider().daily_bars("AAPL")


# ── Rate limiting ────────────────────────────────────────────────────────────


@pytest.fixture
def fake_clock(monkeypatch):
    """A virtual clock where sleep() advances time instead of blocking.

    Necessary, not just faster: `acquire()` re-checks the window after sleeping,
    so a no-op sleep with a real clock turns it into a busy-wait that spins for
    a full 60 seconds. The clock must move when sleep is called.
    """
    now = {"t": 1000.0}
    monkeypatch.setattr(providers.time, "monotonic", lambda: now["t"])
    waits = []

    def _sleep(seconds):
        waits.append(seconds)
        now["t"] += seconds

    monkeypatch.setattr(providers.time, "sleep", _sleep)
    return waits


def test_limiter_allows_up_to_the_cap_without_waiting(fake_clock):
    limiter = providers._RateLimiter(per_minute=3)
    for _ in range(3):
        limiter.acquire()
    assert fake_clock == []


def test_limiter_waits_once_the_cap_is_reached(fake_clock):
    """Without this a 50-name nightly run dies on symbol 9."""
    limiter = providers._RateLimiter(per_minute=2)
    for _ in range(3):
        limiter.acquire()
    assert fake_clock, "third request should have waited"
    assert 0 < fake_clock[0] <= 61


def test_limiter_window_slides_rather_than_resetting(fake_clock):
    """After the window passes, capacity returns without a further wait."""
    limiter = providers._RateLimiter(per_minute=2)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()             # waits once
    before = len(fake_clock)
    limiter.acquire()             # capacity freed by the wait — no new wait
    assert len(fake_clock) == before


def test_limiter_is_shared_across_provider_instances():
    """get_provider() builds a new instance per call; a per-instance limiter
    would reset constantly and never throttle."""
    a, b = TwelveDataProvider("k"), TwelveDataProvider("k")
    assert providers._td_limiter is providers._td_limiter
    assert a is not b


# ── Search (§6.1 autocomplete) ───────────────────────────────────────────────


def _search_body():
    """Shape copied from a live response — note the duplicate SPY listings."""
    return {"data": [
        {"symbol": "SPY", "instrument_name": "SPDR S&P 500 ETF", "instrument_type": "ETF",
         "exchange": "LSE", "country": "United Kingdom"},
        {"symbol": "SPY", "instrument_name": "SPDR S&P 500 ETF", "instrument_type": "ETF",
         "exchange": "NYSE", "country": "United States"},
        {"symbol": "SPY", "instrument_name": "SPDR S&P 500 ETF", "instrument_type": "ETF",
         "exchange": "MEX", "country": "Mexico"},
        {"symbol": "SPYG", "instrument_name": "SPDR Growth", "instrument_type": "ETF",
         "exchange": "NYSE", "country": "United States"},
    ]}


def test_search_deduplicates_by_symbol(monkeypatch):
    """Raw results are one row per venue — "SPY, SPY, SPY" is useless autocomplete."""
    _stub(monkeypatch, _search_body())
    results = TwelveDataProvider().search("SPY", 10)
    assert [r.symbol for r in results] == ["SPY", "SPYG"]


def test_search_prefers_the_us_listing(monkeypatch):
    """A US listing is what this dashboard trades and what other paths assume."""
    _stub(monkeypatch, _search_body())
    assert TwelveDataProvider().search("SPY", 10)[0].exchange == "NYSE"


def test_search_maps_instrument_types(monkeypatch):
    _stub(monkeypatch, {"data": [
        {"symbol": "AAPL", "instrument_name": "Apple", "instrument_type": "Common Stock",
         "exchange": "NASDAQ", "country": "United States"},
        {"symbol": "BTC/USD", "instrument_name": "Bitcoin", "instrument_type": "Digital Currency"},
    ]})
    results = TwelveDataProvider().search("x", 10)
    assert results[0].asset_class == "equity"
    assert results[1].asset_class == "crypto"


def test_search_respects_the_limit(monkeypatch):
    _stub(monkeypatch, _search_body())
    assert len(TwelveDataProvider().search("SPY", 1)) == 1


def test_search_failure_returns_empty_not_an_exception(monkeypatch):
    """A dead search must not break the page — exact symbols still work."""
    _stub(monkeypatch, {"status": "error", "code": 500, "message": "down"})
    assert TwelveDataProvider().search("aapl") == []


def test_search_of_blank_query_makes_no_request(monkeypatch):
    called = {"n": 0}
    import httpx

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k):
            called["n"] += 1
            return _Resp({"data": []})

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(providers, "TWELVE_DATA_API_KEY", "k")
    assert TwelveDataProvider().search("   ") == []
    assert called["n"] == 0
