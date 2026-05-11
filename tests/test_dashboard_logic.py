from app.models import Dashboard, Metric
from app.services.dashboard import overall_signal, summarize_section


def _metric(key: str, status: str, raw_value: float | None = None, **details: float | str | bool | None) -> Metric:
    return Metric(
        key=key,
        label=key,
        display_value=key,
        status=status,
        summary=key,
        raw_value=raw_value,
        details=details,
    )


def _liq(*statuses: str) -> dict[str, Metric]:
    keys = ["dxy", "m2", "rrp", "tga", "fed_balance_sheet", "global_m2_proxy"]
    return {keys[i]: _metric(keys[i], statuses[i]) for i in range(len(statuses))}


def _cycle(*statuses: str) -> dict[str, Metric]:
    keys = ["ism_pmi", "yield_curve", "credit_spreads", "jobless_claims", "korean_exports"]
    return {keys[i]: _metric(keys[i], statuses[i]) for i in range(len(statuses))}


# ─── Liquidity count-based classifier ───

def test_liquidity_expanding_at_80_percent_positive() -> None:
    # 5 of 6 positive → ≥80% → EXPANDING
    status, tone, _ = summarize_section(_liq("positive", "positive", "positive", "positive", "positive", "neutral"), "liquidity")
    assert status == "EXPANDING"
    assert tone == "positive"


def test_liquidity_expanding_all_positive() -> None:
    status, tone, _ = summarize_section(_liq(*(["positive"] * 6)), "liquidity")
    assert status == "EXPANDING"


def test_liquidity_neutral_just_under_threshold() -> None:
    # 4 of 6 positive (66%) → below 80% → NEUTRAL
    status, tone, _ = summarize_section(_liq("positive", "positive", "positive", "positive", "neutral", "neutral"), "liquidity")
    assert status == "NEUTRAL"


def test_liquidity_contracting_at_50_percent_negative() -> None:
    # 3 of 6 negative → CONTRACTING
    status, tone, _ = summarize_section(_liq("negative", "negative", "negative", "neutral", "neutral", "neutral"), "liquidity")
    assert status == "CONTRACTING"
    assert tone == "negative"


# ─── Cycle count-based classifier ───

def test_cycle_expansion_at_80_percent_positive() -> None:
    # 4 of 5 positive → EXPANSION
    status, tone, _ = summarize_section(_cycle("positive", "positive", "positive", "positive", "neutral"), "cycle")
    assert status == "EXPANSION"
    assert tone == "positive"


def test_cycle_late_cycle_4_positive_1_negative() -> None:
    # 4 positive but 1 negative — LATE CYCLE, not full EXPANSION
    status, tone, _ = summarize_section(_cycle("positive", "positive", "positive", "positive", "negative"), "cycle")
    assert status == "LATE CYCLE"
    assert tone == "neutral"


def test_cycle_contraction_at_40_percent_negative() -> None:
    # 2 of 5 negative → CONTRACTION
    status, tone, _ = summarize_section(_cycle("negative", "negative", "neutral", "neutral", "neutral"), "cycle")
    assert status == "CONTRACTION"


def test_cycle_transition_when_mixed_below_thresholds() -> None:
    # 2 positive, 1 negative, 2 neutral → TRANSITION
    status, tone, _ = summarize_section(_cycle("positive", "positive", "negative", "neutral", "neutral"), "cycle")
    assert status == "TRANSITION"


# ─── Overall signal combinator (unchanged from before) ───

def test_overall_signal_risk_on() -> None:
    liquidity = Dashboard("liquidity", "Liquidity", "EXPANDING", "positive", "ok", [_metric("dxy", "positive")])
    cycle = Dashboard("business-cycle", "Business Cycle", "EXPANSION", "positive", "ok", [_metric("ism_pmi", "positive")])
    label, tone, _, action = overall_signal(liquidity, cycle)
    assert label == "RISK ON"
    assert tone == "positive"
    assert "high conviction" in action


def test_overall_signal_risk_off() -> None:
    liquidity = Dashboard("liquidity", "Liquidity", "CONTRACTING", "negative", "ok", [_metric("dxy", "negative")])
    cycle = Dashboard("business-cycle", "Business Cycle", "TRANSITION", "neutral", "ok", [_metric("ism_pmi", "neutral")])
    label, tone, _, action = overall_signal(liquidity, cycle)
    assert label == "RISK OFF"
    assert tone == "negative"
    assert "Reduce risk" in action


def test_overall_signal_selective() -> None:
    liquidity = Dashboard("liquidity", "Liquidity", "NEUTRAL", "neutral", "ok", [_metric("dxy", "neutral")])
    cycle = Dashboard("business-cycle", "Business Cycle", "LATE CYCLE", "neutral", "ok", [_metric("ism_pmi", "neutral")])
    label, tone, _, action = overall_signal(liquidity, cycle)
    assert label == "SELECTIVE"
    assert tone == "neutral"
    assert "Stay selective" in action
