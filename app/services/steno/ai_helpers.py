"""Shared AI helpers for the Steno mirror — Perplexity for fresh facts,
Anthropic for thematic reasoning.

The split is by strength:
  • Perplexity (sonar-pro): live web search → "what is this ticker today?",
    "what new ETFs launched for theme X?", "what's its AUM/liquidity?"
  • Anthropic (Claude Sonnet 4-6): better reasoning on long-context inputs →
    "given this ticker's profile and Steno's thesis commentary, does it fit
    the bucket?", with structured tool_use output and confidence scores.

The ticker_profiles cache is bucket-independent (sector / business doesn't
change because Steno's bucket list changed), so we reuse it heavily.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from app.services.steno.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_API_URL,
    ANTHROPIC_VERSION,
    CLAUDE_MODEL,
    STENO_ROOT,
)


logger = logging.getLogger(__name__)

PROFILES_PATH = STENO_ROOT / "ticker_profiles.json"
PROFILE_MAX_AGE_DAYS = 14  # sector / business descriptions don't move week-to-week

THEME_CANON_PATH = STENO_ROOT / "theme_canonical.json"

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = os.getenv("PERPLEXITY_MODEL", "sonar-pro")


# ── shared JSON I/O ──────────────────────────────────────────────────────────

def _load_json(path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _extract_json(content: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ── Perplexity helper ────────────────────────────────────────────────────────

def perplexity_json(prompt: str, *, system: str | None = None, timeout: float = 45.0) -> dict[str, Any] | None:
    """One-shot Perplexity call expecting JSON back. Returns the parsed dict (with
    `citations` merged in), or None on any failure. Caller decides retry policy.
    """
    api_key = (os.getenv("PERPLEXITY_API_KEY") or "").strip()
    if not api_key:
        logger.warning("PERPLEXITY_API_KEY missing; skipping live lookup")
        return None
    try:
        resp = httpx.post(
            PERPLEXITY_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": PERPLEXITY_MODEL,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system or "You return JSON only, no markdown."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        logger.warning("Perplexity call failed: %s", exc)
        return None
    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    payload = _extract_json(content)
    if payload is None:
        logger.warning("Perplexity returned no JSON: %s", content[:160])
        return None
    citations = body.get("citations") or []
    if citations and "citations" not in payload:
        payload["citations"] = citations
    return payload


# ── Anthropic helper (tool_use for structured output) ────────────────────────

def anthropic_tool(
    *,
    tool_name: str,
    tool_schema: dict[str, Any],
    system: str,
    user_message: str,
    max_tokens: int = 1024,
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    """Call Claude forcing a single tool_use response. Returns the tool input dict
    or None on failure. Cheaper than Vision since prompts are pure text.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY missing; skipping reasoning step")
        return None
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "tools": [{"name": tool_name, "description": "Structured output.", "input_schema": tool_schema}],
        "tool_choice": {"type": "tool", "name": tool_name},
        "messages": [{"role": "user", "content": user_message}],
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.warning("Anthropic call failed: %s", exc)
        return None
    for block in body.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool_name:
            return block.get("input")
    logger.warning("Anthropic returned no tool_use block: %s", str(body)[:200])
    return None


# ── ticker profile (Perplexity, shared across both classifier + resolver) ───

def canonicalize_theme_names(names: list[str]) -> dict[str, str]:
    """Group raw theme labels extracted from many Steno reports into canonical
    themes via one Claude call.

    Steno's reports name the same theme inconsistently — "Decoupling",
    "Decoupling Theme", "Decoupling / Rare Earths & Nuclear Inputs" are one
    theme; and WWTHFTW lists individual stocks ("DroneShield Ltd",
    "AeroVironment Inc") that belong UNDER a broad theme ("Military Drones").
    This returns {raw_name: canonical_theme_name} so the universe builder can
    collapse duplicates and roll individual stocks up to their parent theme.

    Cached at runtime/steno/theme_canonical.json keyed by the hash of the
    sorted input names — re-runs only when the theme set changes.
    """
    import hashlib

    names = sorted({n for n in names if n})
    if not names:
        return {}
    cache_key = hashlib.md5("|".join(names).encode()).hexdigest()
    cache = _load_json(THEME_CANON_PATH)
    if cache_key in cache:
        return cache[cache_key]

    tool_schema = {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "description": "One entry per distinct underlying theme.",
                "items": {
                    "type": "object",
                    "properties": {
                        "canonical": {
                            "type": "string",
                            "description": "The clearest, most complete theme name for this group.",
                        },
                        "members": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Every input name belonging to this theme — copied verbatim.",
                        },
                    },
                    "required": ["canonical", "members"],
                },
            }
        },
        "required": ["groups"],
    }
    numbered = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
    user_msg = (
        "Below are investment theme / position labels extracted from a series of Steno "
        "Research reports. Collapse the OBVIOUS DUPLICATES — but keep genuinely distinct "
        "themes separate. Be conservative: when in doubt, keep two labels apart.\n\n"
        "Rules:\n"
        "• Merge ONLY when labels clearly describe the same underlying theme — wording "
        "variants like 'Decoupling', 'Decoupling Theme', 'Decoupling / Rare Earths & "
        "Nuclear Inputs' are one theme.\n"
        "• An individual company name belongs UNDER its broad theme IF that broad theme "
        "is also present in the list — 'DroneShield Ltd', 'AeroVironment Inc' group under "
        "'Military Drones'; 'Nextracker Inc', 'Bloom Energy Corp' under 'Electricity / "
        "Power Infrastructure'.\n"
        "• If a label is a genuinely DISTINCT theme — a new sector or a specific ticker "
        "Steno is calling out that doesn't clearly belong to any broad theme in the list "
        "— keep it as its own group. Do NOT force-merge unrelated themes.\n"
        "• Every input name MUST appear in exactly one group's members list, copied verbatim.\n"
        "• Prefer an existing broad-theme label from the list as the canonical name.\n\n"
        f"Labels:\n{numbered}"
    )
    result = anthropic_tool(
        tool_name="group_themes",
        tool_schema=tool_schema,
        system="You group investment theme labels into canonical themes. Reply only via the tool.",
        user_message=user_msg,
        max_tokens=4096,
    )
    mapping: dict[str, str] = {}
    if result:
        for g in result.get("groups", []):
            canon = (g.get("canonical") or "").strip()
            for m in g.get("members", []):
                if isinstance(m, str) and m.strip() and canon:
                    mapping[m.strip()] = canon
    # Anything Claude missed maps to itself — never drop a theme.
    for n in names:
        mapping.setdefault(n, n)

    cache[cache_key] = mapping
    _save_json(THEME_CANON_PATH, cache)
    return mapping


def fetch_ticker_profile(ticker: str, *, force: bool = False) -> dict[str, Any] | None:
    """Fresh, bucket-independent ticker fact-sheet from Perplexity.

    Returns {sector, industry, business, primary_exposure, recent_notable, citations}.
    Cached at runtime/steno/ticker_profiles.json for PROFILE_MAX_AGE_DAYS.
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return None

    cache = _load_json(PROFILES_PATH)
    cached = cache.get(ticker)
    if cached and not force:
        ts = cached.get("fetched_at")
        if ts:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
                if age < timedelta(days=PROFILE_MAX_AGE_DAYS):
                    return cached
            except Exception:
                pass

    prompt = (
        f"What is the US-listed (or major exchange) ticker '{ticker}'? Return JSON only:\n"
        "{\n"
        '  "sector": "<GICS sector or commodity/crypto class>",\n'
        '  "industry": "<more specific industry>",\n'
        '  "business": "<1-sentence plain-English of what it actually does>",\n'
        '  "primary_exposure": "<the dominant macro theme this ticker gives exposure to — e.g. \'gold miners equity\', \'silver futures via miners equity\', \'spot bitcoin\', \'US large-cap broad market\', \'drone defense pure-play\'>",\n'
        '  "asset_type": "<one of: equity-single | equity-etf | bond-etf | commodity-etf | crypto-etf | currency-etf | other>",\n'
        '  "recent_notable": "<one sentence on any recent material news / sector flag in the last 90 days, or empty>"\n'
        "}\n"
        "If you cannot identify the ticker confidently, set all string fields to empty strings."
    )
    payload = perplexity_json(
        prompt,
        system="You return facts about securities. Reply with JSON only. Be precise about whether something gives direct vs equity-beta exposure to its underlying theme.",
    )
    if not payload:
        return None
    payload["ticker"] = ticker
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
    cache[ticker] = payload
    _save_json(PROFILES_PATH, cache)
    return payload
