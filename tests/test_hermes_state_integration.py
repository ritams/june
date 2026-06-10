"""Integration test for the hermes_state assembly path.

Unit tests cover each service in isolation. This test exercises the actual
assembly used by `/api/hermes/state` and pins the contract: given a scenario,
season label, and liquidity/cycle states, the produced HermesState has the
right shape and the summary text is keyed to the stance.

Pure-Python — no network, no FastAPI test client needed.
"""

from app.services.hermes_state import build, DEFAULT_MIT_OVERLAY, DEFAULT_SLR_NOTE


def _neutral_scenario():
    return {k: 0.0 for k in ("liquidity", "growth", "risk_on_off", "dollar", "short_rates", "inflation", "oil")}


def test_assembly_returns_expected_dict_shape():
    state = build(
        scenario=_neutral_scenario(),
        season="Summer",
        liquidity_state="EXPANDING",
        cycle_state="EXPANSION",
    )
    d = state.to_dict()
    required = [
        "stance", "risk_budget", "deploy_pct", "cash_pct",
        "macro_season", "liquidity_state", "cycle_state",
        "confidence", "last_updated", "summary",
        "mit_overlay", "slr_note",
        "risk_budget_detail", "season_detail",
    ]
    for key in required:
        assert key in d, f"Missing top-level key {key}"


def test_assembly_summary_changes_with_stance():
    bullish = {
        "liquidity": 1.0, "growth": 1.0, "risk_on_off": 1.0,
        "dollar": -1.0, "short_rates": -1.0, "inflation": -1.0, "oil": -1.0,
    }
    bearish = {k: -v for k, v in bullish.items()}

    bull = build(bullish, season="Summer", liquidity_state="EXPANDING", cycle_state="EXPANSION")
    bear = build(bearish, season="Winter", liquidity_state="CONTRACTING", cycle_state="CONTRACTION")

    assert bull.stance == "Full Risk-On"
    assert bear.stance == "Fortress Mode"
    assert "Press risk" in bull.summary
    assert "Maximum defence" in bear.summary
    # Season riders fire correctly
    assert "Summer" in bull.summary and "aligns" in bull.summary
    assert "Winter" in bear.summary and "confirms" in bear.summary


def test_assembly_handles_unknown_states():
    """When the dashboard snapshot is cold / unavailable, the caller passes
    'Unknown' for liquidity_state and cycle_state. State should still build."""
    state = build(
        scenario=_neutral_scenario(),
        season="Unknown",
        liquidity_state="Unknown",
        cycle_state="Unknown",
    )
    assert state.liquidity_state == "Unknown"
    assert state.cycle_state == "Unknown"
    assert state.macro_season == "Unknown"
    # Risk Budget still computes (uses scenario only)
    assert 0 <= state.risk_budget <= 100


def test_overlays_default_when_not_supplied():
    state = build(
        scenario=_neutral_scenario(),
        season="Summer",
        liquidity_state="EXPANDING",
        cycle_state="EXPANSION",
    )
    assert state.mit_overlay == DEFAULT_MIT_OVERLAY
    assert state.slr_note == DEFAULT_SLR_NOTE


def test_overlays_can_be_overridden():
    state = build(
        scenario=_neutral_scenario(),
        season="Summer",
        liquidity_state="EXPANDING",
        cycle_state="EXPANSION",
        mit_overlay="Custom MIT view text.",
        slr_note="Bank Plumbing / SLR: Restrictive (eSLR live April 1)",
    )
    assert state.mit_overlay == "Custom MIT view text."
    assert state.slr_note.endswith("April 1)")


def test_assembly_emits_iso_timestamp_with_timezone():
    state = build(
        scenario=_neutral_scenario(),
        season="Summer",
        liquidity_state="EXPANDING",
        cycle_state="EXPANSION",
        timezone="Europe/London",
    )
    ts = state.last_updated
    # ISO format with a timezone offset
    assert "T" in ts
    assert ts[-6] in ("+", "-")  # offset
