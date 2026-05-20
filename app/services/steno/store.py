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


FULL_MODEL_MIN_POSITIONS = 5  # below this we treat the report as a tactical update, not a full model


def _looks_like_model_portfolio(extracted: dict[str, Any]) -> tuple[bool, str]:
    """True if the extraction looks like a FULL model-portfolio rebalance.
    Reports with fewer than FULL_MODEL_MIN_POSITIONS are valid commentary or
    tactical updates and shouldn't overwrite the persistent model — those
    stay in history with `is_model_portfolio=False` and surface in the
    Updates feed.
    """
    positions = extracted.get("positions") or []
    if len(positions) < FULL_MODEL_MIN_POSITIONS:
        return False, f"only {len(positions)} positions — tactical update, not full model"
    weights = [abs(p.get("target_weight_pct") or 0) for p in positions]
    if any(w >= 90 for w in weights):
        return False, f"position with implausible weight {max(weights):.0f}%"
    total = sum(weights)
    if total < 20:
        return False, f"position weights sum to only {total:.0f}%"
    return True, ""


def recent_updates(limit: int = 5) -> list[dict[str, Any]]:
    """Return the most recent commentary / tactical-update reports — reports
    that were ingested but not promoted to `latest`. UI shows these as
    "what Steno has said SINCE the current model portfolio."
    """
    data = load_store()
    latest_date = (data.get("latest") or {}).get("report_date") or ""
    updates: list[dict[str, Any]] = []
    for h in reversed(data.get("history") or []):
        if h.get("is_model_portfolio"):
            continue
        if (h.get("report_date") or "") <= latest_date:
            continue
        updates.append({
            "report_date": h.get("report_date"),
            "report_title": h.get("report_title"),
            "risk_tone": h.get("risk_tone"),
            "summary": h.get("summary"),
            "macro_notes": h.get("macro_notes"),
            "positions": h.get("positions") or [],
            "commentary_reason": h.get("commentary_reason"),
            "source_pdf": h.get("source_pdf"),
            "ingested_at": h.get("ingested_at"),
        })
        if len(updates) >= limit:
            break
    return updates


def commit_portfolio(extracted: dict[str, Any], *, source_pdf: str | None = None) -> dict[str, Any]:
    """Add a newly-extracted portfolio to history. Promotes to `latest` only if
    it looks like a real model-portfolio rebalance — commentary-only Steno
    Signals (no portfolio table) stay in history but don't replace the active
    mirror.
    """
    store = load_store()
    latest = store.get("latest")
    if latest and latest.get("report_date") == extracted.get("report_date") and latest.get("source_pdf") == source_pdf:
        # Same report already committed; refresh metadata only.
        latest["source_pdf"] = source_pdf or latest.get("source_pdf")
        latest["ingested_at"] = datetime.now(timezone.utc).isoformat()
        store["latest"] = latest
        save_store(store)
        return latest

    is_model, reason = _looks_like_model_portfolio(extracted)
    record = {
        **extracted,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "source_pdf": source_pdf,
        "is_model_portfolio": is_model,
        "is_commentary_only": not is_model,
        "commentary_reason": None if is_model else reason,
    }
    store["history"].append(record)
    store["history"] = store["history"][-24:]
    # Only promote to latest when (a) it's a real model AND (b) it's newer than
    # the current latest. Compare ISO date strings safely.
    promoted = False
    if is_model:
        latest_date = (latest or {}).get("report_date") or ""
        new_date = record.get("report_date") or ""
        if not latest or new_date >= latest_date:
            store["latest"] = record
            promoted = True
    save_store(store)
    if promoted:
        logger.info("Promoted Steno portfolio dated %s to latest (%d positions)", record.get("report_date"), len(record.get("positions") or []))
    else:
        logger.info("Recorded Steno report dated %s in history but kept prior latest (%s: %s)",
                    record.get("report_date"), "commentary-only" if not is_model else "older", reason or "stale date")
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
