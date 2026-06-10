from app.services.cio_message import render, snapshot_for_store, _what_changed, _factor_riders
from app.services.hermes_state import HermesState
from app.services.risk_budget import compute


def _state(score_inputs=None, stance="Cautious Risk-On"):
    rb_in = score_inputs or {k: 0.0 for k in ("liquidity", "growth", "risk_on_off", "dollar", "short_rates", "inflation", "oil")}
    rb = compute(rb_in)
    return HermesState(
        stance=rb.stance,
        risk_budget=rb.score,
        deploy_pct=rb.deploy_pct,
        cash_pct=rb.cash_pct,
        macro_season="Summer",
        liquidity_state="EXPANDING",
        cycle_state="EXPANSION",
        confidence=rb.confidence,
        last_updated="2026-05-30T08:00",
        summary="Maintain core exposure.",
        risk_budget_detail=rb.to_dict(),
    )


def test_render_contains_all_required_fields():
    s = _state()
    out = render(s, prev=None)
    assert "DJG HERMES CIO WEEKLY" in out
    assert "Risk Budget: 50 / 100" in out
    assert "Stance: Cautious Risk-On" in out
    assert "Deployment: 55%" in out
    assert "Cash Reserve: 45%" in out
    assert "Macro Season: Summer" in out
    assert "Liquidity: EXPANDING" in out
    assert "Cycle: EXPANSION" in out
    assert "Add risk if:" in out
    assert "Cut risk if:" in out
    assert "MIT overlay:" in out


def test_initial_print_when_no_prior_state():
    bullets = _what_changed(None, _state())
    assert any("Initial print" in b for b in bullets)


def test_diff_detects_score_change_direction():
    prev = {"risk_budget": 45, "stance": "Cautious Risk-On"}
    curr = _state()  # score 50
    bullets = _what_changed(prev, curr)
    assert any("rose" in b and "45" in b and "50" in b for b in bullets)


def test_diff_detects_stance_change():
    prev = {"risk_budget": 60, "stance": "Cautious Risk-On"}
    # Force a stance change via a bullish scenario → score > 60
    bullish = {"liquidity": 1.0, "growth": 1.0, "risk_on_off": 0.5,
               "dollar": -1.0, "short_rates": -1.0, "inflation": 0.0, "oil": 0.0}
    curr = _state(bullish)
    bullets = _what_changed(prev, curr)
    assert any("Stance changed" in b for b in bullets)


def test_diff_no_change_falls_back_to_trend_holds_line():
    s = _state()
    prev = snapshot_for_store(s)
    bullets = _what_changed(prev, s)
    assert any("Trend holds" in b for b in bullets)


def test_add_cut_lines_flip_with_stance():
    # Full Risk-On
    bullish = {"liquidity": 1.0, "growth": 1.0, "risk_on_off": 1.0,
               "dollar": -1.0, "short_rates": -1.0, "inflation": -1.0, "oil": -1.0}
    out = render(_state(bullish), prev=None)
    assert "deteriorates" in out  # cut-risk language for risk-on stance

    # Fortress
    bearish = {k: v for k, v in bullish.items()}
    for k in ("liquidity", "growth", "risk_on_off"):
        bearish[k] = -1.0
    for k in ("dollar", "short_rates", "inflation", "oil"):
        bearish[k] = 1.0
    out = render(_state(bearish), prev=None)
    assert "turns positive" in out  # add-risk language for defensive stance


def test_snapshot_for_store_has_diff_keys():
    snap = snapshot_for_store(_state())
    for key in ("risk_budget", "stance", "liquidity_state", "cycle_state", "macro_season", "saved_at"):
        assert key in snap


def test_factor_riders_emit_for_tilted_factors_only():
    """Factors with |z| < 0.5 should be skipped — keeps the bulletin punchy."""
    inputs = {
        "liquidity": 0.7,       # → "expanding"
        "growth": 0.1,          # below threshold, skipped
        "risk_on_off": 0.9,     # → "firm"
        "dollar": -0.8,         # inverted → "weakening"
        "short_rates": 0.0,     # skipped
        "inflation": -0.3,      # skipped
        "oil": 1.0,             # inverted → "hot"
    }
    bullets = _factor_riders(inputs)
    text = " ".join(bullets).lower()
    assert "liquidity" in text and "expanding" in text
    assert "risk appetite" in text and "firm" in text
    assert "dollar" in text and "weakening" in text
    assert "oil" in text and "hot" in text
    # Skipped ones absent
    assert "growth" not in text  # growth z=0.1 is below threshold
    assert "rates" not in text


def test_message_includes_factor_commentary_when_tilted():
    bullish = {"liquidity": 0.8, "growth": 0.8, "risk_on_off": 0.7,
               "dollar": -0.7, "short_rates": -0.6, "inflation": -0.6, "oil": 0.0}
    out = render(_state(bullish), prev=None)
    assert "Driving factors:" in out
    # At least liquidity + growth + dollar should appear
    assert "Liquidity" in out and "expanding" in out
    assert "Growth" in out and "rising" in out
    assert "Dollar" in out and "weakening" in out


def test_message_with_all_neutral_falls_through_cleanly():
    out = render(_state(), prev=None)
    # Initial print + the explicit fall-through note since no factors are tilted
    assert "Initial print" in out
    assert "near neutral" in out or "Driving factors:" not in out


def test_factor_commentary_appears_with_prior_week_too():
    """Even when nothing changed regime-wise, the factor read should print."""
    bullish = {"liquidity": 0.8, "growth": 0.8, "risk_on_off": 0.7,
               "dollar": -0.7, "short_rates": -0.6, "inflation": -0.6, "oil": 0.0}
    curr = _state(bullish)
    prev = snapshot_for_store(curr)  # same → no regime change
    out = render(curr, prev=prev)
    assert "Driving factors:" in out
