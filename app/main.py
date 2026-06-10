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
from app.services import cio_message as _cio_message
from app.services import framework_portfolio as _fp
from app.services import hermes_state as _hermes_state
from app.services import risk_budget as _risk_budget
from app.services import whatif as _whatif


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

# ── Build the MCP server BEFORE FastAPI so we can pass its lifespan ─────────
# Hermes Agent connects to http://127.0.0.1:8000/mcp. The MCP server reuses the
# already-initialized service singletons rather than HTTP-round-tripping.
# fastmcp REQUIRES its lifespan attached to the parent ASGI app.
_mcp_lifespan = None
_mcp_sub_app = None
try:
    from app.mcp_server import build_mcp_server as _build_mcp_server

    _mcp_instance = _build_mcp_server(
        dashboard_service=dashboard_service,
        backtest_service=backtest_service,
        correlation_service=correlation_service,
        phase_service=phase_service,
        state_store=state_store,
        settings=settings,
        telegram_client=telegram_client,
        hermes_state_module=_hermes_state,
        risk_budget_module=_risk_budget,
        whatif_module=_whatif,
        framework_portfolio_module=_fp,
        cio_message_module=_cio_message,
        auto_fill_scenario=auto_fill_scenario,
        current_state_fn=lambda: _current_hermes_state(),
    )
    # fastmcp >=2.0 exposes an HTTP ASGI app via .http_app().
    # We mount it at /mcp on the parent app, so the sub-app's internal path is "/".
    _mcp_sub_app = _mcp_instance.http_app(path="/") if hasattr(_mcp_instance, "http_app") else _mcp_instance.sse_app(path="/")
    _mcp_lifespan = getattr(_mcp_sub_app, "lifespan", None) or getattr(_mcp_instance, "lifespan", None)
except Exception:
    import logging as _logging
    _logging.getLogger("mcp").exception("Failed to build MCP app — dashboard will run without MCP")

app = FastAPI(title="DJG Advisory", version="0.2.0", lifespan=_mcp_lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")
if _mcp_sub_app is not None:
    app.mount("/mcp", _mcp_sub_app)
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
    # Weekly Hermes CIO Telegram message — Monday 07:00 London (build-28th-may.md §11).
    scheduler.add_job(_send_hermes_weekly, "cron", day_of_week="mon", hour=7, minute=0, id="hermes_weekly", replace_existing=True)

    # Steno PDF download + extraction — once a day at STENO_PIPELINE_TIME (HH:MM, default 07:00).
    # New Real Vision reports typically land overnight London time, so a morning pull catches them.
    # The same pipeline can be triggered ad-hoc via POST /api/steno/refresh from the dashboard.
    _schedule_steno_daily(scheduler)
    # IBKR snapshot — refresh once a day at 06:00 London so the mirror has a fresh
    # position view when Steno's 07:00 pull lands. Override via IBKR_REFRESH_TIME=HH:MM.
    _schedule_ibkr_daily(scheduler)
    scheduler.start()

    # First-run bootstrap — if the local Steno cache is empty (or under-covered)
    # AND the next scheduled pull is more than an hour away, kick off the pipeline
    # right now so a fresh deploy doesn't show an empty mirror until tomorrow.
    _maybe_bootstrap_steno()


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


def _maybe_bootstrap_steno() -> None:
    """First-run bootstrap. If the Steno cache is under-covered (we have fewer
    than the expected min reports for the last 12 weeks) AND the daily cron is
    not about to fire imminently, kick off a one-shot async refresh on startup
    so a fresh deploy (or one that's been offline) doesn't sit with stale data
    until tomorrow morning."""
    import logging as _logging
    log = _logging.getLogger("steno.bootstrap")
    try:
        from app.services.steno import pipeline as _pipeline
        cov = _pipeline._coverage_summary()
        if len(cov["have_in_window"]) >= cov["expected_min"]:
            log.info("Steno coverage OK on startup (%d/%d weeks); skipping bootstrap",
                     len(cov["have_in_window"]), cov["target_weeks"])
            return
        # Also bail if a refresh is already running (e.g. process restarted mid-run)
        state = _pipeline.load_refresh_state()
        if state.get("status") == "running":
            log.info("Refresh already running on startup, skipping bootstrap")
            return
        log.info("Steno cache under-covered (%d/%d weeks); kicking off bootstrap refresh",
                 len(cov["have_in_window"]), cov["expected_min"])
        # Detached thread — the pipeline itself has per-step timeouts (Playwright
        # 60s for the feed walk, 600s read timeout per Vision batch). If anything
        # truly hangs, the parent process can be restarted safely; this thread
        # is daemonized so it never blocks shutdown.
        _pipeline.run_pipeline_async(download_new=True, force_reingest=False)
    except Exception as exc:
        log.warning("Bootstrap refresh failed to start: %s", exc)


def _schedule_ibkr_daily(sched) -> None:
    """Pull a fresh IBKR Flex snapshot once a day. Failures are logged but don't
    crash the scheduler — Flex has occasional 503s and the last cached snapshot
    stays usable."""
    import os
    import logging as _logging
    from app.services.ibkr import flex as _flex
    from app.services.ibkr import store as _ibkr_store
    raw = os.getenv("IBKR_REFRESH_TIME", "06:00").strip()
    try:
        h_str, m_str = raw.split(":")
        h, m = int(h_str), int(m_str)
    except Exception:
        h, m = 6, 0

    def _job():
        log = _logging.getLogger("ibkr.scheduler")
        try:
            snap = _flex.fetch_snapshot()
            _ibkr_store.save_snapshot(snap.to_dict())
            log.info("Daily IBKR snapshot saved (%d positions)", len(snap.positions))
        except Exception as exc:
            log.warning("Daily IBKR snapshot failed (last cached snapshot remains): %s", exc)

    sched.add_job(_job, "cron", hour=h, minute=m, id="ibkr_daily", replace_existing=True)


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


@app.get("/transcripts/bittel-mit-april-2026", include_in_schema=False)
def bittel_transcript_page() -> FileResponse:
    return _static_page("bittel-transcript.html")


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
    return {
        "available": True,
        "portfolio": latest,
        "history_count": len(_store.get_history()),
        "updates": _store.recent_updates(limit=5),
    }


@app.get("/api/steno/universe")
def steno_universe(lookback_weeks: int = 8) -> dict:
    """The rolling theme universe — union of themes across all Steno-Research
    docs (Steno Signals macro / Weekly Alpha Digest / What We Told Hedge Funds)
    in the last `lookback_weeks` weeks, with most-recent-valid-weight per theme."""
    from app.services.steno import store as _store
    return _store.build_theme_universe(lookback_weeks=lookback_weeks)


@app.get("/api/steno/updates")
def steno_updates(limit: int = 5) -> dict:
    """Recent Steno reports that came after the current model portfolio —
    commentary / tactical pieces that don't replace the model but reflect
    Steno's current thinking."""
    from app.services.steno import store as _store
    return {"updates": _store.recent_updates(limit=limit), "current_model_date": (_store.get_latest() or {}).get("report_date")}


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


@app.post("/api/steno/feed-preview")
def steno_feed_preview() -> dict:
    """Dry-run scrape: walk Real Vision's feed and return every Steno report
    URL visible (with parsed dates) WITHOUT downloading PDFs or hitting Claude.
    Lets you confirm how far back the feed actually exposes reports before
    committing to a full ingest run."""
    from app.services.steno import auth as _auth
    from app.services.steno import downloader as _dl
    try:
        state_path = _auth.ensure_authenticated(headless=True)
        urls = _dl.list_steno_signals_in_feed(state_path)
    except _dl.SessionExpiredError:
        state_path = _auth.ensure_authenticated(force=True, headless=True)
        urls = _dl.list_steno_signals_in_feed(state_path)
    dated = [u for u in urls if u.get("date")]
    dated.sort(key=lambda u: u["date"], reverse=True)
    return {
        "count": len(urls),
        "with_date": len(dated),
        "urls": dated[:40],
        "oldest": dated[-1]["date"] if dated else None,
        "newest": dated[0]["date"] if dated else None,
    }


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


# ── Hermes CIO endpoints (tools the future GPT 5.5 agent will call) ──────────
#
# These match the 9 functions in build-28th-may.md §10. The agent layer is
# deferred; for now the endpoints return deterministic state derived from the
# existing dashboard data.


_HERMES_SNAPSHOT_TIMEOUT_SECONDS = 8.0


def _current_hermes_state() -> _hermes_state.HermesState:
    """Assemble the CIO View. Resilient to a cold dashboard snapshot: if the
    snapshot path hangs (FRED + yfinance can take a minute on a fresh process)
    or raises, fall back to "Unknown" for the liquidity/cycle status fields and
    still surface Risk Budget + Season + summary, which are derived independently.

    The snapshot call is run in a worker thread with an 8-second deadline. A
    long-running snapshot won't block the CIO card render — important because
    the card is the dashboard's headline element.
    """
    import concurrent.futures

    def _bounded(fn, *args, default=None, timeout: float = _HERMES_SNAPSHOT_TIMEOUT_SECONDS):
        """Run `fn(*args)` with a hard deadline. The worker thread is not awaited
        on timeout — important because ThreadPoolExecutor's context-manager __exit__
        otherwise blocks until the worker finishes."""
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(fn, *args)
            return future.result(timeout=timeout)
        except Exception:
            return default
        finally:
            # cancel_futures=True only matters for queued (not-yet-started) tasks;
            # the started worker continues in the background but we no longer wait.
            pool.shutdown(wait=False, cancel_futures=True)

    snapshot = _bounded(dashboard_service.get_snapshot, False, default=None)
    if snapshot:
        liquidity_state = snapshot["dashboards"]["liquidity"]["status"]
        cycle_state = snapshot["dashboards"]["business-cycle"]["status"]
    else:
        liquidity_state = "Unknown"
        cycle_state = "Unknown"

    scenario = _bounded(
        auto_fill_scenario, {}, backtest_service,
        default={k: 0.0 for k in ("risk_on_off", "growth", "inflation", "short_rates", "liquidity", "dollar", "oil")},
    )

    season_reading = _bounded(phase_service.get, False, default=None)
    if season_reading:
        season_label = season_reading.label
        season_detail = season_reading.to_dict()
    else:
        season_label = "Unknown"
        season_detail = {}

    return _hermes_state.build(
        scenario=scenario,
        season=season_label,
        liquidity_state=liquidity_state,
        cycle_state=cycle_state,
        timezone=settings.app_timezone,
        season_detail=season_detail,
    )


@app.get("/api/hermes/state")
def hermes_state() -> dict:
    """get_current_dashboard_state() — full CIO View payload for the frontend card."""
    return _current_hermes_state().to_dict()


@app.get("/api/hermes/risk-budget")
def hermes_risk_budget() -> dict:
    """get_current_risk_budget() — just the score + stance + components."""
    snapshot = dashboard_service.get_snapshot(force=False)
    scenario = auto_fill_scenario(snapshot, backtest_service)
    return _risk_budget.compute(scenario).to_dict()


@app.get("/api/hermes/season")
def hermes_season() -> dict:
    """get_current_macro_season() — Bittel 4-season classification via PhaseService."""
    reading = phase_service.get(force=False)
    return reading.to_dict()


@app.get("/api/hermes/liquidity-state")
def hermes_liquidity_state() -> dict:
    """get_liquidity_state() — current dashboard liquidity status + metrics."""
    snapshot = dashboard_service.get_snapshot(force=False)
    return {
        "status": snapshot["dashboards"]["liquidity"]["status"],
        "summary": snapshot["dashboards"]["liquidity"].get("summary"),
        "metrics": snapshot["dashboards"]["liquidity"]["metrics"],
    }


@app.get("/api/hermes/cycle-state")
def hermes_cycle_state() -> dict:
    """get_cycle_state() — current dashboard business-cycle status + metrics."""
    snapshot = dashboard_service.get_snapshot(force=False)
    return {
        "status": snapshot["dashboards"]["business-cycle"]["status"],
        "summary": snapshot["dashboards"]["business-cycle"].get("summary"),
        "metrics": snapshot["dashboards"]["business-cycle"]["metrics"],
    }


@app.get("/api/hermes/allocation")
def hermes_allocation() -> dict:
    """get_current_allocation() — softmax+caps allocation engine on the live scenario."""
    snapshot = dashboard_service.get_snapshot(force=False)
    scenario = auto_fill_scenario(snapshot, backtest_service)
    ranking = correlation_service.rank_scenario(scenario)
    return {
        "available": ranking.get("available", False),
        "allocation": ranking.get("allocation"),
        "scenario": scenario,
    }


@app.get("/api/hermes/whatif/options")
def hermes_whatif_options() -> dict:
    return _whatif.list_options()


@app.post("/api/hermes/whatif")
def hermes_whatif(
    amount: float = 100000.0,
    asset_key: str | None = None,
    basket_key: str | None = None,
    start_date: str = "2020-01-01",
    end_date: str | None = None,
    mode: str = "buy_and_hold",
) -> dict:
    """run_what_if_outcome() — buy-and-hold a single asset OR a preset basket OR
    the Framework Portfolio (basket_key='FRAMEWORK_PORTFOLIO')."""
    if basket_key and basket_key.upper() == "FRAMEWORK_PORTFOLIO":
        return hermes_framework_portfolio(amount=amount, start_date=start_date, end_date=end_date)
    try:
        result = _whatif.run(
            amount=amount,
            asset_key=asset_key,
            basket_key=basket_key,
            start_date=start_date,
            end_date=end_date,
            fetcher=backtest_service._download_close,
            mode=mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result.to_dict()


@app.post("/api/hermes/framework-portfolio")
def hermes_framework_portfolio(
    amount: float = 100000.0,
    start_date: str = "2010-01-01",
    end_date: str | None = None,
) -> dict:
    """run_framework_portfolio_outcome() — backtest the dashboard's own allocation
    framework (Risk Budget → band → basket, monthly rebalance)."""
    try:
        result = _fp.run_backtest(
            initial_amount=amount,
            start_date=start_date,
            end_date=end_date,
            factor_series=backtest_service.factor_series(),
            price_fetcher=backtest_service._download_close,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result.to_dict()


@app.get("/api/hermes/weekly-message")
def hermes_weekly_message_preview() -> dict:
    """generate_weekly_cio_message() — preview without persisting/sending."""
    curr = _current_hermes_state()
    state = state_store.load()
    prev = state.get("hermes_weekly")
    return {"message": _cio_message.render(curr, prev), "state": curr.to_dict()}


@app.post("/api/actions/send-weekly-message")
def hermes_send_weekly_message() -> dict:
    """send_weekly_telegram_message() — render, persist this week's snapshot for
    next-week diffing, and send to Telegram."""
    return _send_hermes_weekly()


def _send_hermes_weekly() -> dict:
    curr = _current_hermes_state()
    message = _cio_message.generate_and_persist(curr, state_store)
    sent, err = (False, None)
    try:
        sent = telegram_client.send_message(message)
    except Exception as exc:  # pragma: no cover — network
        err = str(exc)
    return {"telegram_sent": sent, "telegram_error": err, "message": message}


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
