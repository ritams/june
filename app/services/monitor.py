from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.services.dashboard import DashboardService
from app.services.sheets import SheetsClient
from app.services.state import StateStore
from app.services.telegram import TelegramClient


class MonitorService:
    def __init__(
        self,
        settings: Settings,
        dashboard_service: DashboardService,
        telegram: TelegramClient,
        sheets: SheetsClient,
        state_store: StateStore,
    ) -> None:
        self.settings = settings
        self.dashboard_service = dashboard_service
        self.telegram = telegram
        self.sheets = sheets
        self.state_store = state_store

    def run_alert_checks(self) -> list[str]:
        snapshot = self.dashboard_service.get_snapshot(force=True)
        state = self.state_store.load()
        alerts = state.get("alerts", {})
        messages: list[str] = []

        liquidity = {metric["key"]: metric for metric in snapshot["dashboards"]["liquidity"]["metrics"]}
        cycle = {metric["key"]: metric for metric in snapshot["dashboards"]["business-cycle"]["metrics"]}

        checks = [
            (
                "yield_curve_positive",
                cycle["yield_curve"]["raw_value"],
                self._crossed_above(alerts.get("yield_curve_positive"), cycle["yield_curve"]["raw_value"], 0.0),
                "CYCLE TURN: Yield curve confirmed positive. Recovery signal.",
            ),
            (
                "credit_stress",
                cycle["credit_spreads"]["raw_value"],
                self._crossed_above(alerts.get("credit_stress"), cycle["credit_spreads"]["raw_value"], 500.0),
                "CREDIT STRESS: Credit spreads are above 500 bps. Risk off immediately.",
            ),
            (
                "dxy_squeeze",
                liquidity["dxy"]["raw_value"],
                self._crossed_above(alerts.get("dxy_squeeze"), liquidity["dxy"]["raw_value"], 105.0),
                "DOLLAR SQUEEZE: DXY is above 105. Crypto headwind.",
            ),
            (
                "global_m2_proxy_drop",
                liquidity["global_m2_proxy"]["details"].get("mom"),
                self._crossed_below(alerts.get("global_m2_proxy_drop"), liquidity["global_m2_proxy"]["details"].get("mom"), -3.0),
                "LIQUIDITY WARNING: Global M2 proxy is contracting. Reduce risk.",
            ),
        ]

        ism_value = cycle["ism_pmi"]["raw_value"]
        checks.append(
            (
                "ism_cross_50",
                ism_value,
                self._crossed_threshold(alerts.get("ism_cross_50"), ism_value, 50.0),
                "ISM crossed 50. Business cycle inflection point.",
            )
        )

        for key, current, crossed, message in checks:
            alerts[key] = current
            if crossed:
                messages.append(message)
                self._safe_telegram_send(message)

        state["alerts"] = alerts
        self.state_store.save(state)
        return messages

    def send_daily_card(self) -> dict:
        snapshot = self.dashboard_service.get_snapshot(force=True)
        now = datetime.now(ZoneInfo(self.settings.app_timezone)).date().isoformat()
        message = self._format_daily_card(snapshot)
        telegram_sent, telegram_error = self._safe_telegram_send(message)
        sheets_logged, sheets_error = self._safe_sheet_append(self._sheet_row(snapshot))
        state = self.state_store.load()
        state["daily_card_date"] = now
        self.state_store.save(state)
        return {
            "telegram_sent": telegram_sent,
            "telegram_error": telegram_error,
            "sheets_logged": sheets_logged,
            "sheets_error": sheets_error,
            "message": message,
        }

    def should_send_daily_card(self) -> bool:
        state = self.state_store.load()
        today = datetime.now(ZoneInfo(self.settings.app_timezone)).date().isoformat()
        return state.get("daily_card_date") != today

    def _format_daily_card(self, snapshot: dict) -> str:
        liquidity = snapshot["dashboards"]["liquidity"]
        cycle = snapshot["dashboards"]["business-cycle"]
        liquidity_metrics = {metric["key"]: metric for metric in liquidity["metrics"]}
        cycle_metrics = {metric["key"]: metric for metric in cycle["metrics"]}
        lines = [
            f"MACRO DASHBOARD - {datetime.now(ZoneInfo(self.settings.display_timezone)).date().isoformat()}",
            "",
            f"LIQUIDITY: {liquidity['status']}",
            self._card_line(liquidity_metrics["m2"]),
            self._card_line(liquidity_metrics["rrp"]),
            self._card_line(liquidity_metrics["tga"]),
            self._card_line(liquidity_metrics["dxy"]),
            self._card_line(liquidity_metrics["global_m2_proxy"]),
            "",
            f"BUSINESS CYCLE: {cycle['status']}",
            self._card_line(cycle_metrics["ism_pmi"]),
            self._card_line(cycle_metrics["yield_curve"]),
            self._card_line(cycle_metrics["credit_spreads"]),
            self._card_line(cycle_metrics["jobless_claims"]),
            self._card_line(cycle_metrics["korean_exports"]),
            "",
            f"SIGNAL: {snapshot['signal']['label']}",
            snapshot["signal"]["action"],
        ]
        return "\n".join(lines)

    def _sheet_row(self, snapshot: dict) -> dict:
        liquidity = {metric["key"]: metric for metric in snapshot["dashboards"]["liquidity"]["metrics"]}
        cycle = {metric["key"]: metric for metric in snapshot["dashboards"]["business-cycle"]["metrics"]}
        return {
            "logged_at": snapshot["generated_at"],
            "signal": snapshot["signal"]["label"],
            "signal_summary": snapshot["signal"]["summary"],
            "signal_action": snapshot["signal"]["action"],
            "liquidity_status": snapshot["dashboards"]["liquidity"]["status"],
            "business_cycle_status": snapshot["dashboards"]["business-cycle"]["status"],
            "dxy": liquidity["dxy"]["display_value"],
            "m2_mom": liquidity["m2"]["secondary"],
            "rrp": liquidity["rrp"]["display_value"],
            "tga": liquidity["tga"]["display_value"],
            "fed_balance_sheet": liquidity["fed_balance_sheet"]["display_value"],
            "global_m2_proxy": liquidity["global_m2_proxy"]["display_value"],
            "ism_pmi": cycle["ism_pmi"]["display_value"],
            "yield_curve": cycle["yield_curve"]["display_value"],
            "credit_spreads_bps": cycle["credit_spreads"]["display_value"],
            "jobless_claims": cycle["jobless_claims"]["display_value"],
            "korean_exports_yoy": cycle["korean_exports"]["display_value"],
        }

    def _card_line(self, metric: dict) -> str:
        secondary = metric.get("secondary")
        if secondary:
            return f"- {metric['label']}: {metric['display_value']} ({secondary})"
        return f"- {metric['label']}: {metric['display_value']} ({metric['summary']})"

    def _crossed_above(self, previous: float | None, current: float | None, threshold: float) -> bool:
        if previous is None or current is None:
            return False
        return previous <= threshold < current

    def _crossed_below(self, previous: float | None, current: float | None, threshold: float) -> bool:
        if previous is None or current is None:
            return False
        return previous >= threshold > current

    def _crossed_threshold(self, previous: float | None, current: float | None, threshold: float) -> bool:
        if previous is None or current is None:
            return False
        return (previous <= threshold < current) or (previous >= threshold > current)

    def _safe_telegram_send(self, message: str) -> tuple[bool, str | None]:
        try:
            return self.telegram.send_message(message), None
        except Exception as exc:  # pragma: no cover - network failures are nondeterministic
            return False, str(exc)

    def _safe_sheet_append(self, row: dict) -> tuple[bool, str | None]:
        try:
            return self.sheets.append_snapshot(row), None
        except Exception as exc:  # pragma: no cover - external integration
            return False, str(exc)
