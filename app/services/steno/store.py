"""Persistence for Steno model portfolios.

Keeps a single JSON file with the latest committed portfolio + every historical
snapshot. Simpler than SQLite for our cadence (weekly-ish reports) and Dan can
inspect/edit by hand if needed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.services.steno.config import STENO_PORTFOLIO_PATH


logger = logging.getLogger(__name__)


def _empty_store() -> dict[str, Any]:
    return {"latest": None, "history": []}


def load_store() -> dict[str, Any]:
    if not STENO_PORTFOLIO_PATH.exists():
        return _empty_store()
    try:
        return json.loads(STENO_PORTFOLIO_PATH.read_text())
    except Exception as exc:
        logger.warning("Steno store unreadable, resetting: %s", exc)
        return _empty_store()


def save_store(store: dict[str, Any]) -> None:
    STENO_PORTFOLIO_PATH.write_text(json.dumps(store, indent=2))


def commit_portfolio(extracted: dict[str, Any], *, source_pdf: str | None = None) -> dict[str, Any]:
    """Add a newly-extracted portfolio to history and mark it as the latest.

    No-op if a portfolio with the same `report_date` is already latest.
    """
    store = load_store()
    latest = store.get("latest")
    if latest and latest.get("report_date") == extracted.get("report_date"):
        # Already committed; refresh source_pdf metadata only.
        latest["source_pdf"] = source_pdf or latest.get("source_pdf")
        latest["ingested_at"] = datetime.now(timezone.utc).isoformat()
        store["latest"] = latest
        save_store(store)
        return latest

    record = {
        **extracted,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "source_pdf": source_pdf,
    }
    store["history"].append(record)
    # Keep history capped at last 24 reports
    store["history"] = store["history"][-24:]
    store["latest"] = record
    save_store(store)
    logger.info("Committed Steno portfolio dated %s (%d positions)", record.get("report_date"), len(record.get("positions") or []))
    return record


def get_latest() -> dict[str, Any] | None:
    return load_store().get("latest")


def get_history() -> list[dict[str, Any]]:
    return load_store().get("history", [])


def previous_portfolio() -> dict[str, Any] | None:
    """The portfolio committed *before* the current latest, for change diffing."""
    history = get_history()
    if len(history) < 2:
        return None
    return history[-2]
