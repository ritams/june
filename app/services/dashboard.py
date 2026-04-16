from __future__ import annotations

import math
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.models import Dashboard, Metric
from app.services.fred import FredClient, Observation
from app.services.ism import ISMClient
from app.services.market import MarketClient


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


def summarize_section(positive: int, negative: int, section: str) -> tuple[str, str]:
    if section == "liquidity":
        if positive >= 4 and negative == 0:
            return "EXPANDING", "positive"
        if negative >= 3:
            return "CONTRACTING", "negative"
        return "MIXED", "neutral"
    if positive >= 4 and negative <= 1:
        return "MID-EXPANSION", "positive"
    if negative >= 3:
        return "SLOWDOWN", "negative"
    return "TRANSITION", "neutral"


def overall_signal(liquidity: Dashboard, cycle: Dashboard) -> tuple[str, str, str]:
    liq_positive, liq_negative = liquidity.counts()
    cyc_positive, cyc_negative = cycle.counts()
    score = (liq_positive + cyc_positive) - (liq_negative + cyc_negative)
    if liq_negative >= 3 or cyc_negative >= 3 or score < 0:
        return "RISK OFF", "negative", "Stay defensive until liquidity and cycle improve."
    if liq_positive >= 3 and cyc_positive >= 3 and score >= 3:
        return "RISK ON", "positive", "Add on weakness while both dashboards stay supportive."
    return "RISK OFF", "negative", "Conditions are mixed, so default to defense."


class DashboardService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fred = FredClient(settings.fred_api_key)
        self.ism = ISMClient()
        self.market = MarketClient()
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
        signal, signal_tone, signal_summary = overall_signal(liquidity, cycle)
        generated_at = self._now_display().isoformat()
        return {
            "generated_at": generated_at,
            "signal": {"label": signal, "tone": signal_tone, "summary": signal_summary},
            "dashboards": {
                "liquidity": liquidity.to_dict(),
                "business-cycle": cycle.to_dict(),
            },
            "integrations": {
                "telegram": self.settings.telegram_enabled,
                "google_sheets": self.settings.sheets_enabled,
                "ism_configured": True,
                "global_m2_proxy_configured": bool(self.settings.global_m2_proxy_series_id),
            },
        }

    def _build_liquidity_dashboard(self) -> Dashboard:
        metrics = [
            self._safe_metric("dxy", "DXY", self._build_dxy_metric),
            self._safe_metric("m2", "US M2", self._build_m2_metric),
            self._safe_metric("rrp", "Reverse Repo", self._build_rrp_metric),
            self._safe_metric("tga", "TGA", self._build_tga_metric),
            self._safe_metric("fed_balance_sheet", "Fed Balance Sheet", self._build_fed_balance_sheet_metric),
        ]
        positive = sum(metric.status == "positive" for metric in metrics)
        negative = sum(metric.status == "negative" for metric in metrics)
        status, tone = summarize_section(positive, negative, "liquidity")
        return Dashboard(slug="liquidity", title="Liquidity", status=status, tone=tone, metrics=metrics)

    def _build_cycle_dashboard(self) -> Dashboard:
        metrics = [
            self._safe_metric("ism_pmi", "ISM PMI", self._build_ism_metric),
            self._safe_metric("yield_curve", "Yield Curve", self._build_yield_curve_metric),
            self._safe_metric("credit_spreads", "Credit Spreads", self._build_credit_spreads_metric),
            self._safe_metric("jobless_claims", "Jobless Claims", self._build_jobless_claims_metric),
            self._safe_metric("korean_exports", "Korean Exports", self._build_korean_exports_metric),
        ]
        positive = sum(metric.status == "positive" for metric in metrics)
        negative = sum(metric.status == "negative" for metric in metrics)
        status, tone = summarize_section(positive, negative, "cycle")
        return Dashboard(slug="business-cycle", title="Business Cycle", status=status, tone=tone, metrics=metrics)

    def _now_display(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.display_timezone))

    def _latest_date(self, observations: list[Observation]) -> str | None:
        return observations[0].date if observations else None

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
                secondary=str(exc)[:140],
                raw_value=None,
                updated_at=None,
            )

    def _build_dxy_metric(self) -> Metric:
        quote = self.market.dxy()
        pct_change = safe_pct_change(quote.current, quote.previous)
        if quote.current < 101 or (pct_change is not None and pct_change < 0):
            status = "positive"
            summary = "Dollar is easing"
        elif quote.current > 104 or (pct_change is not None and pct_change > 0):
            status = "negative"
            summary = "Dollar strength is a headwind"
        else:
            status = "neutral"
            summary = "Dollar is range-bound"
        secondary = f"{fmt_change(pct_change)} vs prior print"
        return Metric(
            key="dxy",
            label="DXY",
            display_value=f"{quote.current:.2f}",
            status=status,
            summary=summary,
            secondary=secondary,
            raw_value=quote.current,
            updated_at=quote.updated_at,
        )

    def _build_m2_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["m2"], limit=13)
        current = observations[0].value
        previous = observations[1].value if len(observations) > 1 else None
        last_year = observations[12].value if len(observations) > 12 else None
        mom = safe_pct_change(current, previous)
        yoy = safe_pct_change(current, last_year)
        status = "positive" if (mom or 0) > 0 else "negative"
        summary = "Money supply is expanding" if status == "positive" else "Money supply is not expanding"
        return Metric(
            key="m2",
            label="US M2",
            display_value=fmt_trillions(current / 1000),
            status=status,
            summary=summary,
            secondary=f"{fmt_change(mom)} MoM | {fmt_change(yoy)} YoY",
            raw_value=current,
            updated_at=self._latest_date(observations),
        )

    def _build_rrp_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["rrp"], limit=2)
        current = observations[0].value
        previous = observations[1].value if len(observations) > 1 else None
        change = None if previous is None else current - previous
        if change is not None and change < 0:
            status = "positive"
            summary = "RRP is draining into markets"
        elif change is not None and change > 0:
            status = "negative"
            summary = "RRP is rebuilding"
        else:
            status = "neutral"
            summary = "RRP is flat"
        return Metric(
            key="rrp",
            label="Reverse Repo",
            display_value=fmt_billions(current),
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix='B', precision=0)} vs prior day" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
        )

    def _build_tga_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["tga"], limit=2)
        current = observations[0].value / 1000
        previous = observations[1].value / 1000 if len(observations) > 1 else None
        change = None if previous is None else current - previous
        if change is not None and change < 0:
            status = "positive"
            summary = "Treasury is spending down cash"
        elif change is not None and change > 0:
            status = "negative"
            summary = "Treasury cash balance is rebuilding"
        else:
            status = "neutral"
            summary = "Treasury cash is stable"
        return Metric(
            key="tga",
            label="TGA",
            display_value=fmt_billions(current),
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix='B', precision=0)} vs prior week" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
        )

    def _build_fed_balance_sheet_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["fed_balance_sheet"], limit=2)
        current = observations[0].value / 1_000_000
        previous = observations[1].value / 1_000_000 if len(observations) > 1 else None
        change = None if previous is None else current - previous
        if change is not None and change > 0.03:
            status = "positive"
            summary = "Fed balance sheet is expanding"
        elif change is not None and change < -0.03:
            status = "negative"
            summary = "Fed balance sheet is shrinking"
        else:
            status = "neutral"
            summary = "Fed balance sheet is steady"
        return Metric(
            key="fed_balance_sheet",
            label="Fed Balance Sheet",
            display_value=fmt_trillions(current),
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix='T', precision=2)} vs prior week" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
        )

    def _build_ism_metric(self) -> Metric:
        reading = self.ism.latest_manufacturing_pmi()
        current = reading.current
        previous = reading.previous
        change = None if previous is None else current - previous
        if current > 50:
            status = "positive"
            summary = "Manufacturing is in expansion"
        elif current < 50:
            status = "negative"
            summary = "Manufacturing is in contraction"
        else:
            status = "neutral"
            summary = "Manufacturing is neutral"
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
        )

    def _build_yield_curve_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["yield_curve"], limit=2)
        current = observations[0].value
        previous = observations[1].value if len(observations) > 1 else None
        change = None if previous is None else current - previous
        status = "positive" if current > 0 else "negative"
        summary = "Yield curve is positive" if status == "positive" else "Yield curve is inverted"
        return Metric(
            key="yield_curve",
            label="Yield Curve",
            display_value=f"{current:.2f}%",
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix=' pts', precision=2)} vs prior day" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
        )

    def _build_credit_spreads_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["credit_spreads"], limit=2)
        current_bps = observations[0].value * 100
        previous_bps = observations[1].value * 100 if len(observations) > 1 else None
        change = None if previous_bps is None else current_bps - previous_bps
        if current_bps < 400:
            status = "positive"
            summary = "Credit stress is contained"
        elif current_bps > 500:
            status = "negative"
            summary = "Credit stress is elevated"
        else:
            status = "neutral"
            summary = "Credit spreads are mixed"
        return Metric(
            key="credit_spreads",
            label="Credit Spreads",
            display_value=f"{current_bps:.0f} bps",
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix=' bps', precision=0)} vs prior day" if change is not None else None,
            raw_value=current_bps,
            updated_at=self._latest_date(observations),
        )

    def _build_jobless_claims_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["jobless_claims"], limit=2)
        current = observations[0].value
        previous = observations[1].value if len(observations) > 1 else None
        change = None if previous is None else current - previous
        if current < 250_000 and (change is None or change <= 10_000):
            status = "positive"
            summary = "Labor market remains stable"
        elif current > 300_000 or (change is not None and change > 25_000):
            status = "negative"
            summary = "Labor market is softening"
        else:
            status = "neutral"
            summary = "Claims are mixed"
        return Metric(
            key="jobless_claims",
            label="Jobless Claims",
            display_value=f"{current/1000:.0f}k",
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change / 1000 if change is not None else None, suffix='k', precision=0)} vs prior week" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
        )

    def _build_korean_exports_metric(self) -> Metric:
        observations = self.fred.observations(SERIES_IDS["korean_exports"], limit=2)
        current = observations[0].value
        previous = observations[1].value if len(observations) > 1 else None
        change = None if previous is None else current - previous
        if current > 0:
            status = "positive"
            summary = "Export growth is positive"
        elif current < 0:
            status = "negative"
            summary = "Export growth is negative"
        else:
            status = "neutral"
            summary = "Export growth is flat"
        return Metric(
            key="korean_exports",
            label="Korean Exports",
            display_value=f"{current:.1f}% YoY",
            status=status,
            summary=summary,
            secondary=f"{fmt_change(change, suffix=' pts', precision=1)} vs prior month" if change is not None else None,
            raw_value=current,
            updated_at=self._latest_date(observations),
        )
