from __future__ import annotations

from pathlib import Path

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT_DIR, get_settings
from app.services.dashboard import DashboardService
from app.services.monitor import MonitorService
from app.services.sheets import SheetsClient
from app.services.state import StateStore
from app.services.telegram import TelegramClient


settings = get_settings()
dashboard_service = DashboardService(settings)
telegram_client = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
sheets_client = SheetsClient(
    spreadsheet_id=settings.google_sheet_id,
    credentials_file=settings.google_service_account_file,
    worksheet_title=settings.google_sheet_tab,
)
state_store = StateStore(settings.runtime_dir / "monitor_state.json")
monitor_service = MonitorService(settings, dashboard_service, telegram_client, sheets_client, state_store)

app = FastAPI(title="June Dashboard", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")
scheduler = BackgroundScheduler(timezone=settings.app_timezone)


def _static_page(name: str) -> FileResponse:
    return FileResponse(Path(ROOT_DIR / "static" / name))


@app.on_event("startup")
def start_scheduler() -> None:
    if not settings.enable_scheduler:
        return
    if scheduler.running:
        return
    scheduler.add_job(monitor_service.run_alert_checks, "interval", minutes=settings.refresh_interval_minutes)
    hour, minute = settings.daily_card_time.split(":")
    scheduler.add_job(monitor_service.send_daily_card, "cron", hour=int(hour), minute=int(minute))
    scheduler.start()


@app.on_event("shutdown")
def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/liquidity")


@app.get("/liquidity", include_in_schema=False)
def liquidity_page() -> FileResponse:
    return _static_page("liquidity.html")


@app.get("/business-cycle", include_in_schema=False)
def business_cycle_page() -> FileResponse:
    return _static_page("business-cycle.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "telegram_enabled": telegram_client.enabled,
        "google_sheets_enabled": sheets_client.enabled,
        "scheduler_enabled": settings.enable_scheduler,
    }


@app.get("/api/snapshot")
def snapshot(force: bool = False) -> dict:
    return dashboard_service.get_snapshot(force=force)


@app.get("/api/dashboard/{slug}")
def dashboard(slug: str, force: bool = False) -> dict:
    data = dashboard_service.get_snapshot(force=force)
    dashboard_data = data["dashboards"].get(slug)
    if dashboard_data is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return {
        "generated_at": data["generated_at"],
        "signal": data["signal"],
        "dashboard": dashboard_data,
        "integrations": data["integrations"],
    }


@app.post("/api/actions/refresh")
def refresh(send_alerts: bool = False) -> dict:
    data = dashboard_service.get_snapshot(force=True)
    alerts: list[str] = []
    if send_alerts:
        alerts = monitor_service.run_alert_checks()
    return {"snapshot": data, "alerts": alerts}


@app.post("/api/actions/send-daily-card")
def send_daily_card() -> dict:
    return monitor_service.send_daily_card()


def run() -> None:
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
