"""Steno end-to-end pipeline: ensure auth → download new PDFs → render → transcribe →
extract portfolio JSON → commit to store.

Designed to be safe to call repeatedly. Skips already-ingested PDFs.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.steno import auth, downloader, portfolio_extractor, renderer, store, ticker_resolver, transcriber
from app.services.steno.config import STENO_DOWNLOADS_DIR, STENO_PROCESSED_PATH, STENO_ROOT


logger = logging.getLogger(__name__)

REFRESH_STATE_PATH = STENO_ROOT / "refresh_state.json"
HISTORY_WEEKS_TARGET = 12
_refresh_lock = threading.Lock()


def _save_refresh_state(state: dict[str, Any]) -> None:
    REFRESH_STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def load_refresh_state() -> dict[str, Any]:
    if not REFRESH_STATE_PATH.exists():
        return {"status": "idle"}
    try:
        return json.loads(REFRESH_STATE_PATH.read_text())
    except Exception:
        return {"status": "idle"}


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _pdf_date(pdf: Path) -> datetime | None:
    """Try filename first (`steno-signals-2026-03-04.pdf`), then verbose forms."""
    m = _DATE_RE.search(pdf.stem)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None
    m2 = re.search(r"([A-Za-z]+)[-_](\d{1,2})[-_](\d{4})", pdf.stem)
    if m2:
        try:
            return datetime.strptime(f"{m2.group(1)} {m2.group(2)} {m2.group(3)}", "%B %d %Y").replace(tzinfo=timezone.utc)
        except Exception:
            try:
                return datetime.strptime(f"{m2.group(1)} {m2.group(2)} {m2.group(3)}", "%b %d %Y").replace(tzinfo=timezone.utc)
            except Exception:
                return None
    return None


def _coverage_summary(target_weeks: int = HISTORY_WEEKS_TARGET) -> dict[str, Any]:
    """Inspect downloaded PDFs and report coverage vs target_weeks history window."""
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=target_weeks)
    pdfs = sorted(STENO_DOWNLOADS_DIR.glob("steno-signals-*.pdf"))
    dates_in_window: list[str] = []
    for pdf in pdfs:
        d = _pdf_date(pdf)
        if d and d >= cutoff:
            dates_in_window.append(d.date().isoformat())
    return {
        "target_weeks": target_weeks,
        "expected_min": max(1, target_weeks - 2),  # tolerate a couple of missing weeks (no report, holidays)
        "have_in_window": sorted(set(dates_in_window)),
        "total_on_disk": len(pdfs),
    }


def _load_processed() -> set[str]:
    if not STENO_PROCESSED_PATH.exists():
        return set()
    try:
        data = json.loads(STENO_PROCESSED_PATH.read_text())
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def _save_processed(items: set[str]) -> None:
    STENO_PROCESSED_PATH.write_text(json.dumps(sorted(items), indent=2))


def ingest_pdf(pdf_path: Path, *, force: bool = False) -> dict[str, Any]:
    """Render → transcribe → extract → commit a single PDF. Returns the committed record."""
    stem = pdf_path.stem
    processed = _load_processed()
    if not force and stem in processed:
        latest = store.get_latest()
        if latest and latest.get("source_pdf") == pdf_path.name:
            return latest

    images, _ = renderer.render_pdf_pages(pdf_path)
    transcript = transcriber.transcribe_pages(images, pdf_stem=stem)
    portfolio = portfolio_extractor.extract_portfolio(transcript, pdf_stem=stem)
    # Fill in ambiguous theme tickers via Perplexity before committing.
    ticker_resolver.enrich_portfolio_tickers(portfolio)
    record = store.commit_portfolio(portfolio, source_pdf=pdf_path.name)

    processed.add(stem)
    _save_processed(processed)
    return record


def resolve_latest_tickers(*, force: bool = False) -> dict[str, Any]:
    """Re-run Perplexity ticker resolution on the latest committed portfolio.

    Useful when (a) the portfolio was committed before the resolver existed, or
    (b) Dan wants to retry a previously-unresolved theme with force=True.
    """
    latest = store.get_latest()
    if not latest:
        return {"ok": False, "reason": "No Steno portfolio committed yet."}
    before = sum(1 for p in latest.get("positions", []) if p.get("ticker"))
    ticker_resolver.enrich_portfolio_tickers(latest, force=force)
    after = sum(1 for p in latest.get("positions", []) if p.get("ticker"))
    full = store.load_store()
    full["latest"] = latest
    if full.get("history"):
        full["history"][-1] = latest
    store.save_store(full)
    return {"ok": True, "resolved_delta": after - before, "total_with_ticker": after, "total_positions": len(latest.get("positions", []))}


def _progress_writer(summary: dict[str, Any]):
    def write(stage: str, **extra) -> None:
        summary["stage"] = stage
        summary["updated_at"] = datetime.now(timezone.utc).isoformat()
        summary.update(extra)
        _save_refresh_state(summary)
    return write


def run_pipeline(
    *,
    download_new: bool = True,
    force_reingest: bool = False,
    history_weeks: int = HISTORY_WEEKS_TARGET,
) -> dict[str, Any]:
    """One pipeline run. Ensures the last `history_weeks` of Steno reports are
    on disk (best-effort against the Real Vision feed), then ingests every PDF
    that isn't already in processed.json. Writes incremental progress to
    refresh_state.json so the UI can poll.
    """
    summary: dict[str, Any] = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stage": "starting",
        "history_weeks_target": history_weeks,
        "coverage_before": _coverage_summary(history_weeks),
        "downloaded": [],
        "ingested": [],
        "errors": [],
        "ingest_total": 0,
        "ingest_done": 0,
    }
    progress = _progress_writer(summary)
    progress("starting")

    if download_new:
        progress("scanning-realvision")
        try:
            state_path = auth.ensure_authenticated(headless=True)
            new_pdfs = downloader.download_steno_signals(state_path)
            summary["downloaded"] = [p.name for p in new_pdfs]
        except downloader.SessionExpiredError as exc:
            logger.warning("Session expired, forcing re-auth: %s", exc)
            progress("re-authenticating")
            try:
                state_path = auth.ensure_authenticated(force=True, headless=True)
                new_pdfs = downloader.download_steno_signals(state_path)
                summary["downloaded"] = [p.name for p in new_pdfs]
            except Exception as exc2:
                summary["errors"].append(f"download (after reauth): {exc2}")
        except Exception as exc:
            summary["errors"].append(f"download: {exc}")

    progress("checking-coverage")
    summary["coverage_after"] = _coverage_summary(history_weeks)
    have = len(summary["coverage_after"]["have_in_window"])
    expected_min = summary["coverage_after"]["expected_min"]
    if have < expected_min:
        # The downloader walked the full feed already; if we still came up short,
        # Real Vision's UI probably doesn't expose reports that far back. Flag it
        # rather than retry — that would only loop.
        summary["errors"].append(
            f"Only {have} report(s) in the last {history_weeks} weeks (target ≥{expected_min}). "
            "Real Vision feed may not expose older reports."
        )

    # Ingest every PDF on disk (chronological), filtered by processed.json.
    pdfs = sorted(STENO_DOWNLOADS_DIR.glob("steno-signals-*.pdf"), key=lambda p: _pdf_date(p) or datetime.min.replace(tzinfo=timezone.utc))
    if not pdfs:
        pdfs = sorted(STENO_DOWNLOADS_DIR.glob("*.pdf"))
    summary["ingest_total"] = len(pdfs)
    progress("ingesting", ingest_total=len(pdfs))

    for pdf in pdfs:
        try:
            progress("ingesting", current=pdf.name)
            record = ingest_pdf(pdf, force=force_reingest)
            summary["ingested"].append({"pdf": pdf.name, "date": record.get("report_date")})
        except Exception as exc:
            summary["errors"].append(f"ingest {pdf.name}: {exc}")
        summary["ingest_done"] += 1
        progress("ingesting", current=pdf.name, ingest_done=summary["ingest_done"])

    summary["status"] = "complete"
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    progress("done")
    return summary


def run_pipeline_async(*, download_new: bool = True, force_reingest: bool = False) -> dict[str, Any]:
    """Kick off run_pipeline in a daemon thread and return immediately. Concurrent
    invocations are coalesced — if a refresh is already running, this returns
    the existing state without spawning a duplicate."""
    if not _refresh_lock.acquire(blocking=False):
        state = load_refresh_state()
        return {"started": False, "reason": "refresh already running", "state": state}

    def _runner():
        try:
            run_pipeline(download_new=download_new, force_reingest=force_reingest)
        except Exception as exc:
            logger.exception("Steno pipeline crashed")
            state = load_refresh_state()
            state["status"] = "error"
            state["errors"] = (state.get("errors") or []) + [f"unhandled: {exc}"]
            state["finished_at"] = datetime.now(timezone.utc).isoformat()
            _save_refresh_state(state)
        finally:
            _refresh_lock.release()

    thread = threading.Thread(target=_runner, name="steno-refresh", daemon=True)
    thread.start()
    return {"started": True, "state": load_refresh_state()}
