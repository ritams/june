"""Allocation engine — convert scored assets into portfolio weights.

Spec (see docs/djg-design-decisions.md §4):
- Composite score from CorrelationService.rank_scenario.
- Long-only: negative scores → zero weight, surfaced as "Bottom 5 to avoid".
- Softmax(score / τ) with τ = cross-sectional std-dev of *positive* scores.
- Per-class hard caps; residual goes to a CASH pseudo-asset.
- Confidence tiers from HAC-adjusted t-stat (see §5).

This module is intentionally pure: it consumes a `scored` dict (asset_key →
entry with composite_score, bucket, etc.) and returns a weight dict plus
diagnostics. CorrelationService composes the scoring; allocation handles the
weighting.
"""

from __future__ import annotations

import math
from typing import Any


# Hard caps per class (see decisions doc §4)
CLASS_CAPS = {
    "crypto":         0.05,
    "fixed_income":   0.40,
    "commodity":      0.20,
    "currency":       0.15,
    "equity":         0.60,   # combined cap across equity_region / equity_sector / style
}

EM_CAP = 0.15
SINGLE_NAME_CAP = 0.25

# Bucket → broad class for cap purposes
BUCKET_TO_CLASS = {
    "equity_region": "equity",
    "equity_sector": "equity",
    "style":         "equity",
    "fixed_income":  "fixed_income",
    "currency":      "currency",
    "commodity":     "commodity",
    "crypto":        "crypto",
}

EM_TICKERS = {"EEM", "EWZ", "EWW", "EWY"}

CONFIDENCE_TIERS = [
    {"key": "high",   "label": "High",   "min_t": 3.0, "color": "green",  "blurb": "Survives multiple-testing bar (HLZ 2016)"},
    {"key": "medium", "label": "Medium", "min_t": 2.0, "color": "yellow", "blurb": "Conventional p<0.05 single-test"},
    {"key": "low",    "label": "Low",    "min_t": 1.5, "color": "orange", "blurb": "Directional only — watch list"},
    {"key": "noise",  "label": "Noise",  "min_t": 0.0, "color": "grey",   "blurb": "Below noise floor; do not size on this"},
]


def confidence_tier(abs_t: float | None) -> dict[str, Any]:
    if abs_t is None:
        return CONFIDENCE_TIERS[-1]
    for tier in CONFIDENCE_TIERS:
        if abs_t >= tier["min_t"]:
            return tier
    return CONFIDENCE_TIERS[-1]


def _softmax(scores: list[float], tau: float) -> list[float]:
    if not scores:
        return []
    if tau <= 0:
        # Degenerate: argmax → 1.0
        max_idx = max(range(len(scores)), key=lambda i: scores[i])
        out = [0.0] * len(scores)
        out[max_idx] = 1.0
        return out
    scaled = [s / tau for s in scores]
    max_s = max(scaled)
    exps = [math.exp(s - max_s) for s in scaled]
    total = sum(exps)
    if total == 0:
        return [0.0] * len(scores)
    return [e / total for e in exps]


def build_allocation(scored: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Convert composite-scored assets into portfolio weights with caps + disclosures.

    Excludes asset-class proxies (bucket == "asset_class") and `low_history`
    assets from ranking. They still appear in the heat-map upstream.
    """
    eligible = [
        entry for entry in scored.values()
        if entry.get("bucket") in BUCKET_TO_CLASS
        and not entry.get("low_history", False)
    ]

    if not eligible:
        return {
            "available": False,
            "top_assets": [],
            "bottom_assets": [],
            "cash_weight": 1.0,
            "cash_weight_pct": 100.0,
            "tau": None,
            "caps": _flatten_caps(),
        }

    # Add tier + tagging up front so downstream can use it.
    # Confidence prefers HAC t-stat (corrects for serial correlation in macro series).
    for entry in eligible:
        hac = entry.get("avg_t_stat_hac")
        t_for_tier = hac if hac is not None else entry.get("avg_t_stat") or 0.0
        entry["confidence"] = confidence_tier(abs(t_for_tier))
        entry["broad_class"] = BUCKET_TO_CLASS[entry["bucket"]]
        entry["is_em"] = (entry.get("ticker") in EM_TICKERS or entry.get("key") in EM_TICKERS)

    # Bottom 5 = most negative composite scores (long-only → zero weight, but surface)
    sorted_desc = sorted(eligible, key=lambda x: x["composite_score"], reverse=True)
    bottom_5 = [e for e in sorted_desc[::-1] if e["composite_score"] < 0][:5]

    # Top candidates: positive composite score only
    positives = [e for e in sorted_desc if e["composite_score"] > 0]
    if not positives:
        return {
            "available": True,
            "top_assets": [],
            "bottom_assets": [_strip_for_output(e) for e in bottom_5],
            "cash_weight": 1.0,
            "cash_weight_pct": 100.0,
            "tau": None,
            "caps": _flatten_caps(),
            "notes": ["No assets have positive composite score in this scenario — full cash."],
        }

    # τ = cross-sectional std-dev of positive scores. Robust to scale.
    pos_scores = [e["composite_score"] for e in positives]
    if len(pos_scores) > 1:
        mean_s = sum(pos_scores) / len(pos_scores)
        var_s = sum((s - mean_s) ** 2 for s in pos_scores) / (len(pos_scores) - 1)
        tau = math.sqrt(max(var_s, 1e-9))
    else:
        tau = max(pos_scores[0], 1e-9)

    raw_weights = _softmax(pos_scores, tau)

    # Apply caps via iterative water-filling: clip overweight bucket/asset, redistribute,
    # repeat until stable or max iterations reached.
    capped = _apply_caps(positives, raw_weights)

    # Residual to cash
    total_weight = sum(w for _, w in capped)
    cash_weight = max(0.0, 1.0 - total_weight)

    # Build top assets list — sorted by final weight, only weight > 0.5%
    weighted = sorted(
        [(entry, w) for entry, w in capped if w >= 0.005],
        key=lambda pair: pair[1],
        reverse=True,
    )

    top_assets = [
        {
            **_strip_for_output(entry),
            "weight": round(w, 4),
            "weight_pct": round(w * 100, 1),
        }
        for entry, w in weighted
    ]

    return {
        "available": True,
        "top_assets": top_assets[:10],  # full top-10, frontend can truncate to 5
        "bottom_assets": [_strip_for_output(e) for e in bottom_5],
        "cash_weight": round(cash_weight, 4),
        "cash_weight_pct": round(cash_weight * 100, 1),
        "tau": round(tau, 3),
        "caps": _flatten_caps(),
        "notes": [],
    }


def _apply_caps(entries: list[dict[str, Any]], raw_weights: list[float]) -> list[tuple[dict[str, Any], float]]:
    """Iterative water-filling cap enforcement.

    Caps: single-name 25%, broad-class (equity/bonds/commodity/currency/crypto), EM 15%.
    Excess weight from clipped buckets is redistributed only to assets that have
    *not* hit a cap in any prior iteration. If no truly-free asset remains, the
    residual flows to cash. This avoids the oscillation where excess loops back
    onto already-capped assets.
    """
    weights = list(raw_weights)
    n = len(entries)
    permanently_clipped: set[int] = set()

    for _ in range(20):  # convergence is fast in practice
        excess = 0.0
        adjustments: dict[int, float] = {i: weights[i] for i in range(n)}

        # Single-name cap
        for i in range(n):
            if adjustments[i] > SINGLE_NAME_CAP:
                excess += adjustments[i] - SINGLE_NAME_CAP
                adjustments[i] = SINGLE_NAME_CAP
                permanently_clipped.add(i)

        # Class caps
        for class_key, cap in CLASS_CAPS.items():
            class_indices = [i for i in range(n) if entries[i]["broad_class"] == class_key]
            if not class_indices:
                continue
            class_total = sum(adjustments[i] for i in class_indices)
            if class_total > cap + 1e-9:
                scale = cap / class_total
                for i in class_indices:
                    excess += adjustments[i] * (1 - scale)
                    adjustments[i] = adjustments[i] * scale
                    permanently_clipped.add(i)

        # EM cap (subset of equity)
        em_indices = [i for i in range(n) if entries[i]["is_em"]]
        if em_indices:
            em_total = sum(adjustments[i] for i in em_indices)
            if em_total > EM_CAP + 1e-9:
                scale = EM_CAP / em_total
                for i in em_indices:
                    excess += adjustments[i] * (1 - scale)
                    adjustments[i] = adjustments[i] * scale
                    permanently_clipped.add(i)

        if excess < 1e-6:
            weights = [adjustments[i] for i in range(n)]
            break

        # Redistribute excess only to assets that haven't been clipped in any iteration
        free_indices = [i for i in range(n) if i not in permanently_clipped]
        free_total = sum(adjustments[i] for i in free_indices)
        if free_total <= 0 or not free_indices:
            # No room to redistribute — residual flows to cash naturally
            weights = [adjustments[i] for i in range(n)]
            break
        for i in free_indices:
            adjustments[i] = adjustments[i] + excess * (adjustments[i] / free_total)
        weights = [adjustments[i] for i in range(n)]

    return list(zip(entries, weights))


def _strip_for_output(entry: dict[str, Any]) -> dict[str, Any]:
    """Output-safe subset of an entry for the API response."""
    return {
        "key": entry["key"],
        "label": entry["label"],
        "ticker": entry.get("ticker"),
        "bucket": entry["bucket"],
        "broad_class": entry.get("broad_class"),
        "expected_return": entry["expected_return"],
        "avg_t_stat": entry["avg_t_stat"],
        "avg_t_stat_hac": entry.get("avg_t_stat_hac"),
        "composite_score": entry["composite_score"],
        "factors_used": entry["factors_used"],
        "low_history": entry.get("low_history", False),
        "is_em": entry.get("is_em", False),
        "confidence": entry.get("confidence"),
        "basis": entry.get("basis"),
        "benchmark": entry.get("benchmark"),
    }


def _flatten_caps() -> dict[str, float]:
    return {
        **CLASS_CAPS,
        "em_subset": EM_CAP,
        "single_name": SINGLE_NAME_CAP,
    }
