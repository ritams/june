from __future__ import annotations

import math
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings
from app.models import Dashboard, Metric
from app.services.fred import FredClient, Observation
from app.services.ism import ISMClient
from app.services.market import MarketClient
from app.services.perplexity import PerplexityClient
from app.services.stats import latest_zscore


SERIES_IDS = {
    "m2": "M2SL",
    "rrp": "RRPONTSYD",
    "tga": "WTREGEN",
    "fed_balance_sheet": "WALCL",
    "yield_curve": "T10Y2Y",
    "credit_spreads": "BAMLH0A0HYM2",
    "jobless_claims": "IC4WSA",
    "korean_exports": "XTEXVA01KRM659S",
}


def safe_pct_change(current: float, previous: float | None) -> float | None:
    if previous in (None, 0):
        return None
    return ((current - previous) / previous) * 100


def fmt_change(value: float | None, suffix: str = "%", signed: bool = True, precision: int = 1) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{precision}f}{suffix}"


def fmt_billions(value: float) -> str:
    return f"${value:,.0f}B"


def fmt_trillions(value: float) -> str:
    return f"${value:.1f}T"


def metric_direction(change: float | None, tolerance: float = 0.0) -> str:
    if change is None:
        return "unknown"
    if change > tolerance:
        return "rising"
    if change < -tolerance:
        return "falling"
    return "flat"


def summarize_section(metrics: dict[str, Metric], section: str) -> tuple[str, str, str]:
    if section == "liquidity":
        m2_mom = metrics["m2"].details.get("mom")
        rrp_direction = metrics["rrp"].details.get("direction")
        dxy = metrics["dxy"].raw_value
        if m2_mom is not None and m2_mom > 0.3 and rrp_direction == "falling" and dxy is not None and dxy < 101:
            return "EXPANDING", "positive", "Liquidity is expanding across money supply, funding, and dollar conditions."
        if (dxy is not None and dxy > 104) or (m2_mom is not None and m2_mom < 0):
            return "CONTRACTING", "negative", "Liquidity is tightening and becoming a headwind for risk assets."
        return "NEUTRAL", "neutral", "Liquidity inputs are mixed, so the backdrop is not fully supportive."

    ism = metrics["ism_pmi"].raw_value
    yield_curve = metrics["yield_curve"].raw_value
    spreads = metrics["credit_spreads"].raw_value
    if ism is not None and yield_curve is not None and spreads is not None:
        if ism > 52 and yield_curve > 0 and spreads < 350:
            return "EXPANSION", "positive", "The cycle remains in expansion with supportive manufacturing, curve, and credit signals."
        if ism > 50 and spreads < 450:
            return "LATE CYCLE", "neutral", "Growth is still positive, but conditions are no longer cleanly early-cycle."
        if ism < 50 or spreads > 500:
            return "CONTRACTION", "negative", "Cycle indicators are deteriorating into a defensive macro regime."
    return "TRANSITION", "neutral", "Cycle indicators are at an inflection rather than in a confirmed regime."


def overall_signal(liquidity: Dashboard, cycle: Dashboard) -> tuple[str, str, str, str]:
    if liquidity.status == "EXPANDING" and cycle.status == "EXPANSION":
        return (
            "RISK ON",
            "positive",
            "Liquidity and cycle are aligned in a supportive regime.",
            "Add to high conviction positions on dips.",
        )
    if liquidity.status == "CONTRACTING" or cycle.status == "CONTRACTION":
        return (
            "RISK OFF",
            "negative",
            "Liquidity or cycle has moved into a defensive regime.",
            "Reduce risk and protect capital.",
        )
    return (
        "SELECTIVE",
        "neutral",
        "Liquidity and cycle are not fully aligned.",
        "Stay selective until both dashboards confirm.",
    )


class DashboardService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fred = FredClient(settings.fred_api_key)
        self.ism = ISMClient()
        self.market = MarketClient()
        self.perplexity = PerplexityClient(settings.perplexity_api_key, settings.perplexity_model)
        self.lock = threading.Lock()
        self.cache: dict | None = None
        self.cache_timestamp = 0.0

    def get_snapshot(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self.cache and (now - self.cache_timestamp) < self.settings.cache_ttl_seconds:
            return self.cache

        with self.lock:
            if not force and self.cache and (time.time() - self.cache_timestamp) < self.settings.cache_ttl_seconds:
                return self.cache
            snapshot = self._build_snapshot()
            self.cache = snapshot
            self.cache_timestamp = time.time()
            return snapshot

    def _build_snapshot(self) -> dict:
        liquidity = self._build_liquidity_dashboard()
        cycle = self._build_cycle_dashboard()
        signal, signal_tone, signal_summary, signal_action = overall_signal(liquidity, cycle)
        generated_at = self._now_display().isoformat()
        dashboards = {
            liquidity.slug: liquidity.to_dict(),
            cycle.slug: cycle.to_dict(),
        }
        return {
            "generated_at": generated_at,
            "signal": {
                "label": signal,
                "tone": signal_tone,
                "summary": signal_summary,
                "action": signal_action,
            },
            "dashboards": dashboards,
            "sections": list(dashboards.values()),
            "integrations": {
                "telegram": self.settings.telegram_enabled,
                "google_sheets": self.settings.sheets_enabled,
                "perplexity": self.perplexity.enabled,
                "global_m2_proxy": True,
            },
        }

    def _build_liquidity_dashboard(self) -> Dashboard:
        metrics = [
            self._safe_metric("dxy", "DXY", self._build_dxy_metric),
            self._safe_metric("m2", "US M2", self._build_m2_metric),
            self._safe_metric("rrp", "Reverse Repo", self._build_rrp_metric),
            self._safe_metric("tga", "TGA", self._build_tga_metric),
            self._safe_metric("fed_balance_sheet", "Fed Balance Sheet", self._build_fed_balance_sheet_metric),
            self._safe_metric("global_m2_proxy", "Global M2 Proxy", self._build_global_m2_proxy_metric),
        ]
        metrics_by_key = {metric.key: metric for metric in metrics}
        status, tone, summary = summarize_section(metrics_by_key, "liquidity")
        return Dashboard(
            slug="liquidity",
            title="Liquidity",
            status=status,
            tone=tone,
            summary=summary,
            metrics=metrics,
        )

    def _build_cycle_dashboard(self) -> Dashboard:
        metrics = [
            self._safe_metric("ism_pmi", "ISM PMI", self._build_ism_metric),
            self._safe_metric("yield_curve", "Yield Curve", self._build_yield_curve_metric),
            self._safe_metric("credit_spreads", "Credit Spreads", self._build_credit_spreads_metric),
            self._safe_metric("jobless_claims", "Jobless Claims", self._build_jobless_claims_metric),
            self._safe_metric("korean_exports", "Korean Exports", self._build_korean_exports_metric),
        ]
        metrics_by_key = {metric.key: metric for metric in metrics}
        status, tone, summary = summarize_section(metrics_by_key, "cycle")
        return Dashboard(
            slug="business-cycle",
            title="Business Cycle",
            status=status,
            tone=tone,
            summary=summary,
            metrics=metrics,
        )

    def _now_display(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.display_timezone))

    def _latest_date(self, observations: list[Observation]) -> str | None:
        return observations[0].date if observations else None

    def _fred_zscore(self, series_id: str, window: int = 60, monthly: bool = True) -> float | None:
        try:
            obs = self.fred.observations(series_id, limit=None, sort_order="asc", observation_start="2010-01-01")
            if not obs:
                return None
            series = pd.Series([o.value for o in obs], index=pd.to_datetime([o.date for o in obs]), dtype="float64")
            if monthly:
                series = series.groupby(series.index.to_period("M")).last()
                series.index = series.index.to_timestamp("M")
            return latest_zscore(series, window)
        except Exception:
            return None

    def _dxy_zscore(self, window: int = 60) -> float | None:
        try:
            import yfinance as yf
            history = yf.Ticker("DX-Y.NYB").history(period="10y", interval="1mo", auto_adjust=False)
            if history is None or history.empty:
                return None
            series = history["Close"].dropna()
            return latest_zscore(series, window)
        except Exception:
            return None

    def _safe_metric(self, key: str, label: str, builder) -> Metric:
        try:
            return builder()
        except Exception as exc:
            return Metric(
                key=key,
                label=label,
                display_value="n/a",
                status="neutral",
                summary="Source temporarily unavailable",
                secondary=str(exc)[:160],
                raw_value=None,
                updated_at=None,
                details={},
            )

    def _build_dxy_metric(self) -> Metric:
        quote = self.market.dxy()
        pct_change = safe_pct_change(quote.current, quote.previous)
        direction = metric_direction(pct_change)
        if quote.current < 101 or (pct_change is not None and pct_change < 0):
            status = "positive"
            summary = "Dollar is weakening, which is supportive for risk assets."
        elif quote.current > 104 or (pct_change is not None and pct_change > 0):
            status = "negative"
            summary = "Dollar strength is draining liquidity."
        else:
            status = "neutral"
            summary = "Dollar is range-bound."
        zscore = self._dxy_zscore()
        return Metric(
            key="dxy",
            label="DXY",
            display_value=f"{quote.current:.2f}",
            status=status,
            summary=summary,
            secondary=f"{fmt_change(pct_change)} vs prior print",
            raw_value=quote.current,
            updated_at=quote.updated_at,
            source="yfinance",
            cadence="15 minutes",
            details={"pct_change": pct_change, "direction": direction, "zscore": zscore},
        )

    def _build_m2_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["m2"], limit=13)
        current = observations[0].value
        previous = observations[1].value if len(observations) > 1 else None
        last_year = observations[12].value if len(observations) > 12 else None
        current_trillions = current / 1000
        mom = safe_pct_change(current, previous)
        yoy = safe_pct_change(current, last_year)
        status = "positive" if (mom or 0) > 0 else "negative"
        summary = "Money supply is expanding month over month." if status == "positive" else "Money supply is flat to shrinking."
        return Metric(
            key="m2",
            label="US M2",
            display_value=fmt_trillions(current_trillions),
            status=status,
            summary=summary,
            secondary=f"{fmt_change(mom)} MoM | {fmt_change(yoy)} YoY",
            raw_value=current_trillions,
            updated_at=self._latest_date(observations),
            source="FRED",
            cadence="Weekly on release",
            details={"mom": mom, "yoy": yoy, "current_trillions": current_trillions, "zscore": self._fred_zscore(SERIES_IDS["m2"])},
        )

    def _build_rrp_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["rrp"], limit=2)
        current = observations[0].value
        previous = observations[1].value if len(observations) > 1 else None
        change = None if previous is None else current - previous
        direction = metric_direction(change)
        if direction == "falling":
            status = "positive"
            summary = "RRP is draining and releasing liquidity back into markets."
        elif direction == "rising":
            status = "negative"
            summary = "RRP is rebuilding and absorbing liquidity."
        else:
            status = "neutral"
            summary = "RRP is broadly flat."
        return Metric(
            key="rrp",
            label="Reverse Repo",
            display_value=fmt_billions(current),
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix='B', precision=0)} vs prior day" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
            source="FRED",
            cadence="Daily",
            details={"change": change, "direction": direction, "zscore": self._fred_zscore(SERIES_IDS["rrp"])},
        )

    def _build_tga_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["tga"], limit=2)
        current = observations[0].value / 1000
        previous = observations[1].value / 1000 if len(observations) > 1 else None
        change = None if previous is None else current - previous
        direction = metric_direction(change)
        if direction == "falling":
            status = "positive"
            summary = "Treasury is spending down cash into the economy."
        elif direction == "rising":
            status = "negative"
            summary = "Treasury cash balance is rebuilding."
        else:
            status = "neutral"
            summary = "Treasury cash is stable."
        return Metric(
            key="tga",
            label="TGA",
            display_value=fmt_billions(current),
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix='B', precision=0)} vs prior week" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
            source="FRED",
            cadence="Weekly",
            details={"change": change, "direction": direction, "zscore": self._fred_zscore(SERIES_IDS["tga"])},
        )

    def _build_fed_balance_sheet_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["fed_balance_sheet"], limit=2)
        current = observations[0].value / 1_000_000
        previous = observations[1].value / 1_000_000 if len(observations) > 1 else None
        change = None if previous is None else current - previous
        if change is not None and change < 0:
            status = "negative"
            summary = "Fed balance sheet is shrinking."
        else:
            status = "positive"
            summary = "Fed balance sheet is stable to expanding."
        return Metric(
            key="fed_balance_sheet",
            label="Fed Balance Sheet",
            display_value=fmt_trillions(current),
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix='T', precision=2)} vs prior week" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
            source="FRED",
            cadence="Weekly on release",
            details={"change": change, "zscore": self._fred_zscore(SERIES_IDS["fed_balance_sheet"])},
        )

    def _build_global_m2_proxy_metric(self) -> Metric:
        m2_observations = self.fred.observations(SERIES_IDS["m2"], limit=2)
        if len(m2_observations) < 2:
            raise RuntimeError("Not enough M2 history to calculate the proxy")
        dxy_quote = self.market.dxy()
        monthly_closes = self.market.dxy_monthly_closes(limit=2)
        current_m2 = m2_observations[0].value / 1000
        previous_m2 = m2_observations[1].value / 1000
        current_proxy = (current_m2 / dxy_quote.current) * 100
        previous_proxy = (previous_m2 / monthly_closes[1].close) * 100
        mom = safe_pct_change(current_proxy, previous_proxy)
        if (mom or 0) > 0:
            status = "positive"
            summary = "Global M2 proxy is expanding."
        elif (mom or 0) < 0:
            status = "negative"
            summary = "Global M2 proxy is contracting."
        else:
            status = "neutral"
            summary = "Global M2 proxy is flat."
        return Metric(
            key="global_m2_proxy",
            label="Global M2 Proxy",
            display_value=f"{current_proxy:.1f}",
            status=status,
            summary=summary,
            secondary=f"{fmt_change(mom)} MoM",
            raw_value=current_proxy,
            updated_at=dxy_quote.updated_at or self._latest_date(m2_observations),
            source="Calculated from FRED + yfinance",
            cadence="15 minutes / weekly",
            details={"mom": mom, "previous": previous_proxy, "zscore": self._fred_zscore(SERIES_IDS["m2"])},
        )

    def _build_ism_metric(self) -> Metric:
        if self.perplexity.enabled:
            reading = self.perplexity.latest_ism_manufacturing_pmi()
            current = reading.current
            previous = reading.previous
            change = None if previous is None else current - previous
            if current > 52:
                status = "positive"
                summary = "Manufacturing is in expansion."
            elif current >= 50:
                status = "neutral"
                summary = "Manufacturing is positive, but only marginally."
            else:
                status = "negative"
                summary = "Manufacturing is in contraction."
            return Metric(
                key="ism_pmi",
                label="ISM PMI",
                display_value=f"{current:.1f}",
                status=status,
                summary=summary,
                secondary=(
                    f"{fmt_change(change, suffix=' pts', precision=1)} vs prior release"
                    if change is not None
                    else f"Perplexity read for {reading.period_label or 'latest release'}"
                ),
                raw_value=current,
                updated_at=reading.release_date,
                source="Perplexity sonar-pro",
                cadence="2 hours",
                details={"previous": previous, "change": change, "period_label": reading.period_label, "zscore": self._fred_zscore("IPMAN")},
            )

        reading = self.ism.latest_manufacturing_pmi()
        current = reading.current
        previous = reading.previous
        change = None if previous is None else current - previous
        if current > 52:
            status = "positive"
            summary = "Manufacturing is in expansion."
        elif current >= 50:
            status = "neutral"
            summary = "Manufacturing is positive, but only marginally."
        else:
            status = "negative"
            summary = "Manufacturing is in contraction."
        return Metric(
            key="ism_pmi",
            label="ISM PMI",
            display_value=f"{current:.1f}",
            status=status,
            summary=summary,
            secondary=(
                f"{fmt_change(change, suffix=' pts', precision=1)} vs {reading.previous_month}"
                if change is not None and reading.previous_month
                else f"Official ISM report for {reading.data_month} {reading.data_year}"
            ),
            raw_value=current,
            updated_at=reading.release_at,
            source="Official ISM",
            cadence="2 hours",
            details={"previous": previous, "change": change, "period_label": f"{reading.data_month} {reading.data_year}", "zscore": self._fred_zscore("IPMAN")},
        )

    def _build_yield_curve_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["yield_curve"], limit=2)
        current = observations[0].value
        previous = observations[1].value if len(observations) > 1 else None
        change = None if previous is None else current - previous
        crossed_positive = previous is not None and previous <= 0 < current
        if crossed_positive:
            status = "neutral"
            summary = "Yield curve has just flipped positive from inversion."
        elif current > 0:
            status = "positive"
            summary = "Yield curve is positive and supportive."
        else:
            status = "negative"
            summary = "Yield curve remains inverted."
        return Metric(
            key="yield_curve",
            label="Yield Curve",
            display_value=f"{current:+.2f}%",
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix=' pts', precision=2)} vs prior day" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
            source="FRED",
            cadence="Daily",
            details={"previous": previous, "change": change, "crossed_positive": crossed_positive, "zscore": self._fred_zscore(SERIES_IDS["yield_curve"])},
        )

    def _build_credit_spreads_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["credit_spreads"], limit=2)
        current_bps = observations[0].value * 100
        previous_bps = observations[1].value * 100 if len(observations) > 1 else None
        change = None if previous_bps is None else current_bps - previous_bps
        if current_bps < 350:
            status = "positive"
            summary = "Credit stress is contained."
        elif current_bps > 500:
            status = "negative"
            summary = "Credit stress is elevated."
        else:
            status = "neutral"
            summary = "Credit spreads are in a caution range."
        return Metric(
            key="credit_spreads",
            label="Credit Spreads",
            display_value=f"{current_bps:.0f} bps",
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix=' bps', precision=0)} vs prior day" if change is not None else None,
            raw_value=current_bps,
            updated_at=self._latest_date(observations),
            source="FRED",
            cadence="Daily",
            details={"previous": previous_bps, "change": change, "zscore": self._fred_zscore(SERIES_IDS["credit_spreads"])},
        )

    def _build_jobless_claims_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["jobless_claims"], limit=5)
        current = observations[0].value
        previous_month = observations[4].value if len(observations) > 4 else None
        month_change = None if previous_month is None else current - previous_month
        rising_four_weeks = len(observations) >= 5 and all(
            observations[index].value > observations[index + 1].value for index in range(4)
        )
        if current < 250_000 and not rising_four_weeks:
            status = "positive"
            summary = "Jobless claims remain below 250k and stable."
        elif rising_four_weeks or current > 300_000:
            status = "negative"
            summary = "Jobless claims are rising persistently."
        else:
            status = "neutral"
            summary = "Jobless claims are mixed."
        return Metric(
            key="jobless_claims",
            label="Jobless Claims",
            display_value=f"{current/1000:.0f}k",
            status=status,
            summary=summary,
            secondary=(
                f"{fmt_change(month_change / 1000 if month_change is not None else None, suffix='k', precision=0)} vs prior month"
                if month_change is not None
                else None
            ),
            raw_value=current,
            updated_at=self._latest_date(observations),
            source="FRED",
            cadence="Weekly on release",
            details={"month_change": month_change, "rising_four_weeks": rising_four_weeks, "zscore": self._fred_zscore(SERIES_IDS["jobless_claims"])},
        )

    def _build_korean_exports_metric(self) -> Metric:
        if self.perplexity.enabled:
            reading = self.perplexity.latest_korean_exports()
            current = reading.current
            previous = reading.previous
            change = None if previous is None else current - previous
            if current > 0 and (change is None or change > 0):
                status = "positive"
                summary = "Export growth is accelerating."
            elif current < 0 or (change is not None and change < 0):
                status = "negative"
                summary = "Export momentum is decelerating."
            else:
                status = "neutral"
                summary = "Export momentum is stable."
            return Metric(
                key="korean_exports",
                label="Korean Exports",
                display_value=f"{current:.1f}% YoY",
                status=status,
                summary=summary,
                secondary=(
                    f"{fmt_change(change, suffix=' pts', precision=1)} vs prior release"
                    if change is not None
                    else f"Perplexity read for {reading.period_label or 'latest release'}"
                ),
                raw_value=current,
                updated_at=reading.release_date,
                source="Perplexity sonar-pro",
                cadence="2 hours",
                details={"previous": previous, "change": change, "period_label": reading.period_label, "zscore": self._fred_zscore(SERIES_IDS["korean_exports"])},
            )

        observations = self.fred.observations(SERIES_IDS["korean_exports"], limit=2)
        current = observations[0].value
        previous = observations[1].value if len(observations) > 1 else None
        change = None if previous is None else current - previous
        if current > 0 and (change is None or change > 0):
            status = "positive"
            summary = "Export growth is accelerating."
        elif current < 0 or (change is not None and change < 0):
            status = "negative"
            summary = "Export momentum is decelerating."
        else:
            status = "neutral"
            summary = "Export growth is stable."
        return Metric(
            key="korean_exports",
            label="Korean Exports",
            display_value=f"{current:.1f}% YoY",
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix=' pts', precision=1)} vs prior month" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
            source="FRED",
            cadence="Monthly",
            details={"previous": previous, "change": change, "zscore": self._fred_zscore(SERIES_IDS["korean_exports"])},
        )
