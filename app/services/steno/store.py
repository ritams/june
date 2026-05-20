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


def ensure_model_flags(data: dict[str, Any]) -> dict[str, Any]:
    """Defensive backfill — older stores written before the validator existed
    may carry history entries without `is_model_portfolio`. Tag them on read
    so downstream filters (recent_updates, latest selection) work consistently.
    """
    history = data.get("history") or []
    changed = False
    for h in history:
        if "is_model_portfolio" not in h:
            is_model, reason = _looks_like_model_portfolio(h)
            h["is_model_portfolio"] = is_model
            h["is_commentary_only"] = not is_model
            h["commentary_reason"] = None if is_model else reason
            changed = True
    if changed:
        save_store(data)
    return data


DEFAULT_UNIVERSE_LOOKBACK = 6  # most-recent N Steno reports — ~10-week window at Steno's cadence


def build_theme_universe(lookback_reports: int = DEFAULT_UNIVERSE_LOOKBACK) -> dict[str, Any]:
    """Assemble Steno's "implied portfolio" across the most-recent N reports.

    Steno doesn't restate his complete book in every report — each PDF mentions
    a subset of his positions. Walking the recent history gives us the union
    of every theme he's said something about, with the most-recent valid
    weight per theme.

    Quality filters:
      (a) Skip zero-weight entries — Steno's pair-trade format ("USD/JPY Long")
          can extract at weight=0, which would pollute the universe with
          nominal themes. If we can't see a weight we don't pretend to know it.
      (c) Skip implausible single-position weights (≥90%) — these are
          extraction artifacts from commentary-only reports (e.g. May 18
          extracting "100% Energy Stocks" from a headline).

    Returns:
        {
          "themes": [...],           # one per unique theme, sorted by recency
          "reports_used": [...],     # dates of reports in the lookback window
          "lookback_count": int,
          "core_model_date": str,    # which report counts as "core" model
        }
    """
    data = ensure_model_flags(load_store())
    history = data.get("history") or []
    sorted_h = sorted(history, key=lambda h: h.get("report_date") or "", reverse=True)
    recent = sorted_h[:lookback_reports]

    # Find the most-recent full-model report — themes from there are flagged
    # is_core=True; everything else is tactical.
    core_report = next((h for h in sorted_h if h.get("is_model_portfolio")), None)
    core_names: set[str] = set()
    if core_report:
        core_names = {
            (p.get("name") or "").strip().lower()
            for p in (core_report.get("positions") or [])
        }

    themes: dict[str, dict[str, Any]] = {}
    for h in recent:
        report_date = h.get("report_date") or ""
        for pos in (h.get("positions") or []):
            weight = pos.get("target_weight_pct") or 0
            if abs(weight) < 0.01:        # filter (a)
                continue
            if abs(weight) >= 90:         # filter (c)
                continue
            name = (pos.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key not in themes:
                # First (newest) encounter — this report's weight/direction/ticker wins.
                themes[key] = {
                    "name": name,
                    "ticker": pos.get("ticker"),
                    "ticker_source": pos.get("ticker_source"),
                    "ticker_confidence": pos.get("ticker_confidence"),
                    "asset_class": pos.get("asset_class", "other"),
                    "direction": pos.get("direction", "long"),
                    "target_weight_pct": weight,
                    "commentary": pos.get("commentary", ""),
                    "change_vs_prior": pos.get("change_vs_prior"),
                    "source_report_date": report_date,
                    "first_seen": report_date,
                    "last_seen": report_date,
                    "appearances": 1,
                    "is_core": key in core_names,
                    "is_tactical": key not in core_names,
                }
            else:
                # Older occurrence — extend appearance counter + roll back first_seen.
                t = themes[key]
                t["appearances"] += 1
                if report_date and report_date < (t["first_seen"] or report_date):
                    t["first_seen"] = report_date

    # Sort: core themes first (with highest weight), then tactical by recency.
    ordered = sorted(
        themes.values(),
        key=lambda t: (
            not t["is_core"],                   # core first
            -float(t["target_weight_pct"]),     # then by weight desc
            -t["appearances"],                  # then by recurrence
        ),
    )

    return {
        "themes": ordered,
        "reports_used": [h.get("report_date") for h in recent if h.get("report_date")],
        "lookback_count": len(recent),
        "core_model_date": (core_report or {}).get("report_date"),
        "core_model_source_pdf": (core_report or {}).get("source_pdf"),
    }


def universe_as_portfolio(lookback_reports: int = DEFAULT_UNIVERSE_LOOKBACK) -> dict[str, Any] | None:
    """Wrap the theme universe in a portfolio-shaped dict that the mirror engine
    can consume directly. Returns None if no usable themes exist.
    """
    universe = build_theme_universe(lookback_reports=lookback_reports)
    themes = universe["themes"]
    if not themes:
        return None
    # Carry forward metadata from the most-recent full model so the UI keeps
    # showing tone / cash / macro_notes / etc.
    base = (load_store() or {}).get("latest") or {}
    return {
        "report_date": base.get("report_date"),  # date of the core model
        "report_title": f"Rolling theme universe ({universe['lookback_count']} reports)",
        "risk_tone": base.get("risk_tone"),
        "summary": base.get("summary"),
        "positions": themes,
        "cash_weight_pct": base.get("cash_weight_pct"),
        "macro_notes": base.get("macro_notes"),
        "_universe_meta": {
            "reports_used": universe["reports_used"],
            "core_model_date": universe["core_model_date"],
        },
    }


def recent_updates(limit: int = 5) -> list[dict[str, Any]]:
    """Return the most recent commentary / tactical-update reports — reports
    that were ingested but not promoted to `latest`. UI shows these as
    "what Steno has said SINCE the current model portfolio."
    """
    data = ensure_model_flags(load_store())
    latest = data.get("latest") or {}
    latest_date = latest.get("report_date") or ""
    latest_source = latest.get("source_pdf") or ""
    # Sort history by report_date DESC so the updates feed shows newest first
    # regardless of insertion order in the JSON file.
    sorted_history = sorted(
        data.get("history") or [],
        key=lambda h: h.get("report_date") or "",
        reverse=True,
    )
    updates: list[dict[str, Any]] = []
    for h in sorted_history:
        if h.get("is_model_portfolio"):
            continue
        if (h.get("source_pdf") or "") == latest_source and (h.get("report_date") or "") == latest_date:
            continue
        if (h.get("report_date") or "") < latest_date:
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
    is_model, reason = _looks_like_model_portfolio(extracted)
    record = {
        **extracted,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "source_pdf": source_pdf,
        "is_model_portfolio": is_model,
        "is_commentary_only": not is_model,
        "commentary_reason": None if is_model else reason,
    }
    # Dedupe history by (report_date, source_pdf) — overwrite the previous
    # entry for the same PDF rather than appending. Older code appended
    # blindly, which caused history bloat across re-ingests AND let stale
    # legacy entries from prior sessions linger past their replacements.
    history = [
        h for h in (store.get("history") or [])
        if not (
            (h.get("report_date") or "") == (record.get("report_date") or "")
            and (h.get("source_pdf") or "") == (source_pdf or "")
        )
    ]
    history.append(record)
    store["history"] = history[-24:]
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
