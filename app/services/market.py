from __future__ import annotations

from dataclasses import dataclass

import yfinance as yf


@dataclass
class MarketQuote:
    current: float
    previous: float
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
