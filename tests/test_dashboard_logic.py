from app.models import Dashboard, Metric
from app.services.dashboard import overall_signal, summarize_section


def _metric(status: str) -> Metric:
    return Metric(
        key=status,
        label=status,
        display_value=status,
        status=status,
        summary=status,
    )


def test_liquidity_summary_positive() -> None:
    status, tone = summarize_section(4, 0, "liquidity")
    assert status == "EXPANDING"
    assert tone == "positive"


def test_cycle_summary_negative() -> None:
    status, tone = summarize_section(1, 3, "cycle")
    assert status == "SLOWDOWN"
    assert tone == "negative"


def test_overall_signal_risk_on() -> None:
    liquidity = Dashboard("liquidity", "Liquidity", "EXPANDING", "positive", [_metric("positive") for _ in range(4)])
    cycle = Dashboard("business-cycle", "Business Cycle", "MID-EXPANSION", "positive", [_metric("positive") for _ in range(4)])
    label, tone, _ = overall_signal(liquidity, cycle)
    assert label == "RISK ON"
    assert tone == "positive"


def test_overall_signal_risk_off() -> None:
    liquidity = Dashboard("liquidity", "Liquidity", "CONTRACTING", "negative", [_metric("negative") for _ in range(4)])
    cycle = Dashboard("business-cycle", "Business Cycle", "SLOWDOWN", "negative", [_metric("negative") for _ in range(4)])
    label, tone, _ = overall_signal(liquidity, cycle)
    assert label == "RISK OFF"
    assert tone == "negative"
