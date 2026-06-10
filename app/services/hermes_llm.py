"""Bridge from the FastAPI backend to the locally-installed Hermes agent.

When Hermes is on the same host (i.e. dan-mac, where we deploy), we can shell
out to `hermes -z "<prompt>"` to get LLM-generated prose. This is used to
*enrich* the deterministic templates in `hermes_state.build_summary()` and
`cio_message._what_changed()` — never to replace the underlying state, just
the human-facing text.

Design:
  * Bounded by a hard timeout (default 12s). On timeout we return None and the
    caller uses its deterministic fallback.
  * In-memory cache with 15-minute TTL keyed on the prompt. Stops the CIO card
    paying the cost on every page load.
  * Disabled by default if the `hermes` binary isn't on PATH — production
    setup ships with it, but tests and local dev shouldn't depend on it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Any


_HERMES_BIN = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
_TIMEOUT_SECONDS = float(os.getenv("HERMES_LLM_TIMEOUT", "12"))
_CACHE_TTL_SECONDS = float(os.getenv("HERMES_LLM_CACHE_TTL", str(15 * 60)))
_DISABLED = os.getenv("HERMES_LLM_DISABLED", "").lower() in ("1", "true", "yes")

_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def available() -> bool:
    """True if shelling out to the hermes CLI is viable on this host."""
    if _DISABLED:
        return False
    return bool(_HERMES_BIN and os.path.exists(_HERMES_BIN))


def _cache_get(key: str) -> str | None:
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if now - ts > _CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        return value


def _cache_set(key: str, value: str) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)


def ask(prompt: str, *, timeout: float | None = None) -> str | None:
    """Run a zero-shot Hermes query and return the response text.

    Returns None on any failure (binary missing, timeout, non-zero exit, empty
    output). Callers MUST have a deterministic fallback path.
    """
    if not available():
        return None

    cached = _cache_get(prompt)
    if cached is not None:
        return cached

    try:
        result = subprocess.run(
            [_HERMES_BIN, "-z", prompt],
            capture_output=True,
            text=True,
            timeout=timeout if timeout is not None else _TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    _cache_set(prompt, out)
    return out


# ── Prompt builders used by the two consumers ──────────────────────────────

def summary_prompt(state_dict: dict[str, Any]) -> str:
    """Prompt for the CIO View card summary (replaces _STANCE_SUMMARY)."""
    rb = state_dict.get("risk_budget_detail", {})
    inputs = rb.get("inputs", {})
    return (
        "You are Hermes, CIO for DJG Advisory.\n"
        "Write a 2-3 sentence summary of the current stance. Direct, no fluff, "
        "no preamble. Mention 1-2 specific factor values from the inputs below "
        "to ground it. Do NOT call any tools — the state is provided.\n\n"
        f"Stance: {state_dict.get('stance')} (Risk Budget {state_dict.get('risk_budget')}/100)\n"
        f"Deploy/Cash: {state_dict.get('deploy_pct')}% / {state_dict.get('cash_pct')}%\n"
        f"Macro Season: {state_dict.get('macro_season')}\n"
        f"Liquidity: {state_dict.get('liquidity_state')}\n"
        f"Cycle: {state_dict.get('cycle_state')}\n"
        f"Confidence: {state_dict.get('confidence')}\n"
        f"Factor z-scores: {inputs}\n"
    )


def what_changed_prompt(curr: dict[str, Any], prev: dict[str, Any] | None) -> str:
    """Prompt for the weekly message 'what changed' paragraph."""
    return (
        "You are Hermes writing the weekly CIO note. Write 2-4 short bullets "
        "(start each with '- ') describing what changed week over week. "
        "Mention actual numbers. If no regime-level changes, say 'Trend holds.' "
        "Do NOT call tools — the snapshots are below.\n\n"
        f"This week: {curr}\n\nLast week: {prev}\n"
    )
