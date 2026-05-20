"""Classify Dan's IBKR tickers into Steno's thematic buckets — dual-AI.

Pipeline per ticker:
  1. Manual override (`runtime/steno/bucket_overrides.json`) — Dan's explicit pin
     always wins. `None` means "explicitly off-thesis."
  2. Cache lookup keyed by (ticker, current-bucket-set-signature). When Steno
     publishes new buckets, the cache invalidates automatically.
  3. Perplexity fetches a fresh ticker profile (sector, industry, plain-English
     business description, primary exposure, recent news). Cached separately at
     `runtime/steno/ticker_profiles.json` for 14 days — sector doesn't move
     week-to-week, so this stage is cheap on repeat refreshes.
  4. Anthropic reasons over the profile + Steno's bucket commentaries via a
     forced tool_use call. Returns {bucket, confidence, rationale,
     thesis_match, direction_match}.

Why dual-AI: Perplexity has live web search (catches new ETFs like JEDI that
Claude's training cutoff might miss, and tracks sector reclassifications);
Anthropic is stronger at thematic judgment given long-form Steno commentary.
Splitting the work plays to each model's strength.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.services.steno import ai_helpers
from app.services.steno.config import STENO_ROOT


logger = logging.getLogger(__name__)

CLASSIFICATIONS_PATH = STENO_ROOT / "bucket_classifications.json"
OVERRIDES_PATH = STENO_ROOT / "bucket_overrides.json"


# ── Override I/O ─────────────────────────────────────────────────────────────

def load_overrides() -> dict[str, str | None]:
    return ai_helpers._load_json(OVERRIDES_PATH)


def set_override(ticker: str, bucket_name: str | None) -> dict[str, str | None]:
    overrides = load_overrides()
    overrides[ticker.upper().strip()] = bucket_name
    ai_helpers._save_json(OVERRIDES_PATH, overrides)
    return overrides


def clear_override(ticker: str) -> dict[str, str | None]:
    overrides = load_overrides()
    overrides.pop(ticker.upper().strip(), None)
    ai_helpers._save_json(OVERRIDES_PATH, overrides)
    return overrides


def _bucket_signature(bucket_names: list[str]) -> str:
    return "|".join(sorted(bucket_names))


# ── Anthropic tool schema ────────────────────────────────────────────────────

CLASSIFY_TOOL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bucket": {
            "type": ["string", "null"],
            "description": "Exact bucket name from the provided list, or null if no clean fit.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0–1.0 — how strongly does the ticker's primary exposure match this bucket's thesis. Below 0.5 is a soft / 'maybe' fit.",
        },
        "thesis_match": {
            "type": "boolean",
            "description": "True if the underlying theme matches (e.g. silver miners → silver-themed bucket). Use this even when direction mismatches.",
        },
        "direction_match": {
            "type": "boolean",
            "description": "True if Dan's likely long/short on this ticker matches Steno's direction in the bucket. False if e.g. Dan is long silver-miners but Steno is short silver futures.",
        },
        "rationale": {
            "type": "string",
            "description": "One short sentence explaining the choice — especially if confidence < 0.7 or direction_match is false.",
        },
    },
    "required": ["bucket", "confidence", "thesis_match", "direction_match", "rationale"],
}


# ── Main classifier ──────────────────────────────────────────────────────────

def classify_ticker(
    ticker: str,
    description: str | None,
    buckets: list[dict[str, Any]],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Return {bucket, confidence, rationale, source, thesis_match, direction_match, profile}.

    `buckets` is a list of {name, asset_class, direction, commentary} from the
    current Steno portfolio.
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return _miss("Empty ticker.", source="skipped")

    overrides = load_overrides()
    if ticker in overrides:
        return {
            "bucket": overrides[ticker],
            "confidence": 1.0,
            "thesis_match": overrides[ticker] is not None,
            "direction_match": True,  # user pinned, presume intentional
            "rationale": "Manual override.",
            "source": "override",
            "profile": None,
        }

    if not buckets:
        return _miss("No Steno buckets to classify against.", source="skipped")

    bucket_names = [b.get("name", "") for b in buckets if b.get("name")]
    sig = _bucket_signature(bucket_names)
    cache = ai_helpers._load_json(CLASSIFICATIONS_PATH)
    cache_key = f"{ticker}::{sig}"
    if not force and cache_key in cache:
        return {**cache[cache_key], "source": "cached"}

    # ── Stage 1: Perplexity ticker profile ────────────────────────────────
    profile = ai_helpers.fetch_ticker_profile(ticker, force=False)
    if not profile or not profile.get("business"):
        # No live data — fall back to ticker symbol only and let Claude reason from prior.
        profile = {"ticker": ticker, "business": description or "", "primary_exposure": "", "sector": "", "industry": "", "asset_type": ""}

    # ── Stage 2: Anthropic thematic judgment ──────────────────────────────
    bucket_lines = "\n".join(
        f'  • "{b["name"]}" — direction={b.get("direction","long")}, asset_class={b.get("asset_class","?")}\n'
        f'    Steno thesis: {(b.get("commentary") or "")[:240]}'
        for b in buckets
    )
    profile_lines = (
        f"  Ticker: {profile.get('ticker')}\n"
        f"  Sector: {profile.get('sector','')}\n"
        f"  Industry: {profile.get('industry','')}\n"
        f"  Business: {profile.get('business','')}\n"
        f"  Primary exposure: {profile.get('primary_exposure','')}\n"
        f"  Asset type: {profile.get('asset_type','')}\n"
        f"  Recent notable: {profile.get('recent_notable','')}"
    )
    user_msg = (
        "Classify the following holding into exactly one of Steno's current model-portfolio "
        "buckets, OR null if no thesis is a clean fit.\n\n"
        f"=== Holding profile ===\n{profile_lines}\n\n"
        f"=== Steno's current buckets ===\n{bucket_lines}\n\n"
        "Important judgments:\n"
        "• Match THEMATIC EXPOSURE first — e.g. SIL (silver miners ETF) DOES match a "
        "'Silver Futures' bucket because both are silver-themed exposure, even though one is "
        "equity-beta and one is a futures contract. Set direction_match=false in that case "
        "since long miners ≠ short futures.\n"
        "• Reject only if the holding's primary exposure is genuinely unrelated to every "
        "bucket (e.g. AAPL has no fit when Steno's buckets are drone defense / energy / BTC).\n"
        "• Set confidence honestly — 0.9+ for direct matches, 0.5–0.8 for indirect/proxy "
        "matches, below 0.5 only for weak fits we should flag.\n"
        "• The bucket name in your response MUST be copied character-for-character from the list above."
    )
    result = ai_helpers.anthropic_tool(
        tool_name="classify_into_bucket",
        tool_schema=CLASSIFY_TOOL,
        system="You classify securities into Steno's thematic model-portfolio buckets. You prioritize thematic match over instrument-type pedantry, and flag direction mismatches explicitly. Reply only via the tool.",
        user_message=user_msg,
        max_tokens=512,
    )
    if not result:
        return _miss("Anthropic classifier unavailable.", source="error", profile=profile)

    # Defensive: validate bucket name is real (Claude may hallucinate)
    bucket = result.get("bucket")
    if isinstance(bucket, str):
        canon = next((b["name"] for b in buckets if b["name"].lower() == bucket.lower()), None)
        bucket = canon
    else:
        bucket = None

    out = {
        "bucket": bucket,
        "confidence": float(result.get("confidence") or 0.0),
        "thesis_match": bool(result.get("thesis_match")),
        "direction_match": bool(result.get("direction_match", True)),
        "rationale": str(result.get("rationale") or ""),
        "profile": profile,
    }
    cache[cache_key] = out
    ai_helpers._save_json(CLASSIFICATIONS_PATH, cache)
    return {**out, "source": "dual-ai"}


def classify_holdings(
    holdings: list[dict[str, Any]],
    buckets: list[dict[str, Any]],
    *,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for h in holdings:
        sym = (h.get("symbol") or "").upper().strip()
        if not sym:
            continue
        out[sym] = classify_ticker(sym, h.get("description") or "", buckets, force=force)
    return out


def _miss(rationale: str, *, source: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "bucket": None,
        "confidence": 0.0,
        "thesis_match": False,
        "direction_match": False,
        "rationale": rationale,
        "source": source,
        "profile": profile,
    }
