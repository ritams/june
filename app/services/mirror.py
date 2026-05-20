"""Steno → IBKR Mirror Engine — bucket-aggregated.

Steno publishes *thematic* portfolio buckets ("U.S. CapEx / Domestic Cycle
Equities 15%", "Drone Defence ETF 10%", "Energy / Oil & Gas Stocks 10%") and
only occasionally names a specific ticker. Dan owns a basket of individual
stocks. Strict ticker-equality mirroring leaves nearly all of Dan's holdings
flagged as "Remove" even when they fit a Steno theme — so we aggregate
bottom-up: classify each Dan ticker into a Steno bucket (via equivalence map +
Perplexity), then compute the gap on the bucket as a whole.

Assignment cascade for each Dan ticker:
  1. Equivalence map — if it's fungible with a Steno-named ticker, it counts
     toward that bucket (e.g. IAU ↔ GLD for gold).
  2. Manual override (`runtime/steno/bucket_overrides.json`) — Dan can pin a
     ticker to a bucket or explicitly mark it off-thesis.
  3. Perplexity classifier — given the bucket commentaries, asks for the best
     fit. Cached per (ticker, bucket-set).
  4. Off-thesis — no clean fit → ends up in the Remove list.

Bucket-level signals (Buy/Add/Hold/Trim/Sell/Missing) are then computed from
Steno target vs aggregated Dan weight across all bucket members. Off-thesis
holdings each get their own Remove row.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.equivalence import equivalents
from app.services.steno import bucket_classifier
from app.services.steno import store as steno_store


logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE_PCT = 0.5

# Steno sometimes names an asset rather than a ticker ("Gold" vs "GLD"). This
# map lets the equivalence pass still resolve those to a canonical ticker so
# the bucket members get aggregated correctly even when no Perplexity proxy is
# attached. Kept small — for everything else the Perplexity ticker resolver
# already populates pos["ticker"] before we get here.
DEFAULT_ALIASES: dict[str, str] = {
    "gold": "GLD",
    "silver": "SLV",
    "oil": "USO",
    "wti": "USO",
    "us 10y treasury": "IEF",
    "us 10y": "IEF",
    "us 20y+ treasury": "TLT",
    "us long bonds": "TLT",
    "tips": "TIP",
    "s&p 500": "SPY",
    "sp500": "SPY",
    "nasdaq": "QQQ",
    "russell 2000": "IWM",
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "copper": "COPX",
    "natural gas": "UNG",
    "us dollar": "UUP",
    "dollar index": "UUP",
}


def _resolve_ticker(name: str, ticker: str | None) -> str | None:
    if ticker:
        return ticker.upper()
    if not name:
        return None
    return DEFAULT_ALIASES.get(name.strip().lower())


def _classify_action(gap_pct: float, dan_pct_abs: float, steno_pct_abs: float, tolerance: float) -> str:
    if steno_pct_abs < 1e-6 and dan_pct_abs > tolerance:
        return "Sell"
    if dan_pct_abs < tolerance and steno_pct_abs > tolerance:
        return "Buy"
    if abs(gap_pct) <= tolerance:
        return "Hold"
    if gap_pct > 0:
        return "Add"
    return "Trim"


def build_mirror(
    steno_portfolio: dict[str, Any] | None = None,
    ibkr_snapshot: dict[str, Any] | None = None,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
) -> dict[str, Any]:
    if steno_portfolio is None:
        # Use the rolling theme universe — last 6 reports' worth of themes,
        # union'd by name with most-recent-valid-weight winning per theme.
        # Falls back to the latest committed model if no universe is buildable.
        steno_portfolio = steno_store.universe_as_portfolio() or steno_store.get_latest()
    if ibkr_snapshot is None:
        from app.services.ibkr import store as ibkr_store_mod
        ibkr_snapshot = ibkr_store_mod.load_snapshot()

    if not steno_portfolio:
        return {
            "available": False,
            "reason": "No Steno portfolio ingested yet — upload or refresh.",
            "buckets": [],
            "off_thesis": [],
            "summary": {},
        }

    nav = (ibkr_snapshot or {}).get("nav") if ibkr_snapshot else None
    base_ccy = (ibkr_snapshot or {}).get("base_currency", "USD") if ibkr_snapshot else "USD"

    # ── 1. Build buckets from Steno positions ────────────────────────────────
    raw_positions = steno_portfolio.get("positions", []) or []
    buckets: list[dict[str, Any]] = []
    for pos in raw_positions:
        name = pos.get("name", "")
        resolved = _resolve_ticker(name, pos.get("ticker"))
        direction = (pos.get("direction") or "long").lower()
        sign = 1.0 if direction == "long" else -1.0
        steno_pct_abs = pos.get("target_weight_pct") or 0.0
        buckets.append({
            "name": name,
            "ticker": resolved,
            "ticker_source": pos.get("ticker_source"),
            "ticker_rationale": pos.get("ticker_rationale"),
            "asset_class": pos.get("asset_class", "other"),
            "direction": direction,
            "steno_weight_pct": steno_pct_abs * sign,
            "commentary": pos.get("commentary", ""),
            "change_vs_prior": pos.get("change_vs_prior"),
            # Universe metadata (when this bucket came from the rolling universe)
            "is_core": pos.get("is_core"),
            "is_tactical": pos.get("is_tactical"),
            "source_report_date": pos.get("source_report_date"),
            "first_seen": pos.get("first_seen"),
            "last_seen": pos.get("last_seen"),
            "appearances": pos.get("appearances"),
            "members": [],
        })

    bucket_by_name = {b["name"]: b for b in buckets}
    classifier_buckets = [
        {"name": b["name"], "asset_class": b["asset_class"], "direction": b["direction"], "commentary": b["commentary"]}
        for b in buckets
    ]

    # ── 2. Walk Dan's holdings and assign each to a bucket ───────────────────
    holdings = ((ibkr_snapshot or {}).get("positions", [])) if ibkr_snapshot else []
    matched_symbols: set[str] = set()

    # 2a. Equivalence-map pass — exact / fungible ticker match wins outright
    for h in holdings:
        sym = (h.get("symbol") or "").upper().strip()
        if not sym:
            continue
        for b in buckets:
            if not b["ticker"]:
                continue
            if sym in equivalents(b["ticker"]):
                mv = h.get("market_value") or 0.0
                weight = (mv / nav * 100) if nav else 0.0
                b["members"].append({
                    "symbol": sym,
                    "description": h.get("description") or "",
                    "weight_pct": round(weight, 2),
                    "market_value": round(mv, 0),
                    "rationale": (
                        f"Equivalent to {b['ticker']}." if sym != b["ticker"]
                        else f"Direct match to Steno's {b['ticker']}."
                    ),
                    "source": "equivalence",
                })
                matched_symbols.add(sym)
                break

    # 2b. Perplexity classifier pass — for everything not already matched
    unmatched_holdings = [
        h for h in holdings
        if (h.get("symbol") or "").upper().strip()
        and (h.get("symbol") or "").upper().strip() not in matched_symbols
    ]
    classifications = bucket_classifier.classify_holdings(unmatched_holdings, classifier_buckets)

    off_thesis: list[dict[str, Any]] = []
    for h in unmatched_holdings:
        sym = (h.get("symbol") or "").upper().strip()
        result = classifications.get(sym) or {"bucket": None, "rationale": "", "source": "unknown"}
        bucket_name = result.get("bucket")
        mv = h.get("market_value") or 0.0
        weight = (mv / nav * 100) if nav else 0.0
        if bucket_name and bucket_name in bucket_by_name:
            bucket_by_name[bucket_name]["members"].append({
                "symbol": sym,
                "description": h.get("description") or "",
                "weight_pct": round(weight, 2),
                "market_value": round(mv, 0),
                "rationale": result.get("rationale", ""),
                "source": result.get("source", "dual-ai"),
                "confidence": round(float(result.get("confidence") or 0.0), 2),
                "thesis_match": bool(result.get("thesis_match", True)),
                "direction_match": bool(result.get("direction_match", True)),
            })
        else:
            if abs(weight) >= tolerance_pct:
                off_thesis.append({
                    "symbol": sym,
                    "description": h.get("description") or sym,
                    "weight_pct": round(weight, 2),
                    "market_value": round(mv, 0),
                    "rationale": result.get("rationale", "") or "No Steno bucket fits this position.",
                    "source": result.get("source", "dual-ai"),
                    "confidence": round(float(result.get("confidence") or 0.0), 2),
                    "action": "Remove",
                    "capital_amount": round(mv, 0),
                })

    # ── 3. Aggregate per-bucket gap + action ─────────────────────────────────
    for b in buckets:
        sign = 1.0 if b["direction"] == "long" else -1.0
        dan_pct_signed = sum(m["weight_pct"] for m in b["members"]) * sign
        gap = b["steno_weight_pct"] - dan_pct_signed
        cap = (nav or 0.0) * abs(gap) / 100.0
        b["dan_weight_pct"] = round(dan_pct_signed, 2)
        b["gap_pct"] = round(gap, 2)
        b["capital_amount"] = round(cap, 0)
        b["action"] = _classify_action(gap, abs(dan_pct_signed), abs(b["steno_weight_pct"]), tolerance_pct)
        b["steno_weight_pct"] = round(b["steno_weight_pct"], 2)
        b["members"].sort(key=lambda m: -abs(m["weight_pct"]))
        # Warnings — anything the AI flagged that the UI should surface
        warnings: list[str] = []
        for m in b["members"]:
            if m.get("direction_match") is False:
                warnings.append(
                    f"{m['symbol']} is long {m.get('rationale','').rstrip('.')} — but Steno is {b['direction']} this theme."
                    if b["direction"] == "short"
                    else f"{m['symbol']} direction may not match Steno's {b['direction']} stance."
                )
            elif m.get("confidence", 1.0) < 0.5:
                warnings.append(f"{m['symbol']} is a low-confidence fit ({int(m.get('confidence',0)*100)}%) — review.")
        b["warnings"] = warnings

    # ── 4. Detect Missing — bucket names in prior report but gone from latest,
    # where Dan still holds members in the equivalence group ───────────────
    missing_rows: list[dict[str, Any]] = []
    prior = steno_store.previous_portfolio()
    if prior and ibkr_snapshot:
        latest_names = {b["name"] for b in buckets}
        for pp in prior.get("positions", []) or []:
            if pp.get("name") in latest_names:
                continue
            resolved = _resolve_ticker(pp.get("name", ""), pp.get("ticker"))
            if not resolved:
                continue
            group = equivalents(resolved)
            held_members = []
            for h in holdings:
                sym = (h.get("symbol") or "").upper().strip()
                if sym in group:
                    mv = h.get("market_value") or 0.0
                    weight = (mv / nav * 100) if nav else 0.0
                    held_members.append({
                        "symbol": sym, "weight_pct": round(weight, 2),
                        "market_value": round(mv, 0),
                        "rationale": f"Was tracked under '{pp.get('name')}' last report.",
                        "source": "equivalence",
                    })
            if not held_members:
                continue
            dan_pct = sum(m["weight_pct"] for m in held_members)
            if dan_pct <= tolerance_pct:
                continue
            missing_rows.append({
                "name": pp.get("name"),
                "ticker": resolved,
                "ticker_source": pp.get("ticker_source"),
                "asset_class": pp.get("asset_class", "other"),
                "direction": pp.get("direction", "long"),
                "steno_weight_pct": 0.0,
                "dan_weight_pct": round(dan_pct, 2),
                "gap_pct": round(-dan_pct, 2),
                "capital_amount": round((nav or 0.0) * dan_pct / 100.0, 0),
                "action": "Missing",
                "commentary": "Steno dropped this from the model since the prior report — confirm intent.",
                "members": held_members,
            })
            # Strip these symbols from off-thesis so they don't double-count
            off_thesis = [o for o in off_thesis if o["symbol"] not in group]

    # ── 5. Summary metrics + sort ────────────────────────────────────────────
    all_buckets = buckets + missing_rows
    action_counts: dict[str, int] = {}
    for b in all_buckets:
        action_counts[b["action"]] = action_counts.get(b["action"], 0) + 1
    if off_thesis:
        action_counts["Remove"] = action_counts.get("Remove", 0) + len(off_thesis)

    aligned = sum(1 for b in buckets if b["action"] == "Hold")
    alignment_pct = (aligned / len(buckets) * 100) if buckets else 0.0
    total_capital = (
        sum(b["capital_amount"] for b in all_buckets if b["action"] != "Hold")
        + sum(o["market_value"] for o in off_thesis)
    )

    priority = {"Buy": 0, "Sell": 1, "Add": 2, "Trim": 3, "Missing": 4, "Remove": 5, "Hold": 6}
    all_buckets.sort(key=lambda b: (priority.get(b["action"], 9), -abs(b["gap_pct"])))
    off_thesis.sort(key=lambda o: -abs(o["weight_pct"]))

    return {
        "available": True,
        "tolerance_pct": tolerance_pct,
        "nav": nav,
        "base_currency": base_ccy,
        "steno_report_date": steno_portfolio.get("report_date"),
        "steno_report_title": steno_portfolio.get("report_title"),
        "steno_risk_tone": steno_portfolio.get("risk_tone"),
        "steno_summary": steno_portfolio.get("summary"),
        "steno_cash_weight_pct": steno_portfolio.get("cash_weight_pct"),
        "ibkr_account": (ibkr_snapshot or {}).get("account_id"),
        "ibkr_fetched_at": (ibkr_snapshot or {}).get("fetched_at"),
        "ibkr_connected": ibkr_snapshot is not None,
        "alignment_pct": round(alignment_pct, 1),
        "total_buckets": len(buckets),
        "off_thesis_count": len(off_thesis),
        "action_counts": action_counts,
        "total_capital_to_move": round(total_capital, 0),
        "buckets": all_buckets,
        "off_thesis": off_thesis,
        "universe_meta": steno_portfolio.get("_universe_meta"),
    }
