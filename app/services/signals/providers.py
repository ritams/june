"""Technical Signals Layer — price data providers.

Implements `docs/technical-signals-layer-scope.md` §3.3, "Price data — the
single external dependency".

The scope doc specifies Twelve Data (or Polygon + a crypto source). We ship
yfinance first because it is already a dependency of this repo
(app/services/market.py) and needs no API key or paid signup, so the engine
could be built and verified on day one. Everything downstream of this module
consumes the `OHLCVProvider` protocol, never a concrete provider — so adding
Twelve Data later is a new class here plus a config flag, with no change to the
indicator, scoring, or API layers.

What genuinely improves when Twelve Data lands (tracked at config.OHLCV_PROVIDER):

  * **§6.1 autocomplete.** Twelve Data has a reference/symbol-search endpoint.
    yfinance has no supported equivalent, so `search()` below validates a typed
    symbol rather than offering true prefix autocomplete. The §6 search bar
    works either way; it just can't suggest until then.
  * **A meaningful staleness contract.** §3.3's "if the data API is down, flag
    stale" assumes an API with an SLA and an error surface. yfinance scrapes an
    undocumented endpoint and can return an empty frame for a valid symbol
    under rate limiting — indistinguishable, from here, from a delisted ticker.
    We treat empty-and-unexplained as a fetch failure rather than as "no data",
    which is the conservative reading.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol, runtime_checkable

from app.services.signals.bars import Bar, BarSeries
from app.services.signals.config import (
    HISTORY_BARS,
    OHLCV_PROVIDER,
    SEARCH_PROVIDER,
    TWELVE_DATA_API_KEY,
    TWELVE_DATA_RPM,
)


logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Raised when a provider cannot return usable bars for a symbol.

    Deliberately distinct from "the symbol has no data": callers must be able
    to tell a transient API failure (→ §3.3 flag stale, skip alerts, keep the
    last good score) from an unknown ticker (→ tell the user the symbol is
    invalid). Every provider raises this for the former and returns an empty
    series for the latter.
    """


@dataclass(frozen=True)
class SymbolRef:
    """A symbol as returned by provider search — feeds §6.1 autocomplete."""

    symbol: str
    name: str
    asset_class: str          # 'equity' | 'etf' | 'commodity' | 'crypto' (§1)
    exchange: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "asset_class": self.asset_class,
            "exchange": self.exchange,
        }


@runtime_checkable
class OHLCVProvider(Protocol):
    """The contract every price-data source must satisfy.

    Kept deliberately small — three methods — so a second implementation is
    cheap. This is the seam the §3.3 Twelve Data migration turns on.
    """

    name: str

    def daily_bars(self, symbol: str, bars: int = HISTORY_BARS) -> BarSeries:
        """Most recent `bars` daily OHLCV bars, oldest→newest.

        Raises ProviderError on fetch failure. Returns an empty BarSeries if
        the symbol is valid but has no data.
        """
        ...

    def search(self, query: str, limit: int = 10) -> list[SymbolRef]:
        """Symbol lookup for the §6.1 search bar."""
        ...


# ── yfinance ─────────────────────────────────────────────────────────────────

# yfinance wants a period string, not a bar count. These are generous — we slice
# to exactly HISTORY_BARS after fetching. "2y" covers 300 trading days (~14
# months) with room for holidays; the fallback ladder handles instruments with
# shorter listings.
_PERIOD_LADDER = ("2y", "5y", "max")

# Suffix/pattern heuristics for asset classification. yfinance has no asset-class
# field, so we infer. This only drives the §9 asset-class filter chip and the
# §1 grouping — nothing in the scoring path depends on it, so a misclassification
# is cosmetic rather than a correctness bug.
_CRYPTO_SUFFIXES = ("-USD", "-USDT", "-EUR", "-GBP")
_FUTURES_SUFFIX = "=F"
_FX_SUFFIX = "=X"


def classify_asset(symbol: str) -> str:
    """Best-effort asset class from the symbol's shape. Cosmetic only — see above."""
    s = symbol.upper()
    if any(s.endswith(suf) for suf in _CRYPTO_SUFFIXES):
        return "crypto"
    if s.endswith(_FUTURES_SUFFIX):
        return "commodity"
    if s.endswith(_FX_SUFFIX):
        return "fx"
    if s.startswith("^"):
        return "index"
    return "equity"


class YFinanceProvider:
    """yfinance-backed provider — the v1 default.

    Reuses the download approach already proven in app/services/market.py:
    `yf.download` is more reliable than `yf.Ticker().history()` for the index
    and FX symbols this dashboard already tracks, and it handles the
    MultiIndex-for-single-ticker quirk the same way.
    """

    name = "yfinance"

    def daily_bars(self, symbol: str, bars: int = HISTORY_BARS) -> BarSeries:
        import yfinance as yf

        frame = None
        for period in _PERIOD_LADDER:
            try:
                frame = yf.download(
                    symbol,
                    period=period,
                    interval="1d",
                    auto_adjust=False,
                    progress=False,
                    # Suppress yfinance's own retry/print noise; we handle errors here.
                    threads=False,
                )
            except Exception as exc:
                # Network/parse errors are transient far more often than not.
                # Try the next period before giving up — a different range
                # sometimes succeeds when one is rate-limited.
                logger.warning("yfinance download failed for %s (period=%s): %s", symbol, period, exc)
                continue
            if frame is not None and not frame.empty and len(frame) >= min(bars, 50):
                break

        if frame is None or frame.empty:
            # Ambiguous by construction — see the module docstring. Treated as a
            # fetch failure (the conservative reading) so §3.3 flags stale and
            # skips alerts rather than silently scoring nothing.
            raise ProviderError(
                f"yfinance returned no data for {symbol!r}. "
                "Either the symbol is unknown or the endpoint is rate-limiting."
            )

        return BarSeries(
            symbol=symbol,
            timeframe="daily",
            bars=_frame_to_bars(frame)[-bars:],
            fetched_at=datetime.now(timezone.utc),
        )

    def search(self, query: str, limit: int = 10) -> list[SymbolRef]:
        """Validate a typed symbol — yfinance has no supported search endpoint.

        This is the §6.1 gap called out in the module docstring. Rather than
        scrape Yahoo's unofficial autocomplete JSON (which would be exactly the
        kind of fragile dependency §3.1 and §3.2 exist to reject), we confirm
        the symbol resolves and return it as a single result. The §6 search bar
        therefore behaves as "type an exact symbol, press enter" until Twelve
        Data lands.
        """
        import yfinance as yf

        symbol = query.strip().upper()
        if not symbol:
            return []
        try:
            info = yf.Ticker(symbol).fast_info
            # fast_info raises or returns empty for unknown symbols; last_price
            # is the cheapest field that proves the symbol resolved.
            price = info.get("lastPrice") if hasattr(info, "get") else getattr(info, "last_price", None)
            if price is None:
                return []
        except Exception:
            return []
        return [SymbolRef(symbol=symbol, name=symbol, asset_class=classify_asset(symbol))][:limit]


def _frame_to_bars(frame: Any) -> list[Bar]:
    """pandas DataFrame → list[Bar], dropping rows with an unusable close.

    Handles the MultiIndex-for-single-ticker shape yfinance returns (the same
    quirk app/services/market.py works around), and treats NaN volume as None
    rather than 0.0 — see the Bar.volume docstring for why that distinction
    matters to RVOL.
    """
    import pandas as pd

    # Single-ticker downloads can come back with a MultiIndex on the columns
    # (('Close', 'AAPL')). Flatten to the field name.
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.droplevel(axis=1, level=1)

    bars: list[Bar] = []
    for timestamp, row in frame.iterrows():
        close = row.get("Close")
        if close is None or pd.isna(close):
            # A NaN close is unusable for every indicator in §4. Drop the bar
            # rather than forward-fill: a synthetic close would produce a real
            # -looking TD count on a day that never traded.
            continue
        volume = row.get("Volume")
        bars.append(
            Bar(
                bar_date=_as_date(timestamp),
                open=_f(row.get("Open"), close),
                high=_f(row.get("High"), close),
                low=_f(row.get("Low"), close),
                close=float(close),
                volume=None if volume is None or pd.isna(volume) else float(volume),
            )
        )
    return bars


def _f(value: Any, fallback: float) -> float:
    """Float coercion with a fallback for missing OHL fields.

    Falling back to the close is safe and standard: a bar with a known close but
    a missing high/low is degenerate (a doji at the close), which is a far
    better approximation than dropping an otherwise-valid bar and creating a
    hole in the series that would shift every DeMark bar-offset comparison.
    """
    import pandas as pd

    if value is None or pd.isna(value):
        return float(fallback)
    return float(value)


def _as_date(timestamp: Any) -> date:
    """pandas Timestamp / datetime / str → date."""
    if isinstance(timestamp, datetime):
        return timestamp.date()
    if isinstance(timestamp, date):
        return timestamp
    if hasattr(timestamp, "to_pydatetime"):
        return timestamp.to_pydatetime().date()
    return datetime.fromisoformat(str(timestamp)[:10]).date()


# ── Twelve Data ──────────────────────────────────────────────────────────────


class RateLimited(ProviderError):
    """Provider refused the request because we exceeded its rate limit.

    A distinct type because the correct response differs from other failures:
    wait and retry, rather than fall back to cached bars and flag stale.
    """


# ── Twelve Data ──────────────────────────────────────────────────────────────
#
# Everything below was written against the **live API**, not its documentation.
# Four things the docs did not make obvious, each of which broke a first
# implementation that only unit tests had exercised:
#
#   1. Unknown symbols return **HTTP 404**, not a 200 with `status: "error"`.
#      An implementation that only inspects the body never sees them.
#   2. Rate limiting returns **HTTP 429** with the error in the body. The free
#      ("basic") plan allows **8 requests/minute, 800/day** — a 50-name nightly
#      run dies on symbol 9 without throttling.
#   3. Symbol format differs from yfinance for everything except equities and
#      ETFs: `BTC/USD` not `BTC-USD`, `XAU/USD` not `GC=F`, `EUR/USD` not
#      `EURUSD=X`. Indices are not on the basic plan at all.
#   4. `symbol_search` returns the same ticker once per listing venue, so a raw
#      response is "SPY, SPY, SPY, SPY" — useless as autocomplete.


# §3.3 canonical symbols are stored yfinance-style, because that is what the
# watchlist already holds. Switching providers must not orphan existing rows, so
# translation happens at the provider boundary and nothing upstream changes.
#
# Crypto and FX follow a rule; commodities need a table because Twelve Data has
# no futures on the basic plan and the nearest equivalent is the spot pair.
_TD_SYMBOL_MAP = {
    "GC=F": "XAU/USD",     # gold futures → gold spot
    "SI=F": "XAG/USD",     # silver
    "CL=F": "WTI/USD",     # WTI crude
    "BZ=F": "BRENT/USD",   # Brent
    "PL=F": "XPT/USD",     # platinum
    "PA=F": "XPD/USD",     # palladium
}

# Indices are not available on the basic plan (^GSPC, SPX both 404). Named
# explicitly so the error says *why* rather than "unknown symbol".
_TD_UNSUPPORTED_PREFIXES = ("^",)


def to_twelvedata_symbol(canonical: str) -> str:
    """Canonical (yfinance-style) symbol → Twelve Data's format.

    Equities and ETFs pass through unchanged; everything else is translated.
    Raises ProviderError for instrument types the basic plan does not carry, so
    the failure is explicit rather than a confusing 404.
    """
    s = canonical.strip().upper()

    if s in _TD_SYMBOL_MAP:
        return _TD_SYMBOL_MAP[s]

    if any(s.startswith(p) for p in _TD_UNSUPPORTED_PREFIXES):
        raise ProviderError(
            f"{canonical!r}: index symbols are not available on the Twelve Data basic plan. "
            "Use the yfinance provider for indices, or upgrade the plan."
        )

    # Crypto: BTC-USD → BTC/USD
    for suffix in _CRYPTO_SUFFIXES:
        if s.endswith(suffix):
            return f"{s[: -len(suffix)]}/{suffix.lstrip('-')}"

    # FX: EURUSD=X → EUR/USD
    if s.endswith(_FX_SUFFIX):
        base = s[: -len(_FX_SUFFIX)]
        if len(base) == 6:
            return f"{base[:3]}/{base[3:]}"
        return base

    # An untranslated futures symbol would 404 with no explanation.
    if s.endswith(_FUTURES_SUFFIX):
        raise ProviderError(
            f"{canonical!r}: Twelve Data has no futures on the basic plan and no spot equivalent "
            f"is mapped. Add one to _TD_SYMBOL_MAP, or keep this name on the yfinance provider."
        )

    return s


# Twelve Data's basic plan: 8 requests/minute, 800/day (confirmed live via
# /api_usage). Configurable because a paid plan raises it substantially.
TD_REQUESTS_PER_MINUTE = TWELVE_DATA_RPM


class _RateLimiter:
    """Sliding-window throttle shared across all TwelveDataProvider instances.

    Module-level state rather than per-instance, because `get_provider()`
    constructs a fresh provider on every call — a per-instance limiter would
    reset constantly and never actually throttle.

    Blocks rather than raising: the nightly run (§10) is a background job where
    waiting 40 seconds is free, and the alternative — failing 42 of 50 symbols —
    is far worse. The §6 lookup path is protected from that wait by the §3.3
    session cache and by the fact that interactive use is one symbol at a time.
    """

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                # Drop timestamps older than the window.
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()
                if len(self._times) < self.per_minute:
                    self._times.append(now)
                    return
                # Sleep until the oldest request ages out. +0.5s of slack because
                # the server's window and ours are not perfectly aligned, and
                # being one request early costs a 429 and a full retry.
                wait = 60.0 - (now - self._times[0]) + 0.5
            logger.info("Twelve Data rate limit reached; waiting %.1fs", wait)
            time.sleep(wait)


_td_limiter = _RateLimiter(TD_REQUESTS_PER_MINUTE)


class TwelveDataProvider:
    """Twelve Data — the provider §3.3 actually specifies.

    Implements the same `OHLCVProvider` protocol as YFinanceProvider, so
    switching is `SIGNALS_OHLCV_PROVIDER=twelvedata` plus a key. Nothing in the
    indicator, scoring, alerting or API layers changes.

    What it buys over yfinance:

      * **§6.1 autocomplete.** `symbol_search` is a real prefix search.
      * **An honest staleness contract.** Unknown symbol (404) is distinguishable
        from rate limiting (429) and from an outage, which is exactly what
        §3.3's stale-vs-invalid distinction needs and which yfinance cannot do.

    What it costs, on the basic plan:

      * 8 requests/minute — a full watchlist run is throttled and takes minutes.
      * No indices, no futures. See `to_twelvedata_symbol`.
    """

    name = "twelvedata"

    BASE_URL = "https://api.twelvedata.com"
    INTERVAL = "1day"

    # Retries for a 429 that slips past the throttle (clock skew, or another
    # process sharing the key). Beyond this we surface the failure so §3.3 can
    # fall back to cached bars and flag stale.
    MAX_RETRIES = 2

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key or TWELVE_DATA_API_KEY or "").strip()
        if not self.api_key:
            raise ProviderError(
                "TWELVE_DATA_API_KEY is not set — cannot use the twelvedata provider. "
                "Set the key, or leave SIGNALS_OHLCV_PROVIDER=yfinance."
            )

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """One throttled, retried API call. Returns the parsed body.

        Maps transport reality onto our contract:
          404 → `{}` sentinel, meaning "unknown symbol" (caller returns empty)
          429 → RateLimited after retries are exhausted
          other → ProviderError
        """
        import httpx

        # The key goes in a header, NOT a query parameter. Twelve Data accepts
        # both, but httpx logs the full request URL at INFO — passing the key as
        # a param writes it into journalctl on every call, where it persists in
        # log rotation and anywhere logs are shipped. A header keeps it out.
        headers = {"Authorization": f"apikey {self.api_key}"}
        last_error: str | None = None

        for attempt in range(self.MAX_RETRIES + 1):
            _td_limiter.acquire()
            try:
                with httpx.Client(timeout=30.0) as client:
                    response = client.get(f"{self.BASE_URL}/{path}", params=params, headers=headers)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Twelve Data transport error on %s: %s", path, last_error)
                continue

            if response.status_code == 404:
                return {}

            if response.status_code == 429:
                # The throttle should prevent this; if we still got one, the
                # window is out of sync — wait a full window and retry.
                last_error = "rate limited (HTTP 429)"
                if attempt < self.MAX_RETRIES:
                    logger.warning("Twelve Data 429 despite throttle; backing off 60s")
                    time.sleep(60.0)
                    continue
                raise RateLimited(
                    f"Twelve Data rate limit exceeded on {path}. Basic plan allows "
                    f"{TD_REQUESTS_PER_MINUTE} requests/minute; set TWELVE_DATA_RPM if the plan differs."
                )

            try:
                response.raise_for_status()
                body = response.json()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Twelve Data bad response on %s: %s", path, last_error)
                continue

            # Some errors arrive as HTTP 200 with the failure in the body.
            if isinstance(body, dict) and body.get("status") == "error":
                code = body.get("code")
                message = body.get("message", "unknown error")
                if code in (400, 404):
                    return {}
                if code == 429:
                    raise RateLimited(f"Twelve Data rate limit: {message}")
                raise ProviderError(f"Twelve Data error {code} on {path}: {message}")

            return body

        raise ProviderError(f"Twelve Data request to {path} failed after retries: {last_error}")

    # ── OHLCVProvider ────────────────────────────────────────────────────────

    def daily_bars(self, symbol: str, bars: int = HISTORY_BARS) -> BarSeries:
        provider_symbol = to_twelvedata_symbol(symbol)
        body = self._request("time_series", {
            "symbol": provider_symbol,
            "interval": self.INTERVAL,
            "outputsize": max(1, min(bars, 5000)),
            "order": "ASC",
        })

        if not body:
            # Genuinely unknown symbol — an empty series, not a failure. This is
            # the distinction yfinance cannot make.
            logger.info("Twelve Data: no data for %s (as %s)", symbol, provider_symbol)
            return BarSeries(symbol=symbol, timeframe="daily", bars=[],
                             fetched_at=datetime.now(timezone.utc))

        # The series is labelled with the CANONICAL symbol, not the provider's —
        # everything downstream (the DB, the watchlist, the UI) keys on canonical.
        return BarSeries(
            symbol=symbol.strip().upper(),
            timeframe="daily",
            bars=self._parse_values(body.get("values") or [])[-bars:],
            fetched_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _parse_values(values: list[dict[str, Any]]) -> list[Bar]:
        """Twelve Data `values` → Bar list.

        Every numeric field arrives as a **string**, and `volume` is absent for
        instruments that have none — mapped to None, never 0.0, per the Bar
        contract (a 0 would drag an RVOL average down and manufacture a
        capitulation signal).
        """
        out: list[Bar] = []
        for row in values:
            try:
                close = float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue      # a bar with no usable close is unusable for every §4 indicator
            volume_raw = row.get("volume")
            try:
                volume = float(volume_raw) if volume_raw not in (None, "") else None
            except (TypeError, ValueError):
                volume = None
            out.append(
                Bar(
                    bar_date=_as_date(row["datetime"]),
                    open=_str_float(row.get("open"), close),
                    high=_str_float(row.get("high"), close),
                    low=_str_float(row.get("low"), close),
                    close=close,
                    volume=volume,
                )
            )
        return out

    def search(self, query: str, limit: int = 10) -> list[SymbolRef]:
        """Real prefix search — this is what closes the §6.1 autocomplete gap.

        Deduplicated by symbol: the raw API returns one row per listing venue, so
        searching "SPY" yields four identical-looking entries. We keep the first
        occurrence, having sorted US venues first, because a US listing is what
        this dashboard trades and what the other data paths assume.
        """
        q = query.strip()
        if not q:
            return []
        try:
            body = self._request("symbol_search", {"symbol": q, "outputsize": max(limit * 4, 20)})
        except ProviderError as exc:
            # Search failing must not break the page — the user can still type an
            # exact symbol and press enter.
            logger.warning("Twelve Data search failed for %r: %s", q, exc)
            return []

        rows = (body or {}).get("data") or []
        # US venues first, then original order — `sorted` is stable.
        rows = sorted(rows, key=lambda r: 0 if str(r.get("country", "")).lower() in ("united states", "us") else 1)

        seen: set[str] = set()
        out: list[SymbolRef] = []
        for row in rows:
            sym = str(row.get("symbol", "")).upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            out.append(
                SymbolRef(
                    symbol=sym,
                    name=str(row.get("instrument_name") or sym),
                    asset_class=_map_instrument_type(row.get("instrument_type")),
                    exchange=row.get("exchange"),
                )
            )
            if len(out) >= limit:
                break
        return out


def _str_float(value: Any, fallback: float) -> float:
    """Twelve Data sends numbers as strings; fall back to the close if absent.

    Same rationale as the yfinance path: a bar with a known close but a missing
    high/low is degenerate rather than invalid, and dropping it would create a
    hole that shifts every DeMark bar-offset comparison.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


# Twelve Data's instrument_type → our §1 asset classes.
_INSTRUMENT_TYPE_MAP = {
    "common stock": "equity",
    "etf": "etf",
    "digital currency": "crypto",
    "physical currency": "fx",
    "index": "index",
}


def _map_instrument_type(raw: Any) -> str:
    return _INSTRUMENT_TYPE_MAP.get(str(raw or "").strip().lower(), "equity")


# ── Registry ─────────────────────────────────────────────────────────────────

_PROVIDERS: dict[str, type] = {
    "yfinance": YFinanceProvider,
    "twelvedata": TwelveDataProvider,
}


def get_search_provider() -> OHLCVProvider:
    """Provider used for §6.1 symbol search — may differ from the bars provider.

    See SEARCH_PROVIDER in config.py: the default hybrid uses Twelve Data for
    search (the only one with a search endpoint) and yfinance for bars (no rate
    limit, better intraday volume). Falls back to the bars provider if the search
    provider cannot be constructed — a missing key must degrade search, not break
    the page.
    """
    try:
        return get_provider(SEARCH_PROVIDER)
    except ProviderError as exc:
        logger.warning("Search provider %r unavailable (%s); falling back to %s",
                       SEARCH_PROVIDER, exc, OHLCV_PROVIDER)
        return get_provider()


def get_provider(name: str | None = None) -> OHLCVProvider:
    """Resolve the configured provider. Defaults to config.OHLCV_PROVIDER."""
    key = (name or OHLCV_PROVIDER).strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise ProviderError(
            f"Unknown OHLCV provider {key!r}. Available: {', '.join(sorted(_PROVIDERS))}"
        )
    return cls()
