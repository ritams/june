"""Resolve ambiguous Steno theme names to a concrete US-listed ticker — dual-AI.

When Steno writes "Drone Defence ETF" or "Select Shipping Stocks" without a
specific ticker, we want a single canonical proxy for the mirror engine.

Two stages:
  1. Perplexity (live search) surfaces 3 candidate US-listed tickers for the
     theme, with AUM / market cap, brief description, and recent notes. This
     catches new ETFs Claude's training cutoff might miss (e.g. JEDI for drone
     defense, which only just launched).
  2. Anthropic (Claude with tool_use) picks the best fit given Steno's
     commentary, preferring liquidity + pure-play fit. Returns
     {ticker, rationale, confidence}.

Results cached at `runtime/steno/ticker_resolutions.json` keyed by
(asset_class, theme-name). Inline-mentioned tickers (e.g. "Drone Defence ETF
(e.g. JEDI)") short-circuit the whole flow.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from app.services.steno import ai_helpers
from app.services.steno.config import STENO_ROOT


logger = logging.getLogger(__name__)

RESOLUTIONS_PATH = STENO_ROOT / "ticker_resolutions.json"


# ── Anthropic tool schema ────────────────────────────────────────────────────

RESOLVER_TOOL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticker": {
            "type": ["string", "null"],
            "description": "Best US-listed ticker proxy (uppercase), copied verbatim from one of the candidates. null if no candidate is acceptable.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0–1.0 — how strongly this ticker captures the theme. Below 0.6 indicates a soft / debatable choice.",
        },
        "rationale": {
            "type": "string",
            "description": "One short sentence explaining why this ticker beats the other candidates for the theme.",
        },
    },
    "required": ["ticker", "confidence", "rationale"],
}


def _cache_key(name: str, asset_class: str) -> str:
    return f"{(asset_class or 'other').lower()}::{name.strip().lower()}"


def _explicit_ticker_in_name(name: str) -> str | None:
    """If Steno writes "Drone Defence ETF (e.g. JEDI)" we don't need to spend
    an API call — just pull JEDI directly."""
    m = re.search(r"\(e\.?g\.?\s*([A-Z]{2,6})\)", name)
    if m:
        return m.group(1).upper()
    return None


def resolve_theme_to_ticker(
    name: str,
    asset_class: str,
    commentary: str | None = None,
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    """Return {ticker, rationale, confidence, candidates, source} or None."""
    if not name:
        return None

    inline = _explicit_ticker_in_name(name)
    if inline:
        return {
            "ticker": inline,
            "rationale": f"Steno report named '{inline}' inline.",
            "confidence": 1.0,
            "candidates": [],
            "source": "steno-inline",
        }

    key = _cache_key(name, asset_class)
    cache = ai_helpers._load_json(RESOLUTIONS_PATH)
    if not force and key in cache:
        return {**cache[key], "source": "cached"}

    # ── Stage 1: Perplexity candidates ────────────────────────────────────
    asset_hint = {
        "equity": "US-listed equity ETFs or single stocks",
        "bond": "US-listed bond ETFs",
        "commodity": "US-listed commodity ETFs or futures proxies",
        "currency": "US-listed currency ETFs",
        "crypto": "US-listed spot ETFs (e.g. IBIT, FBTC)",
    }.get((asset_class or "").lower(), "US-listed ETFs or stocks")

    candidates_prompt = (
        f"What are the 3 most relevant {asset_hint} that give exposure to: \"{name}\"?\n"
        f"Analyst context: {commentary or '—'}\n\n"
        "Prefer liquid pure-play ETFs over diversified or thinly-traded names. Include any "
        "recently launched ETFs that fit the theme.\n\n"
        "Return ONLY JSON, no markdown:\n"
        "{\n"
        '  "candidates": [\n'
        '    {"ticker": "<TICKER>", "name": "<full name>", "aum_or_mcap_usd": "<approx \\u20243B or null>", "purity": "<pure-play | diversified | proxy>", "note": "<one short clause>"},\n'
        "    ...\n"
        "  ]\n"
        "}"
    )
    candidates_payload = ai_helpers.perplexity_json(
        candidates_prompt,
        system="You identify US-listed tickers for investment themes. Reply with JSON only. Be precise about AUM/liquidity.",
    )
    candidates = (candidates_payload or {}).get("candidates") or []

    if not candidates:
        # Perplexity unavailable — degrade to a single Anthropic-only ask.
        anthro = ai_helpers.anthropic_tool(
            tool_name="resolve_theme_ticker",
            tool_schema=RESOLVER_TOOL,
            system="You map investment themes to a single best US-listed ticker. Prefer liquid pure-play ETFs.",
            user_message=(
                f"Theme: \"{name}\"\nAnalyst context: {commentary or '—'}\nAsset class: {asset_class or 'unknown'}\n\n"
                "Pick the single best US-listed proxy ticker, or null if nothing fits."
            ),
            max_tokens=256,
        )
        if not anthro or not anthro.get("ticker"):
            return None
        result = {
            "ticker": str(anthro["ticker"]).upper().strip(),
            "rationale": anthro.get("rationale") or "",
            "confidence": float(anthro.get("confidence") or 0.5),
            "candidates": [],
        }
        cache[key] = result
        ai_helpers._save_json(RESOLUTIONS_PATH, cache)
        return {**result, "source": "anthropic-only"}

    # ── Stage 2: Anthropic picks the best candidate ──────────────────────
    cand_lines = "\n".join(
        f'  {i+1}. {c.get("ticker","?")} — {c.get("name","")} · AUM/mcap≈{c.get("aum_or_mcap_usd","?")} · {c.get("purity","?")} · {c.get("note","")}'
        for i, c in enumerate(candidates)
    )
    user_msg = (
        f"Steno bucket theme: \"{name}\"\n"
        f"Asset class: {asset_class or 'unknown'}\n"
        f"Steno's commentary: {commentary or '—'}\n\n"
        f"Perplexity surfaced these candidate tickers:\n{cand_lines}\n\n"
        "Pick the SINGLE best ticker — prefer pure-play fit with Steno's thesis, then "
        "liquidity. Return your choice in the tool. The ticker you return must be ONE of "
        "the candidates listed above (uppercase, exact)."
    )
    anthro = ai_helpers.anthropic_tool(
        tool_name="resolve_theme_ticker",
        tool_schema=RESOLVER_TOOL,
        system="You map investment themes to the best US-listed ticker. Prefer pure-play thesis fit, then liquidity. Reply only via the tool.",
        user_message=user_msg,
        max_tokens=384,
    )
    if not anthro:
        # Soft fallback: take the first candidate Perplexity suggested.
        first = candidates[0]
        result = {
            "ticker": str(first.get("ticker", "")).upper().strip(),
            "rationale": first.get("note") or "Top Perplexity candidate (Anthropic unavailable).",
            "confidence": 0.5,
            "candidates": candidates,
        }
    else:
        chosen = anthro.get("ticker")
        if isinstance(chosen, str):
            chosen = chosen.upper().strip()
            valid = {str(c.get("ticker", "")).upper().strip() for c in candidates}
            if chosen not in valid:
                chosen = None  # hallucinated — reject
        else:
            chosen = None
        if not chosen:
            return None
        result = {
            "ticker": chosen,
            "rationale": anthro.get("rationale") or "",
            "confidence": float(anthro.get("confidence") or 0.0),
            "candidates": candidates,
        }

    cache[key] = result
    ai_helpers._save_json(RESOLUTIONS_PATH, cache)
    return {**result, "source": "dual-ai"}


def enrich_portfolio_tickers(portfolio: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Walk portfolio positions; fill in ticker where missing via dual-AI."""
    positions = portfolio.get("positions") or []
    resolved_count = 0
    for pos in positions:
        if pos.get("ticker"):
            pos.setdefault("ticker_source", "steno")
            continue
        asset_class = (pos.get("asset_class") or "").lower()
        if asset_class == "cash":
            continue
        result = resolve_theme_to_ticker(
            pos.get("name", ""),
            asset_class,
            commentary=pos.get("commentary"),
            force=force,
        )
        if result and result.get("ticker"):
            pos["ticker"] = result["ticker"]
            pos["ticker_source"] = "perplexity" if result.get("source") in {"dual-ai", "anthropic-only", "cached"} else result.get("source", "perplexity")
            pos["ticker_rationale"] = result.get("rationale")
            pos["ticker_confidence"] = result.get("confidence")
            resolved_count += 1
        else:
            pos["ticker_source"] = "unresolved"
    if resolved_count:
        logger.info("Resolved %d ambiguous ticker(s) via dual-AI", resolved_count)
    return portfolio
