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


class MarketClient:
    def dxy(self) -> MarketQuote:
        ticker = yf.Ticker("DX-Y.NYB")
        for interval, period in (("15m", "5d"), ("60m", "10d"), ("1d", "1mo")):
            history = ticker.history(period=period, interval=interval, auto_adjust=False)
            if len(history.index) >= 2:
                current = float(history["Close"].iloc[-1])
                previous = float(history["Close"].iloc[-2])
                updated_at = history.index[-1].isoformat()
                return MarketQuote(current=current, previous=previous, updated_at=updated_at)
        raise RuntimeError("Unable to fetch DXY price history from yfinance")

    def dxy_monthly_closes(self, limit: int = 3) -> list[MarketBar]:
        history = yf.Ticker("DX-Y.NYB").history(period="12mo", interval="1d", auto_adjust=False)
        if history.empty:
            raise RuntimeError("Unable to fetch DXY monthly history from yfinance")

        monthly: OrderedDict[str, MarketBar] = OrderedDict()
        for timestamp, row in history.iterrows():
            close = row.get("Close")
            if close is None:
                continue
            label = f"{timestamp.year:04d}-{timestamp.month:02d}"
            monthly[label] = MarketBar(label=label, close=float(close), updated_at=timestamp.isoformat())

        values = list(monthly.values())
        if len(values) < limit:
            raise RuntimeError("Not enough DXY monthly history to calculate proxy series")
        return list(reversed(values[-limit:]))
