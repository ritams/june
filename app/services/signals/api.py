"""Technical Signals Layer — HTTP API.

Implements the endpoints listed in `docs/technical-signals-layer-scope.md` §10:

    ├─ /api/watchlist            (scores, history, sources)
    ├─ /api/lookup/{symbol}      (on-demand scoring)
    ├─ /api/intake/*             (queue: list / approve / dismiss / paste)
    └─ alert dispatcher ──► Hermes webhook (outbound only)

Routes are namespaced under `/api/signals/*` rather than the bare `/api/*` the
scope doc sketches, because this module is embedded in an existing dashboard
(§1: "Module for existing investment dashboard") whose `/api/*` namespace
already holds the Steno, IBKR, mirror and Hermes routes. Namespacing keeps the
signals layer self-contained and its routes greppable.

Not yet implemented here (later chunks of the build, not gaps in the design):
`/api/signals/intake/*` (§7 source intake) and the alert dispatcher (§8). The
engine, scoring and watchlist surface below is what §6 and §9 need.

Conventions follow the rest of app/main.py: plain dicts in and out, query
parameters rather than pydantic bodies, service modules imported at function
scope where the import is heavy.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.services.signals import engine, store
from app.services.signals.config import HISTORY_BARS, SOURCE_TAGS, TRADINGVIEW_URL_TEMPLATE
from app.services.signals.providers import (
    ProviderError,
    classify_asset,
    get_provider,
    get_search_provider,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/signals", tags=["signals"])


# ── Watchlist (§9 ranked table) ──────────────────────────────────────────────


@router.get("/watchlist")
def get_watchlist() -> dict:
    """The §9 ranked watchlist table.

    Returns every active name with its latest score, band, provenance tag,
    90-day sparkline and days-since-last-signal — ranked by entry score
    descending. Names not yet scored appear last rather than being hidden, so a
    name added today is visibly present.
    """
    rows = store.watchlist_with_scores()
    return {
        "count": len(rows),
        "watchlist": rows,
        # §9 header: "last-run stamp". Derived from the newest scored bar rather
        # than a separate run record, so it can never disagree with the data.
        "last_scored_bar": max((r["bar_date"] for r in rows if r.get("bar_date")), default=None),
        "stale_count": sum(1 for r in rows if r.get("stale")),
    }


@router.post("/watchlist")
def post_watchlist(
    symbol: str,
    asset_class: str | None = None,
    source_tag: str = "OWN",
    notes: str | None = None,
    held: bool = False,
) -> dict:
    """Add a name to the watchlist — §6.4 "Add to watchlist (with source)".

    §7 requires provenance on every entry, so `source_tag` must be one of
    STENO / RV / COWAN / OWN. An unknown tag is a 400 rather than a silent
    coercion to OWN: mislabelling would misattribute a name to a research
    provider that never recommended it.

    `asset_class` is inferred from the symbol's shape when omitted, rather than
    defaulting to "equity" — otherwise `GC=F` and `BTC-USD` land on the
    watchlist labelled as equities and §9's asset-class filter silently lies.
    Pass it explicitly to override.
    """
    try:
        row = store.add_to_watchlist(
            symbol,
            asset_class=asset_class or classify_asset(symbol),
            source_tag=source_tag,
            notes=notes,
            held=held,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "entry": row}


@router.delete("/watchlist/{symbol}")
def delete_watchlist(symbol: str) -> dict:
    """Soft-delete a name, preserving its score and signal history."""
    removed = store.remove_from_watchlist(symbol)
    if not removed:
        raise HTTPException(status_code=404, detail=f"{symbol.upper()} is not on the active watchlist")
    return {"ok": True, "symbol": symbol.upper()}


@router.post("/watchlist/{symbol}/held")
def post_held(symbol: str, held: bool = True) -> dict:
    """Toggle held — §5: "Exit alerts only on names toggled held"."""
    updated = store.set_held(symbol, held)
    if not updated:
        raise HTTPException(status_code=404, detail=f"{symbol.upper()} is not on the active watchlist")
    return {"ok": True, "symbol": symbol.upper(), "held": held}


@router.get("/sources")
def get_sources() -> dict:
    """The §7 provenance tags, for the §9 source filter chip."""
    return {"sources": list(SOURCE_TAGS)}


# ── Lookup (§6) ──────────────────────────────────────────────────────────────


@router.get("/lookup/{symbol}")
def get_lookup(symbol: str, bars: int = HISTORY_BARS, fresh: bool = False) -> dict:
    """On-demand scoring for any symbol — §6.

    Runs the identical scoring code as the nightly job (§6.2) and never triggers
    alerts (§6.4). Returns the full §6.3 result card payload: entry score and
    band, exit score, the "Firing now" driver list, the component breakdown, and
    the raw indicator/TD state for the mini chart.

    `fresh=true` bypasses the §3.3 session cache and forces a live fetch. The
    widget's "Live re-score" button passes it — a button labelled *live* that
    quietly returned a cached score would be lying.

    Status codes:
      404 — symbol unknown to the provider, or too new to score
      503 — provider reachable but failing (§3.3 data-API outage)
    """
    try:
        evaluation = engine.lookup(symbol, bars=bars, use_cache=not fresh)
    except engine.InsufficientHistory as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        # yfinance cannot distinguish "unknown symbol" from "rate-limited"
        # (see providers.py). 503 is the honest status: we do not know that the
        # symbol is bad, only that we could not fetch it.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Lookup failed for %s", symbol)
        raise HTTPException(status_code=500, detail=f"Lookup failed: {exc}") from exc
    return evaluation.to_dict()


@router.get("/search")
def get_search(q: str, limit: int = 10) -> dict:
    """Symbol search for the §6.1 search bar.

    **Known limitation:** the yfinance provider has no supported search
    endpoint, so this validates an exact symbol rather than offering prefix
    autocomplete (see providers.py). `autocomplete` in the response tells the
    frontend which behaviour it is getting, so the UI can present a
    "press enter to look up" affordance instead of a suggestion dropdown.
    Switching to Twelve Data (§3.3) turns real autocomplete on without a
    frontend change beyond honouring this flag.
    """
    provider = get_search_provider()
    try:
        results = provider.search(q, limit)
    except Exception as exc:
        logger.warning("Symbol search failed for %r: %s", q, exc)
        results = []
    return {
        "query": q,
        "results": [r.to_dict() for r in results],
        "autocomplete": provider.name != "yfinance",
        "provider": provider.name,
    }


# ── History (§9 row expand) ──────────────────────────────────────────────────


@router.get("/detail/{symbol}")
def get_detail(symbol: str) -> dict:
    """The §9 row-expand card, built from the **persisted** score.

    Distinct from /lookup/{symbol} on purpose, and the distinction matters:

      * `/detail` returns the stored nightly score — the same numbers the ranked
        table row shows, with its stored component breakdown and drivers.
      * `/lookup` re-fetches bars and recomputes live (§6).

    Expanding a row must not silently recompute. Bars, volume and the current
    partial session all move during the day, so a live recompute can differ from
    the persisted score by a few points — and showing "Entry 42" in the row with
    "Entry 45" in its own expanded card is the kind of inconsistency that makes a
    user distrust every other number on the page.

    Returns 404 when the symbol has never been scored; the frontend then falls
    back to offering a live lookup, which is the honest option at that point.
    """
    latest = store.latest_score(symbol)
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail=f"{symbol.upper()} has no persisted score yet — run scoring or use /lookup for a live score",
        )
    return {
        "symbol": symbol.upper(),
        "source": "persisted",
        "score": latest,
        "signals": store.signal_history(symbol),
        "scores": store.score_history(symbol, 90),
        "tradingview_url": TRADINGVIEW_URL_TEMPLATE.format(symbol=symbol.upper()),
    }


@router.get("/bars/{symbol}")
def get_bars(symbol: str, limit: int = 120) -> dict:
    """Price bars with the §4 moving-average series — feeds §6.3's mini chart.

    §6.3/§9 ask for a "mini chart with 21E/50/200". The lookup and detail
    payloads carry only the *latest* value of each average, which is enough for
    the numeric readout but cannot draw a line. This returns the aligned series.

    Reads persisted bars (the nightly run's record) rather than re-fetching, so
    opening a chart costs no API call and cannot disagree with the score beside
    it. Falls back to a live fetch when the symbol has never been scored, which
    is the case for a §6 lookup on a name that is not on the watchlist.

    The averages are computed over the **full** loaded history and only then
    sliced to `limit`, so the 200 SMA is correct at the left edge of the window
    instead of being null for the first 200 of the bars shown.
    """
    from app.services.signals.config import EMA_FAST, SMA_MID, SMA_SLOW
    from app.services.signals.indicators import ema, sma

    sym = symbol.strip().upper()
    series = store.load_bars(sym, HISTORY_BARS)
    source = "persisted"

    if not series.bars:
        # Never scored — fetch live. engine.lookup() caches the bars it fetches
        # (persist_bars defaults True), so we re-read them from the DB rather
        # than reaching into the Evaluation for them.
        try:
            engine.lookup(sym)
        except engine.InsufficientHistory as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        series = store.load_bars(sym, HISTORY_BARS)
        source = "live"
        if not series.bars:
            raise HTTPException(status_code=404, detail=f"No bars available for {sym}")

    closes = series.closes
    ema21 = ema(closes, EMA_FAST)
    sma50 = sma(closes, SMA_MID)
    sma200 = sma(closes, SMA_SLOW)

    # Slice AFTER computing, so slow averages are populated across the window.
    n = len(closes)
    start = max(0, n - max(1, limit))
    return {
        "symbol": sym,
        "source": source,
        "count": n - start,
        "bars": [
            {
                "date": series.bars[i].bar_date.isoformat(),
                "close": closes[i],
                "high": series.bars[i].high,
                "low": series.bars[i].low,
                "ema21": ema21[i],
                "sma50": sma50[i],
                "sma200": sma200[i],
            }
            for i in range(start, n)
        ],
    }


@router.get("/history/{symbol}")
def get_history(symbol: str, limit: int = 90) -> dict:
    """Score and signal history for one name — §9 row-expand card.

    `scores` drives the 90-day sparkline; `signals` is the "signal history" list.
    """
    return {
        "symbol": symbol.upper(),
        "scores": store.score_history(symbol, limit),
        "signals": store.signal_history(symbol),
        "latest": store.latest_score(symbol),
    }


# ── Run control (§10 nightly job) ────────────────────────────────────────────


# A synchronous HTTP request that outlives the reverse proxy's timeout returns a
# 504 to the user while the work continues invisibly server-side — the worst of
# both outcomes. nginx defaults to 60s; we refuse well before that.
MAX_SYNC_RUN_SECONDS = 45


@router.post("/run")
def post_run(symbol: str | None = None, force: bool = False) -> dict:
    """Trigger the §10 daily run now — the whole watchlist, or one symbol.

    Synchronous, unlike the Steno pipeline's async refresh: on yfinance a full
    watchlist run is a few seconds, so a background thread and poll endpoint
    would buy nothing.

    **Twelve Data changes that.** Its basic plan allows 8 requests/minute
    (verified live), so the provider throttles and a 50-name run takes ~6
    minutes — well past nginx's 60s proxy timeout. Rather than hand back a 504
    while the run continues invisibly, we estimate the duration and refuse,
    pointing at the two things that do work: the nightly cron (a background job,
    where the wait is free) or a symbol-scoped run.

    `force=true` overrides the refusal for callers that can wait — a CLI or a
    request with a raised proxy timeout.
    """
    symbols = [symbol] if symbol else None

    estimate = _estimated_run_seconds(symbols)
    if estimate > MAX_SYNC_RUN_SECONDS and not force:
        raise HTTPException(
            status_code=413,
            detail=(
                f"A full run over {get_provider().name} would take about {estimate // 60}m{estimate % 60}s "
                f"because of provider rate limits, past the {MAX_SYNC_RUN_SECONDS}s synchronous budget. "
                "Let the nightly job handle it, pass ?symbol=TICKER to score one name, "
                "or ?force=true if your client can wait."
            ),
        )

    result = engine.run_daily(symbols)
    return {"ok": True, "result": result.to_dict()}


def _estimated_run_seconds(symbols: list[str] | None) -> int:
    """Rough wall-clock for a run, given the provider's rate limit.

    Deliberately crude — it only needs to separate "a few seconds" from "several
    minutes" so the refusal above fires at the right times.
    """
    from app.services.signals.config import OHLCV_PROVIDER

    count = len(symbols) if symbols else len(store.list_watchlist())
    if count == 0:
        return 0
    if OHLCV_PROVIDER != "twelvedata":
        return count            # yfinance: ~1s/symbol, no hard throttle

    from app.services.signals.providers import TD_REQUESTS_PER_MINUTE

    # Requests beyond the first minute's allowance each wait for a slot.
    if count <= TD_REQUESTS_PER_MINUTE:
        return count
    return int((count / max(1, TD_REQUESTS_PER_MINUTE)) * 60)


# ── Intake (§7) ──────────────────────────────────────────────────────────────


@router.post("/intake/email")
async def post_intake_email(request: Request) -> dict:
    """Inbound email from the provider — §7.2, the only externally-posted endpoint.

    §10: "The only inbound endpoint is the email provider's signed POST (verify
    provider signature)."

    Reads the raw body before parsing so the signature is verified over the exact
    bytes that were signed — re-serialising parsed JSON would change whitespace
    and key order and break verification.

    Returns 401 on a bad or missing signature, and 200 with a status for every
    other outcome (queued / dropped / skipped). A non-portfolio email is a
    successful no-op per §7.3's "dropped silently", not an error — returning 4xx
    would make the provider retry and eventually disable the webhook.
    """
    from app.services.signals import intake as intake_mod

    raw = await request.body()
    signature = (
        request.headers.get("X-Signals-Signature")
        or request.headers.get("X-Mailgun-Signature")
        or request.headers.get("X-Postmark-Signature")
    )
    timestamp = request.headers.get("X-Signals-Timestamp") or request.headers.get("X-Mailgun-Timestamp")

    try:
        intake_mod.verify_signature(raw, signature, timestamp)
    except intake_mod.IntakeAuthError as exc:
        logger.warning("Rejected intake webhook: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        payload = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body is not valid JSON") from exc

    try:
        return intake_mod.ingest_email(payload)
    except RuntimeError as exc:
        # Extraction failed (missing API key, model error). 502 so the provider
        # retries — the email is real and we genuinely failed to process it.
        logger.exception("Intake extraction failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/intake/paste")
async def post_intake_paste(request: Request, source_tag: str = "OWN") -> dict:
    """§7.5 manual fallback — "a 'Paste a note' box on the same strip".

    §7.5: "paste text or upload a PDF from any source (Cowan videos, anything),
    same extraction, same queue. Bulletproof when an email format changes or a
    source isn't emailed."

    Session-authenticated like the rest of the dashboard API, so no signature is
    required here.
    """
    from app.services.signals import intake as intake_mod

    body = await request.json()
    text = (body or {}).get("text") or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="`text` is required")

    try:
        return intake_mod.ingest_text(text, source_tag=source_tag, subject=(body or {}).get("subject"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("Paste extraction failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/intake/upload")
async def post_intake_upload(file: UploadFile = File(...), source_tag: str = "OWN") -> dict:
    """§7.5: "paste text or **upload a PDF** from any source ... same extraction, same queue".

    Session-authenticated like the rest of the dashboard API. Unlike the inbound
    email webhook, this path has a human choosing the file, so linked/attached
    PDFs that the webhook declines to fetch (SSRF) can be handled here.
    """
    from app.services.signals import intake as intake_mod

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        text, method = intake_mod.extract_pdf_text(data, name=file.filename or "upload.pdf")
    except intake_mod.PdfTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        result = intake_mod.ingest_text(text, source_tag=source_tag, subject=file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("Upload extraction failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {**result, "extraction_method": method}


@router.get("/intake/pending")
def get_intake_pending() -> dict:
    """The §7.4 review queue plus the §7 source-silence notice.

    Both in one response because the §9 intake strip renders them together, and
    a second round-trip for a notice that is usually empty is wasted.
    """
    from app.services.signals import intake as intake_mod

    items = intake_mod.pending()
    return {
        "count": len(items),
        "pending": items,
        "name_count": sum(len(i["actions"]) for i in items),
        "silent_sources": intake_mod.silent_sources(),
    }


@router.post("/intake/{intake_id}/approve")
def post_intake_approve(intake_id: int, tickers: str | None = None) -> dict:
    """§7.4 "[Approve all]" and "[Review]" — the only path from intake to the watchlist.

    Omit `tickers` to approve everything (Approve all). Pass a comma-separated
    list to approve a subset after reviewing (Review).
    """
    from app.services.signals import intake as intake_mod

    selected = [t for t in (tickers or "").split(",") if t.strip()] if tickers else None
    try:
        return {"ok": True, **intake_mod.approve(intake_id, selected)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/intake/{intake_id}/dismiss")
def post_intake_dismiss(intake_id: int) -> dict:
    """§7.4 Dismiss — resolves the item without touching the watchlist."""
    from app.services.signals import intake as intake_mod

    try:
        return {"ok": True, **intake_mod.dismiss(intake_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/intake/digest")
def get_intake_digest() -> dict:
    """Preview the §7.6 Sunday digest without sending it."""
    from app.services.signals import intake as intake_mod

    message = intake_mod.build_intake_digest()
    return {"available": message is not None, "message": message}


# ── Alerts (§8) ──────────────────────────────────────────────────────────────


@router.get("/alerts")
def get_alerts(limit: int = 50) -> dict:
    """Alert history with delivery state — §8.6 "delivery logged".

    Surfaces `delivered` and `last_error` so a misconfigured or failing sink is
    visible on the dashboard rather than only in the logs. An alerting system
    that silently stops delivering is worse than one that never worked, because
    silence reads as "nothing is happening".
    """
    rows = store.recent_alerts(limit)
    return {
        "count": len(rows),
        "alerts": rows,
        "undelivered": sum(1 for r in rows if not r["delivered"] and r["attempts"] > 0),
    }


@router.get("/digest")
def get_digest() -> dict:
    """Preview the §8.3 digest without sending it.

    Mirrors the /api/hermes/weekly-message pattern already in app/main.py:
    render, return, do not dispatch or persist.
    """
    from app.services.signals import alerts as alerts_mod

    message = alerts_mod.build_digest()
    return {"available": message is not None, "message": message}


@router.post("/alerts/evaluate")
def post_evaluate_alerts(dry_run: bool = True) -> dict:
    """Evaluate the §8 rules against the latest scores.

    `dry_run=true` (the default) returns what *would* be sent without sending or
    logging anything — the safe way to inspect the rules. It is the default
    deliberately: a POST that notifies Fred as a side effect of someone poking
    the endpoint would violate §1's "no repeat pings" in the most annoying
    possible way.

    `dry_run=false` dispatches for real, which also consumes each ticker's
    §8.2 daily slot.
    """
    from app.services.signals import alerts as alerts_mod

    candidates = alerts_mod.evaluate()
    if dry_run:
        return {
            "dry_run": True,
            "would_send": [a.to_dict() for a in candidates if a.realtime],
            "would_hold_for_digest": [a.to_dict() for a in candidates if not a.realtime],
        }

    from app.main import telegram_client  # late import: avoids a circular import at module load

    result = alerts_mod.dispatch(candidates, telegram_client=telegram_client)
    return {"dry_run": False, **result}


@router.post("/watchlist/{symbol}/td-optout")
def post_td_optout(symbol: str, optout: bool = True) -> dict:
    """§8.4: "per-ticker opt-out" from the TD completion override."""
    updated = store.set_td_optout(symbol, optout)
    if not updated:
        raise HTTPException(status_code=404, detail=f"{symbol.upper()} is not on the active watchlist")
    return {"ok": True, "symbol": symbol.upper(), "td_optout": optout}


@router.post("/backtest")
def post_backtest(
    symbol: str | None = None,
    bars: int = HISTORY_BARS,
    horizon: int = 20,
    step: int = 1,
) -> dict:
    """§5 Phase 4 replay — hit-rates per band per asset class.

    Synchronous and potentially slow: replaying one 300-bar symbol is ~240 full
    indicator passes. A watchlist-wide run at `step=1` takes minutes. Pass
    `symbol` to scope it, or raise `step` to trade resolution for wall-clock.

    Note the default `bars` is 300 (§3.3's live setting), which is thin for
    tuning — pass a larger value for a real run. `sample_warnings` in the
    response flags bands whose sample is too small to act on.
    """
    from app.services.signals import backtest as backtest_mod

    return backtest_mod.run(
        [symbol] if symbol else None,
        bars=bars,
        horizon=horizon,
        step=max(1, step),
    )


@router.get("/status")
def get_status() -> dict:
    """Engine health for the §9 header — provider, coverage, staleness.

    Deliberately reports `stale_symbols` and `unscored` separately: §3.3's
    staleness contract is about data age, while an unscored name is one the run
    could not evaluate at all. Collapsing them would hide which failure occurred.
    """
    from app.services.signals.config import (
        OHLCV_PROVIDER,
        SIGNALS_DB_PATH,
        TD_COUNTS_VERIFIED,
        TD_OVERRIDE_REALTIME_SCOPE,
    )

    search_provider = get_search_provider()
    rows = store.watchlist_with_scores()
    return {
        "provider": OHLCV_PROVIDER,
        # §6.1: search may use a different provider from bars — see SEARCH_PROVIDER
        # in config.py for why the hybrid is the default.
        "search_provider": search_provider.name,
        "autocomplete": search_provider.name != "yfinance",
        # §3.1 sign-off state — the widget badges TD counts as unverified while
        # this is false. See docs/td-sequential-verification.md.
        "td_counts_verified": TD_COUNTS_VERIFIED,
        "td_realtime_scope": TD_OVERRIDE_REALTIME_SCOPE,
        "db_path": str(SIGNALS_DB_PATH),
        "watchlist_count": len(rows),
        "scored_count": sum(1 for r in rows if r.get("entry_score") is not None),
        "unscored": [r["symbol"] for r in rows if r.get("entry_score") is None],
        "stale_symbols": [r["symbol"] for r in rows if r.get("stale")],
        "last_scored_bar": max((r["bar_date"] for r in rows if r.get("bar_date")), default=None),
    }
