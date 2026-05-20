from __future__ import annotations

from pathlib import Path

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT_DIR, get_settings
from app.services.backtest import BacktestService
from app.services.correlations import FACTOR_KEYS, SCENARIO_PRESETS, CorrelationService
from app.services.dashboard import DashboardService
from app.services.monitor import MonitorService
from app.services.phase import PhaseService
from app.services.scenario_inputs import auto_fill_scenario
from app.services.sheets import SheetsClient
from app.services.state import StateStore
from app.services.telegram import TelegramClient


settings = get_settings()
dashboard_service = DashboardService(settings)
backtest_service = BacktestService(settings, fred=dashboard_service.fred)
correlation_service = CorrelationService(settings, backtest_service)
phase_service = PhaseService(backtest_service)
telegram_client = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
sheets_client = SheetsClient(
    spreadsheet_id=settings.google_sheet_id,
    credentials_file=settings.google_service_account_file,
    worksheet_title=settings.google_sheet_tab,
)
state_store = StateStore(settings.runtime_dir / "monitor_state.json")
monitor_service = MonitorService(
    settings,
    dashboard_service,
    telegram_client,
    sheets_client,
    state_store,
    correlation_service=correlation_service,
    phase_service=phase_service,
)

app = FastAPI(title="DJG Advisory", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")
scheduler = BackgroundScheduler(timezone=settings.app_timezone)


def _static_page(name: str) -> FileResponse:
    return FileResponse(Path(ROOT_DIR / "static" / name))


@app.on_event("startup")
def start_scheduler() -> None:
    backtest_service.ensure_cache_async()
    correlation_service.ensure_cache_async()
    if not settings.enable_scheduler:
        return
    if scheduler.running:
        return
    # 15-min crossing detection on the cached snapshot — cheap, no external calls beyond the cache.
    scheduler.add_job(monitor_service.run_alert_checks, "interval", minutes=settings.refresh_interval_minutes)
    # Daily full data pull — replaces 15-min force-refreshes (macro is daily/weekly anyway).
    scheduler.add_job(monitor_service.run_daily_refresh, "cron", hour=6, minute=30)
    hour, minute = settings.daily_card_time.split(":")
    scheduler.add_job(monitor_service.send_daily_card, "cron", hour=int(hour), minute=int(minute))
    scheduler.add_job(backtest_service.refresh_cache, "cron", day_of_week="sun", hour=6, minute=0)
    scheduler.add_job(correlation_service.refresh_cache, "cron", day=1, hour=6, minute=30)
    scheduler.add_job(monitor_service.run_regime_change_check, "cron", hour=hour_or_six(settings.daily_card_time), minute=0)

    # Steno PDF download + extraction — once a day at STENO_PIPELINE_TIME (HH:MM, default 07:00).
    # New Real Vision reports typically land overnight London time, so a morning pull catches them.
    # The same pipeline can be triggered ad-hoc via POST /api/steno/refresh from the dashboard.
    _schedule_steno_daily(scheduler)
    scheduler.start()


def _schedule_steno_daily(sched) -> None:
    import os
    from app.services.steno import pipeline as _pipeline
    raw = os.getenv("STENO_PIPELINE_TIME", "07:00").strip()
    try:
        h_str, m_str = raw.split(":")
        h, m = int(h_str), int(m_str)
    except Exception:
        h, m = 7, 0
    sched.add_job(_pipeline.run_pipeline, "cron", hour=h, minute=m, id="steno_daily", replace_existing=True)


def hour_or_six(card_time: str) -> int:
    try:
        return int(card_time.split(":")[0])
    except Exception:
        return 6


@app.on_event("shutdown")
def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(ROOT_DIR / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/dashboard", include_in_schema=False)
def dashboard_page() -> FileResponse:
    return _static_page("dashboard.html")


@app.get("/liquidity", include_in_schema=False)
def liquidity_page() -> FileResponse:
    return _static_page("liquidity.html")


@app.get("/business-cycle", include_in_schema=False)
def business_cycle_page() -> FileResponse:
    return _static_page("business-cycle.html")


@app.get("/steno", include_in_schema=False)
def steno_page() -> FileResponse:
    return _static_page("steno.html")


# ── Steno + IBKR mirror endpoints ──────────────────────────────────────────────

@app.get("/api/steno/portfolio")
def steno_portfolio() -> dict:
    from app.services.steno import store as _store
    latest = _store.get_latest()
    if not latest:
        return {"available": False, "reason": "No Steno portfolio ingested yet."}
    return {"available": True, "portfolio": latest, "history_count": len(_store.get_history())}


@app.post("/api/steno/refresh")
def steno_refresh(download_new: bool = True, force_reingest: bool = False) -> dict:
    """Kick off the Steno pipeline asynchronously. Returns immediately with the
    refresh-state snapshot. Poll /api/steno/refresh-status for progress."""
    from app.services.steno import pipeline as _pipeline
    return _pipeline.run_pipeline_async(download_new=download_new, force_reingest=force_reingest)


@app.get("/api/steno/refresh-status")
def steno_refresh_status() -> dict:
    from app.services.steno import pipeline as _pipeline
    return _pipeline.load_refresh_state()


@app.post("/api/steno/resolve-tickers")
def steno_resolve_tickers(force: bool = False) -> dict:
    """Run Perplexity ticker resolution on the latest committed portfolio.

    Use force=true to retry positions that previously came back as unresolved or
    to override a cached resolution.
    """
    from app.services.steno import pipeline as _pipeline
    return _pipeline.resolve_latest_tickers(force=force)


@app.post("/api/steno/ingest-cached")
def steno_ingest_cached(force: bool = False) -> dict:
    """Ingest already-downloaded PDFs without hitting Real Vision. Useful for testing
    and for situations where the PDFs were brought in via scp/sftp instead of scraped."""
    from app.services.steno import pipeline as _pipeline
    return _pipeline.run_pipeline(download_new=False, force_reingest=force)


@app.get("/api/ibkr/portfolio")
def ibkr_portfolio() -> dict:
    from app.services.ibkr import store as _store
    snapshot = _store.load_snapshot()
    age = _store.snapshot_age_seconds()
    return {
        "available": snapshot is not None,
        "age_seconds": age,
        "snapshot": snapshot,
    }


@app.post("/api/ibkr/refresh")
def ibkr_refresh() -> dict:
    from app.services.ibkr import flex as _flex
    from app.services.ibkr import store as _store
    try:
        snap = _flex.fetch_snapshot()
    except _flex.IBKRFlexError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    data = snap.to_dict()
    _store.save_snapshot(data)
    return {"ok": True, "fetched_at": data["fetched_at"], "positions": len(data["positions"])}


@app.get("/api/mirror")
def mirror_payload(tolerance: float | None = None) -> dict:
    from app.services.mirror import build_mirror, DEFAULT_TOLERANCE_PCT
    tol = tolerance if tolerance is not None else DEFAULT_TOLERANCE_PCT
    return build_mirror(tolerance_pct=tol)


@app.post("/api/mirror/override")
def mirror_set_override(ticker: str, bucket: str | None = None, clear: bool = False) -> dict:
    """Pin a Dan ticker to a specific Steno bucket (or mark off-thesis explicitly).

    Pass `clear=true` to remove the override and fall back to auto-classification.
    Pass `bucket=` empty / null to explicitly mark the ticker off-thesis.
    """
    from app.services.steno import bucket_classifier as _bc
    if clear:
        overrides = _bc.clear_override(ticker)
    else:
        overrides = _bc.set_override(ticker, bucket or None)
    return {"ok": True, "overrides": overrides}


@app.get("/api/mirror/overrides")
def mirror_list_overrides() -> dict:
    from app.services.steno import bucket_classifier as _bc
    return {"overrides": _bc.load_overrides()}


@app.get("/api/health")
def health() -> dict:
    backtest_status = backtest_service.cache_status()
    return {
        "ok": True,
        "telegram_enabled": telegram_client.enabled,
        "google_sheets_enabled": sheets_client.enabled,
        "perplexity_enabled": bool(settings.perplexity_api_key),
        "scheduler_enabled": settings.enable_scheduler,
        "backtest_cache_available": backtest_status.available,
        "backtest_last_calculated": backtest_status.last_calculated,
        "backtest_cache_stale": backtest_status.stale,
    }


@app.get("/api/snapshot")
def snapshot(force: bool = False) -> dict:
    data = dashboard_service.get_snapshot(force=force)
    data["backtest"] = {
        "last_calculated": backtest_service.cache_status().last_calculated,
        "stale": backtest_service.cache_status().stale,
    }
    try:
        data["phase"] = phase_service.get(force=force).to_dict()
    except Exception as exc:  # pragma: no cover — surface error without breaking snapshot
        data["phase"] = {"key": "unknown", "label": "Unknown", "blurb": str(exc)[:160]}
    return data


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
        "playbook": backtest_service.dashboard_playbook(slug, data),
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


@app.post("/api/actions/recalculate-playbook")
def recalculate_playbook() -> dict:
    payload = backtest_service.refresh_cache()
    return {
        "ok": True,
        "last_calculated": payload["last_calculated"],
        "signals": sorted(payload["signals"].keys()),
    }


@app.post("/api/actions/recalculate-correlations")
def recalculate_correlations() -> dict:
    payload = correlation_service.refresh_cache()
    return {
        "ok": True,
        "last_calculated": payload["last_calculated"],
        "factor_count": len(payload.get("factors", {})),
        "asset_count": len(payload.get("assets", {})),
    }


_HISTORY_SERIES = {
    "m2": "M2SL",
    "ism": "IPMAN",
    "cpi_yoy": "CPIAUCSL",
    "yield_curve": "T10Y2Y",
    "credit_spreads": "BAMLH0A0HYM2",
    "two_year": "DGS2",
    "oil": "DCOILWTICO",
}


@app.get("/api/history/{key}")
def history(key: str, months: int = 60) -> dict:
    series_id = _HISTORY_SERIES.get(key)
    if not series_id:
        raise HTTPException(status_code=404, detail="Unknown series")
    obs = dashboard_service.fred.observations(series_id, limit=None, sort_order="asc", observation_start="2010-01-01")
    points = [{"date": o.date, "value": o.value} for o in obs[-months:]]
    if key == "cpi_yoy" and len(obs) >= 13:
        all_values = [{"date": o.date, "value": o.value} for o in obs]
        yoy: list[dict] = []
        for i in range(12, len(all_values)):
            base = all_values[i - 12]["value"]
            if base:
                yoy.append({"date": all_values[i]["date"], "value": (all_values[i]["value"] / base - 1) * 100})
        points = yoy[-months:]
    return {"key": key, "series_id": series_id, "points": points}


@app.get("/api/scenario/presets")
def scenario_presets() -> dict:
    return {
        "factor_keys": FACTOR_KEYS,
        "presets": SCENARIO_PRESETS,
    }


@app.get("/api/scenario")
def scenario(
    risk_on_off: float | None = None,
    growth: float | None = None,
    inflation: float | None = None,
    short_rates: float | None = None,
    liquidity: float | None = None,
    dollar: float | None = None,
    oil: float | None = None,
    auto: bool = False,
) -> dict:
    if auto:
        snapshot = dashboard_service.get_snapshot(force=False)
        try:
            scenario_input = auto_fill_scenario(snapshot, backtest_service)
        except Exception:
            # Upstream FRED/yfinance flake — fall back to neutral so the dashboard still renders.
            scenario_input = {factor: 0.0 for factor in FACTOR_KEYS}
    else:
        raw = {
            "risk_on_off": risk_on_off,
            "growth": growth,
            "inflation": inflation,
            "short_rates": short_rates,
            "liquidity": liquidity,
            "dollar": dollar,
            "oil": oil,
        }
        scenario_input = {k: (0.0 if v is None else max(-1.0, min(1.0, v))) for k, v in raw.items()}
    result = correlation_service.rank_scenario(scenario_input)
    return result


def run() -> None:
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
