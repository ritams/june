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


def test_liquidity_summary_positive() -> None:
    metrics = {
        "dxy": _metric("dxy", "positive", raw_value=99.5),
        "m2": _metric("m2", "positive", raw_value=21.4, mom=0.6),
        "rrp": _metric("rrp", "positive", raw_value=182.0, direction="falling"),
    }
    status, tone, _ = summarize_section(metrics, "liquidity")
    assert status == "EXPANDING"
    assert tone == "positive"


def test_cycle_summary_negative() -> None:
    metrics = {
        "ism_pmi": _metric("ism_pmi", "negative", raw_value=49.4),
        "yield_curve": _metric("yield_curve", "negative", raw_value=-0.25),
        "credit_spreads": _metric("credit_spreads", "negative", raw_value=540.0),
    }
    status, tone, _ = summarize_section(metrics, "cycle")
    assert status == "CONTRACTION"
    assert tone == "negative"


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
