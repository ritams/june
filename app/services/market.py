from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import yfinance as yf


@dataclass
class MarketQuote:
    current: float
    previous: float
    updated_at: str | None


@dataclass
class MarketBar:
    label: str
    close: float
    updated_at: str | None


_DXY_TICKER = "DX-Y.NYB"


def _download_close(ticker: str, *, period: str | None = None, start: str | None = None, interval: str = "1d"):
    """yf.download is more reliable than yf.Ticker().history() for indices like DX-Y.NYB.

    Returns the Close series as a pandas Series, or empty Series if nothing came back.
    """
    kwargs = {"auto_adjust": False, "progress": False, "interval": interval}
    if period:
        kwargs["period"] = period
    if start:
        kwargs["start"] = start
    data = yf.download(ticker, **kwargs)
    if data is None or data.empty:
        return None
    close = data["Close"]
    # yf.download can return a MultiIndex DataFrame when a single ticker is downloaded.
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    return close.dropna().sort_index()


class MarketClient:
    def dxy(self) -> MarketQuote:
        # yf.Ticker().history(period="...") fails on DX-Y.NYB intermittently.
        # yf.download(period="...") is the reliable path. Try a few intervals.
        for interval, period in (("1d", "10d"), ("1d", "1mo"), ("1d", "3mo")):
            close = _download_close(_DXY_TICKER, period=period, interval=interval)
            if close is not None and len(close) >= 2:
                current = float(close.iloc[-1])
                previous = float(close.iloc[-2])
                updated_at = close.index[-1].isoformat()
                return MarketQuote(current=current, previous=previous, updated_at=updated_at)
        raise RuntimeError("Unable to fetch DXY price history from yfinance")

    def dxy_monthly_closes(self, limit: int = 3) -> list[MarketBar]:
        close = _download_close(_DXY_TICKER, period="1y", interval="1d")
        if close is None or close.empty:
            raise RuntimeError("Unable to fetch DXY monthly history from yfinance")

        monthly: OrderedDict[str, MarketBar] = OrderedDict()
        for timestamp, value in close.items():
            label = f"{timestamp.year:04d}-{timestamp.month:02d}"
            monthly[label] = MarketBar(label=label, close=float(value), updated_at=timestamp.isoformat())

        values = list(monthly.values())
        if len(values) < limit:
            raise RuntimeError("Not enough DXY monthly history to calculate proxy series")
        return list(reversed(values[-limit:]))
