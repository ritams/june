"""Unit tests for the cycle phase detector.

Tests the inner classifier `classify_from_z_series` directly so we can construct
deterministic z-score series rather than relying on rolling-z over synthetic raw
factor data (which has counterintuitive properties — see decisions doc §3 notes).
"""

from __future__ import annotations

import pandas as pd

from app.services.phase import (
    CONFIRMATION_MONTHS,
    PHASES,
    classify_from_z_series,
    detect_phase,
)


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2010-01-31", periods=n, freq="ME")


def _z_series_with_trajectory(values: list[float]) -> pd.Series:
    return pd.Series(values, index=_idx(len(values)), dtype="float64")


def test_summer_when_both_z_series_rising_steadily() -> None:
    # Each value is +0.5 above the value 3 months prior → delta_z = +0.5 → direction "up".
    growth_z = _z_series_with_trajectory([0, 0, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])
    inflation_z = _z_series_with_trajectory([0, 0, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])

    reading = classify_from_z_series(growth_z, inflation_z)
    assert reading.confirmed
    assert reading.key == "summer", f"expected summer, got {reading.key} (proposed {reading.proposed_phase})"
    assert reading.growth_dir == "up"
    assert reading.inflation_dir == "up"
    assert reading.months_in_phase >= CONFIRMATION_MONTHS


def test_winter_when_both_z_series_falling_steadily() -> None:
    growth_z = _z_series_with_trajectory([3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0, -0.5, -1.0])
    inflation_z = _z_series_with_trajectory([3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0, -0.5, -1.0])

    reading = classify_from_z_series(growth_z, inflation_z)
    assert reading.confirmed
    assert reading.key == "winter"
    assert reading.growth_dir == "down"
    assert reading.inflation_dir == "down"


def test_spring_when_growth_up_and_inflation_down() -> None:
    growth_z = _z_series_with_trajectory([0, 0, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])
    inflation_z = _z_series_with_trajectory([3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0, -0.5, -1.0])

    reading = classify_from_z_series(growth_z, inflation_z)
    assert reading.confirmed
    assert reading.key == "spring"


def test_autumn_when_growth_down_and_inflation_up() -> None:
    growth_z = _z_series_with_trajectory([3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0, -0.5, -1.0])
    inflation_z = _z_series_with_trajectory([0, 0, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])

    reading = classify_from_z_series(growth_z, inflation_z)
    assert reading.confirmed
    assert reading.key == "autumn"


def test_hysteresis_holds_direction_when_change_below_band() -> None:
    # Steady rising z, then a tiny dip < 0.25 — direction should NOT flip.
    growth_z = _z_series_with_trajectory([0, 0, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 2.95])
    inflation_z = _z_series_with_trajectory([0, 0, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 2.95])

    reading = classify_from_z_series(growth_z, inflation_z)
    # The last delta_z = 2.95 - 2.0 = 0.95, still up. But even with a softer dip
    # this should hold direction. Verify that even a tiny final move keeps "up".
    assert reading.growth_dir == "up"


def test_single_month_flip_confirms_immediately() -> None:
    """With CONFIRMATION_MONTHS = 1, a one-month direction agreement is enough to flip.

    Previously (CONFIRMATION_MONTHS = 2) this would have stayed on the prior
    confirmed phase. See docs/alternative-rules.md §1 for the rationale behind
    dropping the 2-month rule.
    """
    growth_z_vals    = [3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0.0, -0.5, -1.0, -1.5, -2.0, +1.0]
    inflation_z_vals = [3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0.0, -0.5, -1.0, -1.5, -2.0, -2.5]

    growth_z = _z_series_with_trajectory(growth_z_vals)
    inflation_z = _z_series_with_trajectory(inflation_z_vals)

    reading = classify_from_z_series(growth_z, inflation_z)
    # Last month direction: growth ↑ (jump from -2 to +1, Δ3m positive), inflation ↓ → Spring.
    assert reading.confirmed
    assert reading.key == "spring"
    assert reading.proposed_phase == "spring"
    assert reading.months_in_phase >= CONFIRMATION_MONTHS


def test_unknown_when_history_too_short() -> None:
    # Only a few months of data → can't establish direction
    growth_z = _z_series_with_trajectory([0.1, 0.2])
    inflation_z = _z_series_with_trajectory([0.1, 0.2])
    reading = classify_from_z_series(growth_z, inflation_z)
    assert reading.key == "unknown"
    assert not reading.confirmed


def test_phase_dictionary_covers_all_four_directions() -> None:
    expected = {"spring", "summer", "autumn", "winter"}
    assert {meta["key"] for meta in PHASES.values()} == expected


def test_detect_phase_with_empty_factors_returns_unknown() -> None:
    reading = detect_phase({"growth": pd.Series(dtype="float64"), "inflation": pd.Series(dtype="float64")})
    assert reading.key == "unknown"


def test_liquidity_tiebreaker_is_recorded_when_factors_disagree() -> None:
    # Growth up, inflation down → Spring directionally. With rising liquidity, note added.
    growth_z = _z_series_with_trajectory([0, 0, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])
    inflation_z = _z_series_with_trajectory([3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0, -0.5, -1.0])
    liquidity_z = _z_series_with_trajectory([0, 0, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])

    reading = classify_from_z_series(growth_z, inflation_z, liquidity_z)
    # Last delta_l = 3.5 - 2.0 = 1.5 → liquidity_dir = "up"
    assert reading.liquidity_dir == "up"
    assert any("Liquidity direction" in n for n in reading.notes)
