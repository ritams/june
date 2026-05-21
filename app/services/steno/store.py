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


DEFAULT_UNIVERSE_LOOKBACK_WEEKS = 8  # ~8 weeks covers ~6-12 reports across all doc types


def build_theme_universe(lookback_weeks: int = DEFAULT_UNIVERSE_LOOKBACK_WEEKS) -> dict[str, Any]:
    """Assemble Steno's "implied portfolio" across all Steno-Research reports
    in the last `lookback_weeks` weeks.

    Walks every doc type — Steno Signals (no positions, ignored here), Weekly
    Alpha Digest, What We Told Hedge Funds — and unions themes by name. For
    each theme, the most-recent non-zero weight wins; doc_type and source
    metadata are carried through so the UI can show "from WWTHF Mar 6".

    Quality filters:
      (a) Skip zero-weight entries — without a weight we can't compute a gap.
      (b) Skip implausible single-position weights (≥90%) — extraction
          artifacts from commentary-only reports.

    Returns:
        {
          "themes": [...],         # one per unique theme, sorted by recency
          "reports_used": [...],   # {date, doc_type, source_pdf} for each
          "lookback_weeks": int,
          "core_model_date": str,
          "doc_type_breakdown": {what_we_told_hedge_funds: 3, weekly_alpha_digest: 2, ...}
        }
    """
    from datetime import datetime, timedelta, timezone

    data = ensure_model_flags(load_store())
    history = data.get("history") or []
    cutoff = (datetime.now(timezone.utc) - timedelta(weeks=lookback_weeks)).date().isoformat()
    in_window = sorted(
        [h for h in history if (h.get("report_date") or "") >= cutoff],
        key=lambda h: h.get("report_date") or "",
        reverse=True,
    )

    core_report = next((h for h in in_window if h.get("is_model_portfolio")), None)

    # ── 1. Collect every raw position entry in the window ────────────────────
    raw: list[dict[str, Any]] = []
    for h in in_window:
        report_date = h.get("report_date") or ""
        doc_type = h.get("doc_type") or "unknown"
        for pos in (h.get("positions") or []):
            weight = pos.get("target_weight_pct") or 0
            if abs(weight) < 0.01:        # filter (a): no weight → can't compute a gap
                continue
            if abs(weight) >= 90:         # filter (b): hallucinated single-position weight
                continue
            name = (pos.get("name") or "").strip()
            if not name:
                continue
            raw.append({
                "name": name,
                "ticker": (pos.get("ticker") or None),
                "ticker_source": pos.get("ticker_source"),
                "ticker_confidence": pos.get("ticker_confidence"),
                "asset_class": pos.get("asset_class", "other"),
                "direction": pos.get("direction", "long"),
                "target_weight_pct": weight,
                "commentary": pos.get("commentary", ""),
                "change_vs_prior": pos.get("change_vs_prior"),
                "report_date": report_date,
                "doc_type": doc_type,
                "source_pdf": h.get("source_pdf"),
                # WAD entries are theme-level allocations; WWTHFTW entries are
                # individual-stock constituents of a theme.
                "is_theme_level": doc_type != "what_we_told_hedge_funds",
            })

    # ── 2. Canonicalize theme names via one Claude call ─────────────────────
    # Collapses "Decoupling" / "Decoupling Theme" / "Decoupling / Rare Earths"
    # into one theme, and rolls individual stocks (DroneShield → Military
    # Drones) up to their parent theme.
    from app.services.steno.ai_helpers import canonicalize_theme_names
    canon_map = canonicalize_theme_names([r["name"] for r in raw])

    core_canon: set[str] = set()
    if core_report:
        for p in core_report.get("positions") or []:
            n = (p.get("name") or "").strip()
            if n:
                core_canon.add(canon_map.get(n, n))

    # ── 3. Group raw entries by canonical theme ─────────────────────────────
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in raw:
        canon = canon_map.get(r["name"], r["name"])
        groups.setdefault(canon, []).append(r)

    themes: list[dict[str, Any]] = []
    for canon, entries in groups.items():
        entries.sort(key=lambda e: e["report_date"], reverse=True)  # newest first
        theme_level = [e for e in entries if e["is_theme_level"]]
        # Representative = most-recent theme-level entry (its weight is the
        # theme allocation). Fall back to most-recent entry of any kind.
        rep = theme_level[0] if theme_level else entries[0]
        # Constituents = individual-stock mentions (WWTHFTW), distinct by ticker,
        # most-recent weight per ticker.
        constituents: dict[str, dict[str, Any]] = {}
        for e in entries:
            if e["is_theme_level"]:
                continue
            tk = (e.get("ticker") or e["name"]).upper()
            if tk not in constituents:
                constituents[tk] = {
                    "name": e["name"],
                    "ticker": e.get("ticker"),
                    "target_weight_pct": e["target_weight_pct"],
                    "direction": e.get("direction"),
                    "commentary": e.get("commentary"),
                    "source_report_date": e["report_date"],
                    "source_doc_type": e["doc_type"],
                }
        themes.append({
            "name": canon,
            "ticker": rep.get("ticker"),
            "ticker_source": rep.get("ticker_source"),
            "ticker_confidence": rep.get("ticker_confidence"),
            "asset_class": rep.get("asset_class", "other"),
            "direction": rep.get("direction", "long"),
            "target_weight_pct": rep["target_weight_pct"],
            "commentary": rep.get("commentary", ""),
            "change_vs_prior": rep.get("change_vs_prior"),
            "source_report_date": rep["report_date"],
            "source_doc_type": rep["doc_type"],
            "source_pdf": rep.get("source_pdf"),
            "first_seen": min(e["report_date"] for e in entries),
            "last_seen": max(e["report_date"] for e in entries),
            "appearances": len(entries),
            "is_core": canon in core_canon,
            "is_tactical": canon not in core_canon,
            "constituents": list(constituents.values()),
        })

    themes.sort(key=lambda t: (not t["is_core"], -float(t["target_weight_pct"]), -t["appearances"]))

    breakdown: dict[str, int] = {}
    for h in in_window:
        dt = h.get("doc_type") or "unknown"
        breakdown[dt] = breakdown.get(dt, 0) + 1

    return {
        "themes": themes,
        "reports_used": [
            {"date": h.get("report_date"), "doc_type": h.get("doc_type"), "source_pdf": h.get("source_pdf")}
            for h in in_window if h.get("report_date")
        ],
        "lookback_weeks": lookback_weeks,
        "report_count": len(in_window),
        "doc_type_breakdown": breakdown,
        "core_model_date": (core_report or {}).get("report_date"),
        "core_model_source_pdf": (core_report or {}).get("source_pdf"),
    }


def universe_as_portfolio(
    lookback_weeks: int = DEFAULT_UNIVERSE_LOOKBACK_WEEKS,
) -> dict[str, Any] | None:
    """Wrap the theme universe in a portfolio-shaped dict the mirror engine can
    consume directly.

    The universe is already canonicalized — every theme is unique (duplicates
    like "Decoupling" / "Decoupling Theme" / "Decoupling / Rare Earths" are
    collapsed by the LLM canonicalization pass), and individual stocks from
    WWTHFTW are rolled up as `constituents` of their parent theme. So we just
    return the theme list straight through.
    """
    universe = build_theme_universe(lookback_weeks=lookback_weeks)
    themes = universe["themes"]
    if not themes:
        return None

    base = (load_store() or {}).get("latest") or {}
    core_count = sum(1 for t in themes if t.get("is_core"))
    return {
        "report_date": base.get("report_date"),
        "report_title": f"Rolling theme universe ({universe['report_count']} reports, {lookback_weeks}w)",
        "risk_tone": base.get("risk_tone"),
        "summary": base.get("summary"),
        "positions": themes,
        "cash_weight_pct": base.get("cash_weight_pct"),
        "macro_notes": base.get("macro_notes"),
        "_universe_meta": {
            "lookback_weeks": lookback_weeks,
            "report_count": universe["report_count"],
            "doc_type_breakdown": universe["doc_type_breakdown"],
            "reports_used": universe["reports_used"],
            "core_model_date": universe["core_model_date"],
            "theme_count": len(themes),
            "core_theme_count": core_count,
            "tactical_theme_count": len(themes) - core_count,
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


def commit_portfolio(extracted: dict[str, Any], *, source_pdf: str | None = None, doc_type: str | None = None) -> dict[str, Any]:
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
        "doc_type": doc_type or extracted.get("doc_type"),
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
