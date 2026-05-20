"""Cache the last IBKR Flex snapshot to disk so the dashboard renders quickly."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR


logger = logging.getLogger(__name__)

IBKR_SNAPSHOT_PATH = ROOT_DIR / "runtime" / "ibkr" / "snapshot.json"
IBKR_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)


def save_snapshot(snapshot: dict[str, Any]) -> None:
    IBKR_SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2))


def load_snapshot() -> dict[str, Any] | None:
    if not IBKR_SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(IBKR_SNAPSHOT_PATH.read_text())
    except Exception as exc:
        logger.warning("IBKR snapshot unreadable: %s", exc)
        return None


def snapshot_age_seconds() -> float | None:
    if not IBKR_SNAPSHOT_PATH.exists():
        return None
    return (datetime.now(timezone.utc).timestamp() - IBKR_SNAPSHOT_PATH.stat().st_mtime)
