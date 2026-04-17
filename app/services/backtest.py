from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd
import yfinance as yf

from app.config import Settings
from app.services.fred import FredClient
from app.services.ism import ISMClient


HORIZONS = {
    180: "6m",
    365: "1yr",
    540: "18m",
    730: "2yr",
}

ASSET_SPECS = {
    "SPY": {
        "ticker": "SPY",
        "label": "S&P",
        "start": "2000-01-01",
        "since": "2000",
        "confidence": {"tone": "positive", "label": "HIGH", "description": "20+ complete cycles"},
    },
    "QQQ": {
        "ticker": "QQQ",
        "label": "Nasdaq",
        "start": "2000-01-01",
        "since": "2000",
        "confidence": {"tone": "positive", "label": "HIGH", "description": "20+ complete cycles"},
    },
    "IAU": {
        "ticker": "IAU",
        "label": "Gold",
        "start": "2005-01-01",
        "since": "2005",
        "confidence": {"tone": "neutral", "label": "MEDIUM", "description": "Shorter ETF history"},
    },
    "BTC": {
        "ticker": "BTC-USD",
        "label": "BTC",
        "start": "2017-01-01",
        "since": "2017",
        "confidence": {"tone": "negative", "label": "LOW", "description": "Direction only, never sizing"},
    },
    "DXY": {
        "ticker": "DX-Y.NYB",
        "label": "DXY",
        "start": "2000-01-01",
        "since": "2000",
    },
}

SIGNAL_SPECS = {
    "risk_on": {
        "label": "RISK ON",
        "subtitle": "Liquidity = EXPANDING and Cycle = EXPANSION",
        "assets": ["SPY", "QQQ", "IAU", "BTC"],
        "horizons": [180, 365, 540, 730],
        "page": "liquidity",
        "direction": "bullish",
    },
    "risk_off": {
        "label": "RISK OFF",
        "subtitle": "Liquidity = CONTRACTING or Cycle = CONTRACTION",
        "assets": ["SPY", "QQQ", "IAU", "BTC"],
        "horizons": [180, 365, 540, 730],
        "page": "business-cycle",
        "direction": "defensive",
    },
    "yield_curve_uninversion": {
        "label": "Yield Curve Uninversion",
        "subtitle": "T10Y2Y crosses from negative to positive",
        "assets": ["SPY", "QQQ", "BTC"],
        "horizons": [180, 365, 540, 730],
        "page": "business-cycle",
        "direction": "bullish",
    },
    "m2_acceleration": {
        "label": "M2 Acceleration",
        "subtitle": "US M2 MoM > 0.5% for 2 consecutive months",
        "assets": ["BTC", "QQQ", "IAU"],
        "horizons": [180, 365, 540, 730],
        "page": "liquidity",
        "direction": "bullish",
    },
    "dollar_weakness": {
        "label": "Dollar Weakness",
        "subtitle": "DXY below 100 and 3-month trend negative",
        "assets": ["BTC", "IAU", "QQQ"],
        "horizons": [180, 365, 540, 730],
        "page": "liquidity",
        "direction": "bullish",
    },
    "credit_stress": {
        "label": "Credit Stress",
        "subtitle": "High-yield spreads cross above 500 bps",
        "assets": ["SPY", "QQQ"],
        "horizons": [180, 365, 540, 730],
        "page": "business-cycle",
        "direction": "defensive",
    },
    "macro_summer_entry": {
        "label": "Macro Summer Entry",
        "subtitle": "Yield curve positive, ISM rising, credit spreads contracting",
        "assets": ["QQQ", "BTC"],
        "horizons": [365, 540, 730],
        "page": "business-cycle",
        "direction": "bullish",
    },
}

PAGE_SIGNALS = {
    "liquidity": ["risk_on", "m2_acceleration", "dollar_weakness"],
    "business-cycle": ["risk_off", "yield_curve_uninversion", "credit_stress", "macro_summer_entry"],
}

ALLOCATION = {
    "RISK_ON": {
        "deploy_pct": 80,
        "cash_pct": 20,
        "action": "Add to risk assets on dips. Stay invested.",
        "favor": "Nasdaq, AI infrastructure, crypto on dips",
        "trim": "Cash overweight, defensive positions",
    },
    "NEUTRAL": {
        "deploy_pct": 65,
        "cash_pct": 35,
        "action": "Hold. No new risk until signal improves.",
        "favor": "Quality growth, selective tech, gold",
        "trim": "Aggressive leverage and speculative alts",
    },
    "RISK_OFF": {
        "deploy_pct": 40,
        "cash_pct": 60,
        "action": "Raise cash. Wait for signal to flip.",
        "favor": "Cash, gold, defensive exposure",
        "trim": "High beta equities and speculative crypto",
    },
}

HISTORY_START = "2000-01-01"
ISM_PROXY_SERIES_ID = "IPMAN"


def signal_transition_dates(active: pd.Series) -> list[pd.Timestamp]:
    normalized = active.fillna(False).astype(bool)
    transitions = normalized & ~normalized.shift(1, fill_value=False)
    return list(normalized.index[transitions])


def calculate_forward_returns(
    event_dates: list[pd.Timestamp],
    asset_prices: pd.Series,
    horizons: list[int],
    today: pd.Timestamp | None = None,
) -> dict[str, dict[str, float | int]]:
    results: dict[str, dict[str, float | int]] = {}
    if today is None:
        today = pd.Timestamp.today().normalize()
    prices = asset_prices.sort_index()
    for horizon in horizons:
        returns: list[float] = []
        for event_date in event_dates:
            exit_date = event_date + timedelta(days=horizon)
            if exit_date > today:
                continue
            entry = prices.asof(event_date)
            exit_price = prices.asof(exit_date)
            if pd.isna(entry) or pd.isna(exit_price) or entry == 0:
                continue
            returns.append(float((exit_price - entry) / entry * 100))
        if returns:
            results[str(horizon)] = {
                "avg": round(mean(returns), 1),
                "win_rate": round(sum(value > 0 for value in returns) / len(returns), 2),
                "n": len(returns),
                "best": round(max(returns), 1),
                "worst": round(min(returns), 1),
            }
    return results


def _as_monthly_last(series: pd.Series) -> pd.Series:
    monthly = series.groupby(series.index.to_period("M")).last()
    monthly.index = monthly.index.to_timestamp("M")
    return monthly.dropna().sort_index()


def _format_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{digits}f}%"


@dataclass
class BacktestCacheStatus:
    last_calculated: str | None
    stale: bool
    available: bool


class BacktestService:
    def __init__(
        self,
        settings: Settings,
        fred: FredClient | None = None,
        ism: ISMClient | None = None,
    ) -> None:
        self.settings = settings
        self.fred = fred or FredClient(settings.fred_api_key)
        self.ism = ism or ISMClient()
        self.cache_path = settings.runtime_dir / "backtest_results.json"
        self.lock = threading.Lock()
        self._refresh_thread: threading.Thread | None = None

    def ensure_cache_async(self) -> None:
        if not self.should_refresh():
            return
        with self.lock:
            if self._refresh_thread and self._refresh_thread.is_alive():
                return
            self._refresh_thread = threading.Thread(target=self.refresh_cache, daemon=True)
            self._refresh_thread.start()

    def should_refresh(self) -> bool:
        status = self.cache_status()
        return not status.available or status.stale

    def cache_status(self) -> BacktestCacheStatus:
        if not self.cache_path.exists():
            return BacktestCacheStatus(last_calculated=None, stale=True, available=False)
        try:
            payload = json.loads(self.cache_path.read_text())
            last_calculated = payload.get("last_calculated")
            if not last_calculated:
                return BacktestCacheStatus(last_calculated=None, stale=True, available=False)
            most_recent_sunday = self._most_recent_sunday()
            stale = date.fromisoformat(last_calculated) < most_recent_sunday
            return BacktestCacheStatus(last_calculated=last_calculated, stale=stale, available=True)
        except Exception:
            return BacktestCacheStatus(last_calculated=None, stale=True, available=False)

    def refresh_cache(self) -> dict[str, Any]:
        payload = self._calculate_results()
        with self.lock:
            self.cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return payload

    def load_cache(self) -> dict[str, Any]:
        with self.lock:
            if not self.cache_path.exists():
                return {"last_calculated": None, "signals": {}}
            return json.loads(self.cache_path.read_text())

    def dashboard_playbook(self, slug: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        cache = self.load_cache()
        allocation_key = self._allocation_key(snapshot["signal"]["label"])
        allocation = ALLOCATION[allocation_key]
        cards = [self._signal_card(signal_key, cache.get("signals", {}).get(signal_key)) for signal_key in PAGE_SIGNALS[slug]]
        return {
            "available": bool(cache.get("signals")),
            "last_calculated": cache.get("last_calculated"),
            "stale": self.cache_status().stale,
            "allocation": {
                "key": allocation_key,
                **allocation,
            },
            "conviction": self._page_conviction(slug, cards),
            "cards": cards,
        }

    def _calculate_results(self) -> dict[str, Any]:
        assets = self._load_assets()
        macro = self._load_macro_data(assets["DXY"])
        signal_events = {
            "risk_on": self._risk_on_events(macro),
            "risk_off": self._risk_off_events(macro),
            "yield_curve_uninversion": self._yield_curve_uninversion_events(macro),
            "m2_acceleration": self._m2_acceleration_events(macro),
            "dollar_weakness": self._dollar_weakness_events(macro),
            "credit_stress": self._credit_stress_events(macro),
            "macro_summer_entry": self._macro_summer_events(macro),
        }

        payload: dict[str, Any] = {
            "last_calculated": datetime.now().date().isoformat(),
            "signals": {},
        }
        for signal_key, event_dates in signal_events.items():
            spec = SIGNAL_SPECS[signal_key]
            signal_payload = {
                "label": spec["label"],
                "subtitle": spec["subtitle"],
                "event_count": len(event_dates),
                "event_dates": [timestamp.date().isoformat() for timestamp in event_dates],
                "assets": {},
            }
            for asset_key in spec["assets"]:
                asset_spec = ASSET_SPECS[asset_key]
                signal_payload["assets"][asset_key] = {
                    "label": asset_spec["label"],
                    "since": asset_spec["since"],
                    "confidence": asset_spec["confidence"],
                    "results": calculate_forward_returns(event_dates, assets[asset_key], spec["horizons"]),
                }
            payload["signals"][signal_key] = signal_payload
        return payload

    def _load_assets(self) -> dict[str, pd.Series]:
        assets: dict[str, pd.Series] = {}
        for asset_key, spec in ASSET_SPECS.items():
            data = yf.download(spec["ticker"], start=spec["start"], auto_adjust=True, progress=False)
            close = data["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            assets[asset_key] = close.dropna().sort_index()
        return assets

    def _load_macro_data(self, dxy_daily: pd.Series) -> dict[str, pd.Series]:
        m2 = self._fred_series("M2SL")
        rrp = self._fred_series("RRPONTSYD")
        yield_curve = self._fred_series("T10Y2Y")
        credit_spreads = self._fred_series("BAMLH0A0HYM2") * 100
        ism = self._historical_ism_series()

        monthly = pd.DataFrame(
            {
                "m2": _as_monthly_last(m2),
                "rrp": _as_monthly_last(rrp),
                "dxy": _as_monthly_last(dxy_daily),
                "yield_curve": _as_monthly_last(yield_curve),
                "credit_spreads": _as_monthly_last(credit_spreads),
                "ism": _as_monthly_last(ism),
            }
        ).dropna()
        monthly["m2_mom"] = monthly["m2"].pct_change() * 100
        monthly["rrp_change"] = monthly["rrp"].diff()
        monthly["credit_change"] = monthly["credit_spreads"].diff()
        monthly["ism_change"] = monthly["ism"].diff()
        monthly["dxy_3m_change"] = monthly["dxy"] - monthly["dxy"].shift(3)

        return {
            "monthly": monthly,
            "yield_curve_daily": yield_curve,
            "credit_daily": credit_spreads,
        }

    def _fred_series(self, series_id: str) -> pd.Series:
        observations = self.fred.observations(series_id, limit=None, sort_order="asc", observation_start=HISTORY_START)
        index = pd.to_datetime([item.date for item in observations])
        values = [item.value for item in observations]
        return pd.Series(values, index=index, dtype="float64").sort_index()

    def _historical_ism_series(self) -> pd.Series:
        proxy = self._fred_series(ISM_PROXY_SERIES_ID)
        try:
            archive_points = self.ism.historical_manufacturing_pmi()
        except Exception:
            return proxy

        if not archive_points:
            return proxy

        official_index = pd.to_datetime([point.period_end.isoformat() for point in archive_points])
        official_values = [point.value for point in archive_points]
        official = pd.Series(official_values, index=official_index, dtype="float64").sort_index()

        first_official_date = official.index.min()
        backfill = proxy[proxy.index < first_official_date]
        combined = pd.concat([backfill, official]).sort_index()
        return combined[~combined.index.duplicated(keep="last")]

    def _risk_on_events(self, macro: dict[str, pd.Series]) -> list[pd.Timestamp]:
        monthly = macro["monthly"]
        liquidity = (monthly["m2_mom"] > 0.3) & (monthly["rrp_change"] < 0) & (monthly["dxy"] < 101)
        cycle = (monthly["ism"] > 52) & (monthly["yield_curve"] > 0) & (monthly["credit_spreads"] < 350)
        return signal_transition_dates(liquidity & cycle)

    def _risk_off_events(self, macro: dict[str, pd.Series]) -> list[pd.Timestamp]:
        monthly = macro["monthly"]
        liquidity = (monthly["dxy"] > 104) | (monthly["m2_mom"] < 0)
        cycle = (monthly["ism"] < 50) | (monthly["credit_spreads"] > 500)
        return signal_transition_dates(liquidity | cycle)

    def _yield_curve_uninversion_events(self, macro: dict[str, pd.Series]) -> list[pd.Timestamp]:
        series = macro["yield_curve_daily"].dropna().sort_index()
        active = (series >= 0) & (series.shift(1) < 0)
        return list(series.index[active.fillna(False)])

    def _m2_acceleration_events(self, macro: dict[str, pd.Series]) -> list[pd.Timestamp]:
        monthly = macro["monthly"]
        active = (monthly["m2_mom"] > 0.5) & (monthly["m2_mom"].shift(1) > 0.5)
        return signal_transition_dates(active)

    def _dollar_weakness_events(self, macro: dict[str, pd.Series]) -> list[pd.Timestamp]:
        monthly = macro["monthly"]
        active = (monthly["dxy"] < 100) & (monthly["dxy_3m_change"] < 0)
        return signal_transition_dates(active)

    def _credit_stress_events(self, macro: dict[str, pd.Series]) -> list[pd.Timestamp]:
        series = macro["credit_daily"].dropna().sort_index()
        active = (series >= 500) & (series.shift(1) < 500)
        return list(series.index[active.fillna(False)])

    def _macro_summer_events(self, macro: dict[str, pd.Series]) -> list[pd.Timestamp]:
        monthly = macro["monthly"]
        yield_cross = (monthly["yield_curve"] >= 0) & (monthly["yield_curve"].shift(1) < 0)
        active = yield_cross & (monthly["ism_change"] > 0) & (monthly["credit_change"] < 0)
        return signal_transition_dates(active)

    def _signal_card(self, signal_key: str, cached_signal: dict[str, Any] | None) -> dict[str, Any]:
        spec = SIGNAL_SPECS[signal_key]
        if not cached_signal:
            return {
                "signal_key": signal_key,
                "label": spec["label"],
                "subtitle": spec["subtitle"],
                "available": False,
                "warning": "Backtest cache pending. Results will appear after the scheduled calculation finishes.",
                "assets": [],
                "callout": None,
            }

        assets: list[dict[str, Any]] = []
        has_limited_samples = False
        for asset_key in spec["assets"]:
            asset_cache = cached_signal["assets"].get(asset_key, {})
            results = asset_cache.get("results", {})
            horizon_map: dict[str, Any] = {}
            for horizon in spec["horizons"]:
                raw = results.get(str(horizon))
                if not raw:
                    horizon_map[str(horizon)] = {
                        "label": HORIZONS[horizon],
                        "display_avg": "n/a",
                        "display_win_rate": "n/a",
                        "n": 0,
                        "missing": True,
                        "limited": False,
                    }
                    continue
                limited = raw["n"] < 5
                has_limited_samples = has_limited_samples or limited
                avg_display = _format_pct(raw["avg"])
                win_rate_display = f"{round(raw['win_rate'] * 100):.0f}%"
                horizon_map[str(horizon)] = {
                    "label": HORIZONS[horizon],
                    "avg": raw["avg"],
                    "win_rate": raw["win_rate"],
                    "n": raw["n"],
                    "best": raw["best"],
                    "worst": raw["worst"],
                    "display_avg": f"{avg_display}*" if limited else avg_display,
                    "display_win_rate": f"{win_rate_display}*" if limited else win_rate_display,
                    "missing": False,
                    "limited": limited,
                }
            btc_warning = asset_key == "BTC" and cached_signal.get("event_count", 0) < 10
            assets.append(
                {
                    "symbol": asset_key,
                    "label": asset_cache.get("label", asset_key),
                    "since": asset_cache.get("since"),
                    "confidence": asset_cache.get("confidence"),
                    "warning": "Direction only — never use for precise sizing." if btc_warning else None,
                    "horizons": horizon_map,
                }
            )
        card_warnings = ["18m/2yr windows exclude recent signals where the window hasn't elapsed yet."]
        if has_limited_samples:
            card_warnings.append("Values marked * are based on fewer than 5 complete cases.")
        return {
            "signal_key": signal_key,
            "label": cached_signal.get("label", spec["label"]),
            "subtitle": cached_signal.get("subtitle", spec["subtitle"]),
            "available": True,
            "event_count": cached_signal.get("event_count", 0),
            "assets": assets,
            "warning": " ".join(card_warnings),
            "callout": self._build_callout(spec["direction"], assets, spec["horizons"], cached_signal.get("label", spec["label"])),
        }

    def _build_callout(
        self,
        direction: str,
        assets: list[dict[str, Any]],
        horizons: list[int],
        signal_label: str,
    ) -> str | None:
        choice = self._callout_choice(direction, assets, horizons, min_cases=5)
        limited = False
        if choice is None:
            choice = self._callout_choice(direction, assets, horizons, min_cases=1)
            limited = choice is not None
        if choice is None:
            return None

        raw_avg, chosen_asset, chosen_horizon, chosen_n = choice
        prefix = "Limited sample: " if limited else ""
        return (
            f"{prefix}At {signal_label}, {chosen_asset} has averaged {_format_pct(raw_avg)} over {HORIZONS[chosen_horizon]} "
            f"in {chosen_n} complete cases."
        )

    def _callout_choice(
        self,
        direction: str,
        assets: list[dict[str, Any]],
        horizons: list[int],
        min_cases: int,
    ) -> tuple[float, str, int, int] | None:
        preferred_horizons = list(reversed(horizons))
        choice: tuple[float, str, int, int] | None = None
        choice_score: float | None = None
        for horizon in preferred_horizons:
            for asset in assets:
                entry = asset["horizons"].get(str(horizon))
                if not entry or entry["n"] < min_cases or "avg" not in entry:
                    continue
                score = -entry["avg"] if direction == "defensive" else entry["avg"]
                if choice_score is None or score > choice_score:
                    choice_score = score
                    choice = (entry["avg"], asset["label"], horizon, entry["n"])
            if choice is not None:
                break
        return choice

    def _page_conviction(self, slug: str, cards: list[dict[str, Any]]) -> str | None:
        for signal_key in PAGE_SIGNALS[slug]:
            card = next((item for item in cards if item["signal_key"] == signal_key and item.get("callout")), None)
            if card:
                return card["callout"]
        return None

    def _allocation_key(self, signal_label: str) -> str:
        if signal_label == "RISK ON":
            return "RISK_ON"
        if signal_label == "RISK OFF":
            return "RISK_OFF"
        return "NEUTRAL"

    def _most_recent_sunday(self) -> date:
        today = datetime.now().date()
        return today - timedelta(days=(today.weekday() + 1) % 7)
