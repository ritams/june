"""MIT overlay — dynamic text for the Hermes CIO card.

Per build-28th-may.md §12, MIT (Julien Bittel's Macro Investing Tool) is a
*qualitative overlay only* — it never feeds the scoring engine. This module
fetches a fresh MIT summary from Perplexity once per week and caches it to
`runtime/mit_overlay.json` so the CIO card shows real current commentary
instead of the default placeholder.

Refresh cadence: 7 days (MIT publishes monthly on Real Vision; weekly refresh
catches updates without spamming Perplexity).

The cache file is the single source of truth at read time — if it's missing
or stale and Perplexity is unreachable, `current()` falls back to a sensible
default. The refresh runs async on a daily scheduler tick.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.perplexity import PerplexityClient


logger = logging.getLogger("mit_overlay")


_REFRESH_INTERVAL_SECONDS = 7 * 24 * 60 * 60  # weekly
_REFRESH_LOCK = threading.Lock()
_in_flight = False


DEFAULT_TEXT = (
    "Latest MIT view is broadly constructive on the business cycle, but liquidity "
    "remains volatile. Use as context, not as a model override."
)


def _cache_path(runtime_dir: Path) -> Path:
    return runtime_dir / "mit_overlay.json"


def load(runtime_dir: Path) -> dict[str, Any] | None:
    """Load the cached MIT overlay payload. Returns None if missing/unreadable."""
    p = _cache_path(runtime_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        logger.warning("MIT overlay cache unreadable at %s", p)
        return None


def save(runtime_dir: Path, payload: dict[str, Any]) -> None:
    """Persist the latest MIT overlay payload."""
    p = _cache_path(runtime_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True))


def needs_refresh(runtime_dir: Path) -> bool:
    payload = load(runtime_dir)
    if not payload:
        return True
    fetched_at = payload.get("fetched_at")
    if not fetched_at:
        return True
    try:
        ts = datetime.fromisoformat(fetched_at).timestamp()
    except ValueError:
        return True
    return (time.time() - ts) > _REFRESH_INTERVAL_SECONDS


def current(runtime_dir: Path) -> str:
    """Return the most recent overlay text — or the default if no cache exists.
    Reads from disk so the dashboard sees the latest fetch from a daemon thread
    without restart."""
    payload = load(runtime_dir)
    if not payload or not payload.get("summary"):
        return DEFAULT_TEXT
    summary = payload["summary"].strip()
    as_of = payload.get("as_of")
    if as_of:
        return f"{summary} (per Bittel {as_of})"
    return summary


def refresh(runtime_dir: Path, perplexity: PerplexityClient, force: bool = False) -> dict[str, Any] | None:
    """Synchronously refresh the MIT overlay if cache is stale or `force=True`.
    Returns the new payload, or None if Perplexity is unreachable / disabled."""
    global _in_flight
    if not force and not needs_refresh(runtime_dir):
        return load(runtime_dir)
    if not perplexity.enabled:
        logger.info("Perplexity disabled, MIT overlay refresh skipped")
        return None

    with _REFRESH_LOCK:
        if _in_flight:
            return load(runtime_dir)
        _in_flight = True
    try:
        try:
            fresh = perplexity.latest_mit_overlay()
        except Exception as exc:
            logger.warning("MIT overlay Perplexity fetch failed: %s", exc)
            return None
        payload = {
            "summary": fresh.get("summary"),
            "as_of": fresh.get("as_of"),
            "season": fresh.get("season"),
            "citations": fresh.get("citations", []),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save(runtime_dir, payload)
        logger.info("MIT overlay refreshed (as_of=%s)", payload.get("as_of"))
        return payload
    finally:
        with _REFRESH_LOCK:
            _in_flight = False


def refresh_async(runtime_dir: Path, perplexity: PerplexityClient, force: bool = False) -> None:
    """Kick off a background refresh — never blocks the caller."""
    thread = threading.Thread(
        target=refresh,
        args=(runtime_dir, perplexity),
        kwargs={"force": force},
        daemon=True,
        name="mit-overlay-refresh",
    )
    thread.start()
