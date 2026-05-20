"""Steno portfolio extractor — runs Claude with a strict tool_use schema to
pull structured portfolio signal out of transcribed Steno-Research PDFs.

Doc-type-aware prompting:
  • Steno Signals (macro only): extract risk_tone + summary + macro_notes, but
    DO NOT extract positions — these reports have no portfolio table and any
    "positions" Claude returns are hallucinations from headlines.
  • Weekly Alpha Digest: extract narrative trims/adds from the Portfolio Update
    section (commentary-style, may not have explicit weights).
  • What We Told Hedge Funds: most actionable — explicit ticker mentions with
    direction + thesis. Treat ticker-level signals as authoritative.
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
from app.services.steno.doc_types import DocType


logger = logging.getLogger(__name__)


def _system_prompt(doc: DocType | None) -> str:
    if doc is None or doc.key == "steno_signals":
        return (
            "You are extracting MACRO context from a 'Steno Signals' report. These reports "
            "discuss themes and regime calls but DO NOT contain a model portfolio table. "
            "Return ONLY risk_tone, summary, and macro_notes. The positions array MUST be "
            "empty — do NOT extract themes as portfolio positions, even if Steno emphasises "
            "a sector in the title or body. Hallucinated positions break downstream mirroring."
        )
    if doc.key == "weekly_alpha_digest":
        return (
            "You are extracting portfolio changes from a 'Weekly Alpha Digest' report. This "
            "report has a 'Portfolio Update' / 'YTD Performance' section in narrative form. "
            "Extract each ticker or theme Steno discusses with a direction change "
            "(added / trimmed / closed / new / held) into the positions array. Weights are "
            "often implicit — set target_weight_pct to the explicit % if Steno gives one, "
            "otherwise leave it as 0 and capture the action in change_vs_prior + commentary."
        )
    if doc.key == "what_we_told_hedge_funds":
        return (
            "You are extracting tactical positions from a 'What We Told Hedge Funds This Week' "
            "report — the most ticker-explicit Steno product. Extract every named ticker with "
            "its direction (long/short), thesis context, and any stated weight. If no weight "
            "is stated, leave target_weight_pct as 0 (the position is tactical / unsized). "
            "These signals are authoritative — DO NOT invent positions but DO capture every "
            "ticker mentioned with a thesis attached."
        )
    return (
        "You are extracting portfolio data from a Steno-Research PDF. Find any portfolio table "
        "or per-ticker thesis discussion and translate to the tool schema verbatim. Do not invent "
        "positions. If a position name has no ticker, leave ticker null. Round weights to 2 decimals."
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


def extract_portfolio(
    transcript: str,
    pdf_stem: str | None = None,
    doc_type: DocType | None = None,
) -> dict[str, Any]:
    """Send transcript to Claude with portfolio tool_use, return parsed JSON.

    Cached per pdf_stem in runtime/steno/cache/<stem>-portfolio.json to avoid
    re-billing on dashboard reloads. The doc_type controls the system prompt
    so we don't extract phantom positions from non-portfolio reports.
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
        "system": _system_prompt(doc_type),
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

    # Stamp the doc type so downstream consumers can weight + label correctly.
    if doc_type is not None:
        portfolio["doc_type"] = doc_type.key
        portfolio["doc_label"] = doc_type.label
    # Defence-in-depth: a Steno Signals extraction should NEVER have positions.
    if doc_type and doc_type.key == "steno_signals" and portfolio.get("positions"):
        logger.info("Discarding %d hallucinated positions from steno_signals report %s",
                    len(portfolio["positions"]), pdf_stem)
        portfolio["positions"] = []

    if cache_path:
        cache_path.write_text(json.dumps(portfolio, indent=2))
    return portfolio
