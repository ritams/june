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
from app.services.stats import tstat_from_returns


HORIZONS = {
    180: "6m",
    365: "1yr",
    540: "18m",
    730: "2yr",
}

_HIGH = {"tone": "positive", "label": "HIGH", "description": "20+ complete cycles"}
_MED = {"tone": "neutral", "label": "MEDIUM", "description": "Shorter ETF history"}
_LOW = {"tone": "negative", "label": "LOW", "description": "Direction only, never sizing"}
_LIMITED = {"tone": "negative", "label": "LIMITED", "description": "Short post-2008 history; included with low_history flag"}


def _spec(ticker, label, bucket, *, start="2008-01-01", since=None, benchmark=None, confidence=None, low_history=False):
    return {
        "ticker": ticker,
        "label": label,
        "bucket": bucket,
        "start": start,
        "since": since or start[:4],
        "benchmark": benchmark,
        "confidence": confidence or _MED,
        "low_history": low_history,
    }


ASSET_SPECS = {
    # Equity regions (vs ACWI)
    "SPY": _spec("SPY", "S&P 500", "equity_region", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "QQQ": _spec("QQQ", "Nasdaq", "equity_region", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "IWM": _spec("IWM", "US Small Cap", "equity_region", start="2000-05-01", benchmark="ACWI", confidence=_HIGH),
    "EWJ": _spec("EWJ", "Japan", "equity_region", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "EWY": _spec("EWY", "South Korea", "equity_region", start="2000-05-01", benchmark="ACWI", confidence=_HIGH),
    "EFA": _spec("EFA", "Developed ex-US", "equity_region", start="2001-08-01", benchmark="ACWI", confidence=_HIGH),
    "EEM": _spec("EEM", "Emerging Markets", "equity_region", start="2003-04-01", benchmark="ACWI", confidence=_HIGH),
    "EWU": _spec("EWU", "UK", "equity_region", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "EWL": _spec("EWL", "Switzerland", "equity_region", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "EWZ": _spec("EWZ", "Brazil", "equity_region", start="2000-07-01", benchmark="ACWI", confidence=_HIGH),
    "EWA": _spec("EWA", "Australia", "equity_region", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "EWW": _spec("EWW", "Mexico", "equity_region", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),

    # Equity sectors (vs ACWI)
    "XLK": _spec("XLK", "Technology", "equity_sector", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "XLY": _spec("XLY", "Cons Discretionary", "equity_sector", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "XLE": _spec("XLE", "Energy", "equity_sector", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "XLF": _spec("XLF", "Financials", "equity_sector", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "XLV": _spec("XLV", "Healthcare", "equity_sector", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "XLU": _spec("XLU", "Utilities", "equity_sector", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "XLP": _spec("XLP", "Cons Staples", "equity_sector", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "XLI": _spec("XLI", "Industrials", "equity_sector", start="2000-01-01", benchmark="ACWI", confidence=_HIGH),
    "SMH": _spec("SMH", "Semiconductors", "equity_sector", start="2000-06-01", benchmark="ACWI", confidence=_HIGH),

    # Fixed income (vs AGG)
    "TLT": _spec("TLT", "Long Bonds 20yr+", "fixed_income", start="2002-08-01", benchmark="AGG", confidence=_HIGH),
    "IEF": _spec("IEF", "Medium Bonds 7-10yr", "fixed_income", start="2002-08-01", benchmark="AGG", confidence=_HIGH),
    "TIP": _spec("TIP", "TIPS", "fixed_income", start="2003-12-01", benchmark="AGG", confidence=_HIGH),
    "HYG": _spec("HYG", "High Yield Credit", "fixed_income", start="2007-04-01", benchmark="AGG", confidence=_HIGH),
    "LQD": _spec("LQD", "IG Credit", "fixed_income", start="2002-07-01", benchmark="AGG", confidence=_HIGH),

    # Currencies (already vs USD)
    "FXA": _spec("FXA", "AUD", "currency", start="2006-06-01", confidence=_MED),
    "FXC": _spec("FXC", "CAD", "currency", start="2006-06-01", confidence=_MED),
    "FXB": _spec("FXB", "GBP", "currency", start="2006-06-01", confidence=_MED),
    "FXF": _spec("FXF", "CHF", "currency", start="2006-06-01", confidence=_MED),
    "FXY": _spec("FXY", "JPY", "currency", start="2007-02-01", confidence=_MED),
    "UUP": _spec("UUP", "USD Bull", "currency", start="2007-02-01", confidence=_MED),

    # Commodities (vs GSG)
    "IAU": _spec("IAU", "Gold", "commodity", start="2005-01-01", benchmark="GSG", confidence=_MED),
    "COPX": _spec("COPX", "Copper Miners", "commodity", start="2009-04-01", benchmark="GSG", confidence=_MED),
    "USO": _spec("USO", "Oil", "commodity", start="2006-04-01", benchmark="GSG", confidence=_MED),
    "DBA": _spec("DBA", "Agriculture", "commodity", start="2007-01-01", benchmark="GSG", confidence=_MED),
    "DJP": _spec("DJP", "Broad Commodities", "commodity", start="2006-06-01", benchmark="GSG", confidence=_MED),
    "HPS-A.TO": _spec("HPS-A.TO", "Hammond Power (TSX)", "commodity", start="2010-01-01", benchmark="GSG", confidence=_LIMITED, low_history=True),

    # Style factors (vs ACWI)
    "IWF": _spec("IWF", "US Growth", "style", start="2000-05-01", benchmark="ACWI", confidence=_HIGH),
    "IWD": _spec("IWD", "US Value", "style", start="2000-05-01", benchmark="ACWI", confidence=_HIGH),
    "MTUM": _spec("MTUM", "Momentum", "style", start="2013-04-01", benchmark="ACWI", confidence=_LOW),

    # Crypto
    "BTC": _spec("BTC-USD", "BTC", "crypto", start="2014-09-01", confidence=_LOW),

    # Cash proxy (1-3mo T-bills) — used by asset-class bucket only
    "BIL": _spec("BIL", "1-3mo T-Bills", "_cash_proxy", start="2007-06-01"),

    # Internal: DXY price series, used by macro data layer (not in scenario universe)
    "DXY": _spec("DX-Y.NYB", "DXY", "_internal", start="2000-01-01"),
}

# Benchmarks for relative-return computation. Pulled like assets but excluded from ranking.
BENCHMARK_SPECS = {
    "ACWI": _spec("ACWI", "MSCI ACWI", "_benchmark", start="2008-04-01"),
    "AGG":  _spec("AGG",  "Bloomberg Global Agg", "_benchmark", start="2003-09-01"),
    "GSG":  _spec("GSG",  "S&P GSCI", "_benchmark", start="2006-07-01"),
}

# Asset-class proxies for the absolute-return view (Bittel's 6-class top-3/bottom-3)
ASSET_CLASS_PROXIES = {
    "equities":    {"label": "Equities",    "proxy": "SPY"},
    "credit":      {"label": "Credit",      "proxy": "HYG"},
    "commodities": {"label": "Commodities", "proxy": "DJP"},
    "bonds":       {"label": "Bonds",       "proxy": "IEF"},
    "cash":        {"label": "Cash",        "proxy": "BIL"},
    "crypto":      {"label": "Crypto",      "proxy": "BTC"},
}

SCENARIO_BUCKETS = ["asset_class", "equity_region", "equity_sector", "fixed_income", "currency", "commodity", "style", "crypto"]

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
            t_stat = tstat_from_returns(returns)
            results[str(horizon)] = {
                "avg": round(mean(returns), 1),
                "win_rate": round(sum(value > 0 for value in returns) / len(returns), 2),
                "n": len(returns),
                "best": round(max(returns), 1),
                "worst": round(min(returns), 1),
                "t_stat": round(t_stat, 2) if t_stat is not None else None,
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
        # In-memory cache for factor_series() — multiple consumers (Risk Budget,
        # PhaseService, FrameworkPortfolio) call this on the same request. Hitting
        # FRED 8x per consumer trips the 429 limit quickly.
        self._factor_cache: dict[str, pd.Series] | None = None
        self._factor_cache_at: float = 0.0
        self._factor_cache_ttl_seconds: float = 6 * 60 * 60

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
        keys = {"DXY"}
        for spec in SIGNAL_SPECS.values():
            keys.update(spec["assets"])
        assets: dict[str, pd.Series] = {}
        for asset_key in keys:
            spec = ASSET_SPECS[asset_key]
            assets[asset_key] = self._download_close(spec["ticker"], spec["start"])
        return assets

    def _load_macro_data(self, dxy_daily: pd.Series) -> dict[str, pd.Series]:
        m2 = self._fred_series("M2SL")
        rrp = self._fred_series("RRPONTSYD")
        yield_curve = self._fred_series("T10Y2Y")
        credit_spreads = self._fred_series("BAMLH0A0HYM2") * 100
        ism = self._historical_ism_series()
        cpi = self._fred_series("CPIAUCSL")
        two_year = self._fred_series("DGS2")
        oil = self._fred_series("DCOILWTICO")

        monthly = pd.DataFrame(
            {
                "m2": _as_monthly_last(m2),
                "rrp": _as_monthly_last(rrp),
                "dxy": _as_monthly_last(dxy_daily),
                "yield_curve": _as_monthly_last(yield_curve),
                "credit_spreads": _as_monthly_last(credit_spreads),
                "ism": _as_monthly_last(ism),
                "cpi": _as_monthly_last(cpi),
                "two_year": _as_monthly_last(two_year),
                "oil": _as_monthly_last(oil),
            }
        ).dropna(subset=["m2", "dxy", "yield_curve", "ism"])
        monthly["m2_mom"] = monthly["m2"].pct_change() * 100
        monthly["m2_yoy"] = monthly["m2"].pct_change(12) * 100
        monthly["rrp_change"] = monthly["rrp"].diff()
        monthly["credit_change"] = monthly["credit_spreads"].diff()
        monthly["ism_change"] = monthly["ism"].diff()
        monthly["ism_yoy"] = monthly["ism"] - monthly["ism"].shift(12)
        monthly["dxy_3m_change"] = monthly["dxy"] - monthly["dxy"].shift(3)
        monthly["dxy_yoy"] = monthly["dxy"].pct_change(12) * 100
        monthly["cpi_yoy"] = monthly["cpi"].pct_change(12) * 100
        monthly["oil_yoy"] = monthly["oil"].pct_change(12) * 100
        monthly["two_year_yoy"] = monthly["two_year"] - monthly["two_year"].shift(12)

        return {
            "monthly": monthly,
            "yield_curve_daily": yield_curve,
            "credit_daily": credit_spreads,
        }

    # --- Public hooks for the correlation engine (Phase 4c) ---

    def factor_series(self) -> dict[str, pd.Series]:
        """Return the 7 macro factor series indexed by month-end. Used by correlations.

        In-memory cached for `_factor_cache_ttl_seconds` because every consumer
        (Risk Budget, PhaseService, FrameworkPortfolio) calls this on the same
        request — without the cache they each pull ~8 FRED series and quickly
        trip the rate limit.
        """
        import time as _time
        with self.lock:
            if self._factor_cache is not None and (_time.time() - self._factor_cache_at) < self._factor_cache_ttl_seconds:
                return self._factor_cache
        dxy_daily = self._download_close(ASSET_SPECS["DXY"]["ticker"], ASSET_SPECS["DXY"]["start"])
        macro = self._load_macro_data(dxy_daily)
        monthly = macro["monthly"]
        spy_close = self._download_close(ASSET_SPECS["SPY"]["ticker"], ASSET_SPECS["SPY"]["start"])
        spy_monthly = _as_monthly_last(spy_close)
        risk_on_off = spy_monthly.pct_change() * 100
        out = {
            "risk_on_off": risk_on_off.dropna(),
            "growth": monthly["ism_yoy"].dropna(),
            "inflation": monthly["cpi_yoy"].dropna(),
            "short_rates": monthly["two_year_yoy"].dropna(),
            "liquidity": monthly["m2_yoy"].dropna(),
            "dollar": monthly["dxy_yoy"].dropna(),
            "oil": monthly["oil_yoy"].dropna(),
        }
        with self.lock:
            self._factor_cache = out
            self._factor_cache_at = _time.time()
        return out

    def load_universe(self) -> dict[str, pd.Series]:
        """Daily price series for every asset + benchmark + cash proxy. Used by correlations."""
        out: dict[str, pd.Series] = {}
        for key, spec in {**ASSET_SPECS, **BENCHMARK_SPECS}.items():
            if spec["bucket"] == "_internal":
                continue
            try:
                out[key] = self._download_close(spec["ticker"], spec["start"])
            except Exception:
                continue
        return out

    def _download_close(self, ticker: str, start: str) -> pd.Series:
        data = yf.download(ticker, start=start, auto_adjust=True, progress=False)
        close = data["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        return close.dropna().sort_index()

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
                t_stat = raw.get("t_stat")
                horizon_map[str(horizon)] = {
                    "label": HORIZONS[horizon],
                    "avg": raw["avg"],
                    "win_rate": raw["win_rate"],
                    "n": raw["n"],
                    "best": raw["best"],
                    "worst": raw["worst"],
                    "t_stat": t_stat,
                    "display_avg": f"{avg_display}*" if limited else avg_display,
                    "display_win_rate": f"{win_rate_display}*" if limited else win_rate_display,
                    "display_t_stat": "n/a" if t_stat is None else f"{t_stat:+.2f}",
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
