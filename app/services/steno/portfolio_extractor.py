"""Steno portfolio extractor — runs Claude with a strict tool_use schema to
pull a structured model portfolio out of the transcribed PDF text.

Unlike the macro-summary analyzer in steno-bot, this is laser-focused on the
*portfolio table* that we need to mirror against IBKR: ticker, target weight,
direction, asset class, change-vs-prior, and per-position commentary.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.services.steno.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_API_URL,
    ANTHROPIC_VERSION,
    CLAUDE_MODEL,
    STENO_CACHE_DIR,
)


logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are extracting Steno Signals model-portfolio data from a transcribed PDF. "
    "Your only job is to find the portfolio table (positions, target weights, commentary) "
    "and translate it into the tool schema verbatim. Do not invent positions. "
    "If a position name has no ticker, leave ticker null. Round weights to 2 decimals."
)

PORTFOLIO_TOOL: dict[str, Any] = {
    "name": "extract_steno_portfolio",
    "description": "Extract Steno's model portfolio as structured data for mirroring against IBKR.",
    "input_schema": {
        "type": "object",
        "properties": {
            "report_date": {
                "type": "string",
                "description": "ISO-8601 date of the Steno Signals report (YYYY-MM-DD). Best-effort parse from cover page.",
            },
            "report_title": {"type": "string", "description": "Full report title (e.g. 'Steno Signals 4 March 2026')."},
            "risk_tone": {
                "type": "string",
                "enum": ["risk-on", "risk-off", "neutral", "selective"],
                "description": "Overall risk posture Steno is conveying in this report.",
            },
            "summary": {"type": "string", "description": "2-3 sentence top-level thesis. Max 60 words."},
            "positions": {
                "type": "array",
                "description": "Every position in the model portfolio table. Include longs and shorts. Skip cash here (it's tracked separately).",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Human-readable position name (e.g. 'Gold', 'US 10y Treasury', 'NVDA')."},
                        "ticker": {
                            "type": ["string", "null"],
                            "description": "Exchange ticker if Steno states one (e.g. 'GLD', 'TLT', 'NVDA'). null if only a generic asset name is given.",
                        },
                        "asset_class": {
                            "type": "string",
                            "enum": ["equity", "bond", "commodity", "currency", "crypto", "other"],
                        },
                        "direction": {"type": "string", "enum": ["long", "short"]},
                        "target_weight_pct": {
                            "type": "number",
                            "description": "Steno's target portfolio weight in percent (e.g. 6.0 means 6%). Use the absolute weight even for shorts.",
                        },
                        "entry_price": {"type": ["number", "null"]},
                        "change_vs_prior": {
                            "type": ["string", "null"],
                            "enum": ["new", "added", "trimmed", "held", "exited", None],
                            "description": "How the position changed vs the previous report, if Steno marks it. null if not stated.",
                        },
                        "commentary": {"type": "string", "description": "Steno's per-position commentary. Max 30 words."},
                    },
                    "required": ["name", "asset_class", "direction", "target_weight_pct", "commentary"],
                },
            },
            "cash_weight_pct": {
                "type": ["number", "null"],
                "description": "Cash allocation in percent (e.g. 25.0). null if Steno doesn't break out cash.",
            },
            "macro_notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 5 macro indicators Steno is monitoring (DXY, ISM, oil, credit spreads, etc.) — short phrases only.",
            },
        },
        "required": ["report_date", "risk_tone", "positions"],
    },
}


def extract_portfolio(transcript: str, pdf_stem: str | None = None) -> dict[str, Any]:
    """Send transcript to Claude with portfolio tool_use, return parsed JSON.

    Cached per pdf_stem in runtime/steno/cache/<stem>-portfolio.json to avoid
    re-billing on dashboard reloads.
    """
    cache_path = STENO_CACHE_DIR / f"{pdf_stem}-portfolio.json" if pdf_stem else None
    if cache_path and cache_path.exists():
        logger.info("Portfolio cache hit for %s", pdf_stem)
        return json.loads(cache_path.read_text())

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY missing in environment")

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "tools": [PORTFOLIO_TOOL],
        "tool_choice": {"type": "tool", "name": "extract_steno_portfolio"},
        "messages": [{"role": "user", "content": f"Transcript:\n\n{transcript[:120000]}"}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    with httpx.Client(timeout=120.0) as client:
        resp = client.post(ANTHROPIC_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        body = resp.json()

    portfolio: dict[str, Any] | None = None
    for block in body.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "extract_steno_portfolio":
            portfolio = block["input"]
            break
    if portfolio is None:
        raise RuntimeError("Claude did not return a tool_use block — extraction failed")

    if cache_path:
        cache_path.write_text(json.dumps(portfolio, indent=2))
    return portfolio
