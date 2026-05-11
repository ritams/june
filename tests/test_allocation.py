"""Unit tests for the allocation engine: softmax + caps + tier."""

from __future__ import annotations

import math

from app.services.allocation import (
    CLASS_CAPS,
    EM_CAP,
    SINGLE_NAME_CAP,
    build_allocation,
    confidence_tier,
)


def _scored(key: str, ticker: str, bucket: str, score: float, t: float = 2.0, e_r: float = 1.0, low_history: bool = False) -> dict:
    return {
        "key": key,
        "label": key,
        "ticker": ticker,
        "bucket": bucket,
        "benchmark": None,
        "basis": "absolute",
        "expected_return": e_r,
        "avg_t_stat": t,
        "avg_t_stat_hac": t,
        "composite_score": score,
        "factors_used": 3,
        "low_history": low_history,
        "disclosures": [],
    }


def test_confidence_tier_thresholds() -> None:
    assert confidence_tier(3.5)["key"] == "high"
    assert confidence_tier(3.0)["key"] == "high"
    assert confidence_tier(2.5)["key"] == "medium"
    assert confidence_tier(2.0)["key"] == "medium"
    assert confidence_tier(1.7)["key"] == "low"
    assert confidence_tier(1.5)["key"] == "low"
    assert confidence_tier(1.4)["key"] == "noise"
    assert confidence_tier(0)["key"] == "noise"
    assert confidence_tier(None)["key"] == "noise"


def test_softmax_weights_sum_to_unity_or_less() -> None:
    scored = {
        "SPY": _scored("SPY", "SPY", "equity_region", 5.0),
        "QQQ": _scored("QQQ", "QQQ", "equity_region", 4.0),
        "TLT": _scored("TLT", "TLT", "fixed_income", 2.0),
    }
    out = build_allocation(scored)
    assert out["available"]
    total = sum(a["weight"] for a in out["top_assets"]) + out["cash_weight"]
    assert math.isclose(total, 1.0, rel_tol=1e-3)


def test_class_caps_enforced() -> None:
    # Saturate equities to force the equity bucket cap (60%)
    scored = {
        f"E{i}": _scored(f"E{i}", f"E{i}", "equity_region", 10.0 + i)
        for i in range(15)
    }
    out = build_allocation(scored)
    equity_total = sum(
        a["weight"] for a in out["top_assets"]
        if a["bucket"] in {"equity_region", "equity_sector", "style"}
    )
    assert equity_total <= CLASS_CAPS["equity"] + 1e-3
    # Residual goes to cash
    assert out["cash_weight"] > 0


def test_crypto_cap_5_percent() -> None:
    # Crypto has a strong score but cap is 5%
    scored = {
        "BTC": _scored("BTC", "BTC-USD", "crypto", 100.0),
        "SPY": _scored("SPY", "SPY", "equity_region", 1.0),
    }
    out = build_allocation(scored)
    btc = next((a for a in out["top_assets"] if a["key"] == "BTC"), None)
    assert btc is not None
    assert btc["weight"] <= CLASS_CAPS["crypto"] + 1e-3


def test_single_name_cap() -> None:
    # One asset has overwhelming score; cap should clip to 25%
    scored = {
        "MEGA": _scored("MEGA", "MEGA", "equity_region", 1000.0),
        "OK1": _scored("OK1", "OK1", "equity_region", 5.0),
        "OK2": _scored("OK2", "OK2", "equity_region", 4.0),
    }
    out = build_allocation(scored)
    mega = next((a for a in out["top_assets"] if a["key"] == "MEGA"), None)
    assert mega is not None
    assert mega["weight"] <= SINGLE_NAME_CAP + 1e-3


def test_em_cap_applies_within_equity() -> None:
    scored = {
        "EEM": _scored("EEM", "EEM", "equity_region", 50.0),
        "EWZ": _scored("EWZ", "EWZ", "equity_region", 40.0),
        "EWW": _scored("EWW", "EWW", "equity_region", 30.0),
        "EWY": _scored("EWY", "EWY", "equity_region", 20.0),
    }
    out = build_allocation(scored)
    em_total = sum(a["weight"] for a in out["top_assets"] if a.get("is_em"))
    assert em_total <= EM_CAP + 1e-3


def test_negative_scores_become_bottom_assets_not_shorts() -> None:
    scored = {
        "GOOD": _scored("GOOD", "GOOD", "equity_region", 5.0),
        "BAD": _scored("BAD", "BAD", "equity_region", -3.0),
    }
    out = build_allocation(scored)
    bad_in_top = any(a["key"] == "BAD" for a in out["top_assets"])
    bad_in_bottom = any(a["key"] == "BAD" for a in out["bottom_assets"])
    assert not bad_in_top
    assert bad_in_bottom


def test_low_history_excluded_from_ranking() -> None:
    scored = {
        "HIGH_SCORE_LOW_HIST": _scored(
            "HIGH_SCORE_LOW_HIST", "HPS-A.TO", "commodity", 50.0, low_history=True
        ),
        "NORMAL": _scored("NORMAL", "GLD", "commodity", 1.0),
    }
    out = build_allocation(scored)
    # The low-history asset should NOT appear in top_assets nor bottom_assets
    keys = {a["key"] for a in out["top_assets"]} | {a["key"] for a in out["bottom_assets"]}
    assert "HIGH_SCORE_LOW_HIST" not in keys


def test_all_negative_returns_full_cash() -> None:
    scored = {
        "A": _scored("A", "A", "equity_region", -2.0),
        "B": _scored("B", "B", "equity_region", -1.0),
    }
    out = build_allocation(scored)
    assert out["cash_weight"] == 1.0
    assert out["top_assets"] == []
