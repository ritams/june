from app.services.risk_budget import compute, band_for_score, BANDS, WEIGHTS


def _neutral() -> dict:
    return {k: 0.0 for k in ("liquidity", "growth", "risk_on_off", "dollar", "short_rates", "inflation", "oil")}


def test_all_neutral_lands_at_50_cautious():
    rb = compute(_neutral())
    assert rb.score == 50
    assert rb.stance == "Cautious Risk-On"
    assert rb.deploy_pct == 55
    assert rb.cash_pct == 45


def test_all_max_risk_on_lands_at_100_full():
    # liquidity/growth/risk_on_off at +1; dollar/rates/inflation/oil at -1 (which is good for risk
    # because the weights invert them). This is the maximum-risk-on configuration.
    scenario = {
        "liquidity": 1.0, "growth": 1.0, "risk_on_off": 1.0,
        "dollar": -1.0, "short_rates": -1.0, "inflation": -1.0, "oil": -1.0,
    }
    rb = compute(scenario)
    assert rb.score == 100
    assert rb.stance == "Full Risk-On"
    assert rb.deploy_pct == 90 and rb.cash_pct == 10


def test_all_max_risk_off_lands_at_0_fortress():
    scenario = {
        "liquidity": -1.0, "growth": -1.0, "risk_on_off": -1.0,
        "dollar": 1.0, "short_rates": 1.0, "inflation": 1.0, "oil": 1.0,
    }
    rb = compute(scenario)
    assert rb.score == 0
    assert rb.stance == "Fortress Mode"
    assert rb.deploy_pct == 20 and rb.cash_pct == 80


def test_dollar_is_inverted():
    """Rising dollar should LOWER the risk budget, not raise it."""
    base = _neutral()
    rising_dollar = {**base, "dollar": 1.0}
    falling_dollar = {**base, "dollar": -1.0}
    assert compute(rising_dollar).score < compute(falling_dollar).score


def test_rates_is_inverted():
    base = _neutral()
    rising = {**base, "short_rates": 1.0}
    falling = {**base, "short_rates": -1.0}
    assert compute(rising).score < compute(falling).score


def test_inflation_oil_combined_and_inverted():
    base = _neutral()
    high = {**base, "inflation": 1.0, "oil": 1.0}
    low = {**base, "inflation": -1.0, "oil": -1.0}
    # Both should clearly shift score
    assert compute(high).score < 50 < compute(low).score
    # mean(±1, ±1) = ±1 → contribution = 0.10 * ∓1 = ∓0.10 on weighted_sum.
    # Full swing on weighted_sum = 0.20 → swing on score = 0.20/2 * 100 = 10.
    delta = compute(low).score - compute(high).score
    assert delta == 10


def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_bands_cover_0_to_100_no_gaps_or_overlap():
    # Each adjacent pair: prev.hi + 1 == next.lo
    for prev, curr in zip(BANDS, BANDS[1:]):
        assert prev[1] + 1 == curr[0], f"Gap or overlap between {prev} and {curr}"
    assert BANDS[0][0] == 0
    assert BANDS[-1][1] == 100


def test_band_for_score_boundaries():
    # Each band edge
    assert band_for_score(0)["stance"] == "Fortress Mode"
    assert band_for_score(20)["stance"] == "Fortress Mode"
    assert band_for_score(21)["stance"] == "Defensive"
    assert band_for_score(40)["stance"] == "Defensive"
    assert band_for_score(41)["stance"] == "Cautious Risk-On"
    assert band_for_score(60)["stance"] == "Cautious Risk-On"
    assert band_for_score(61)["stance"] == "Constructive Risk-On"
    assert band_for_score(80)["stance"] == "Constructive Risk-On"
    assert band_for_score(81)["stance"] == "Full Risk-On"
    assert band_for_score(100)["stance"] == "Full Risk-On"


def test_band_for_score_clamps():
    assert band_for_score(-50)["score"] == 0
    assert band_for_score(150)["score"] == 100


def test_confidence_high_when_many_factors_tilted():
    # 5+ factors with |z| >= 0.5
    scenario = {
        "liquidity": 0.6, "growth": 0.6, "risk_on_off": 0.6,
        "dollar": -0.6, "short_rates": -0.6, "inflation": 0.0, "oil": 0.0,
    }
    assert compute(scenario).confidence == "High"


def test_confidence_low_when_factors_near_neutral():
    scenario = {k: 0.1 for k in ("liquidity", "growth", "risk_on_off", "dollar", "short_rates", "inflation", "oil")}
    assert compute(scenario).confidence == "Low"


def test_missing_factor_treated_as_neutral():
    rb = compute({"liquidity": 0.5})  # everything else absent
    # Only liquidity contributes: 0.30 * 0.5 = 0.15 → score ≈ 57.5.
    # Float arithmetic on 0.15 + 1.0 lands fractionally below, so round() returns 57.
    assert rb.score in (57, 58)
    assert rb.stance == "Cautious Risk-On"


def test_to_dict_round_trips_basic_shape():
    rb = compute(_neutral())
    d = rb.to_dict()
    assert d["score"] == 50
    assert d["stance"] == "Cautious Risk-On"
    assert "components" in d and len(d["components"]) == 6
    assert "weighted_sum" in d
    assert "inputs" in d
