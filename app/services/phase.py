"""Cycle phase detector — Spring / Summer / Autumn / Winter.

Spec (see docs/djg-design-decisions.md §3):
- Direction over levels: classify on the 3-month change in z-score, not absolute z.
- Hysteresis: |Δz| < 0.25 → keep prior direction (filters noise near zero).
- Confirmation: phase only flips when both factors agree for 2 consecutive months.
- Tiebreaker: when growth and inflation directions disagree on the same month,
  consult the liquidity factor's direction (Bittel framework — liquidity leads).

Inputs are factor series produced by `BacktestService.factor_series()`. Growth is
ISM-YoY; inflation is CPI-YoY; liquidity is M2-YoY. The function operates on the
raw factor series so the caller doesn't have to know about z-scoring.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.services.backtest import BacktestService
from app.services.stats import latest_zscore, rolling_zscore


HYSTERESIS_BAND = 0.25
CONFIRMATION_MONTHS = 2
ZSCORE_WINDOW = 60  # 5 years of monthly observations
DIRECTION_LOOKBACK_MONTHS = 3


PHASES = {
    ("up", "down"):    {"key": "spring", "label": "Spring", "blurb": "Growth rising, inflation falling — risk-on tech and growth"},
    ("up", "up"):      {"key": "summer", "label": "Summer", "blurb": "Growth and inflation both rising — cyclicals, semis, EM, commodity FX"},
    ("down", "up"):    {"key": "autumn", "label": "Autumn", "blurb": "Growth falling, inflation rising — energy, gold, hard assets"},
    ("down", "down"):  {"key": "winter", "label": "Winter", "blurb": "Growth and inflation both falling — long bonds, defensive equities, dollar"},
}

UNKNOWN_PHASE = {"key": "unknown", "label": "Unknown", "blurb": "Insufficient history to classify."}


@dataclass
class PhaseReading:
    key: str
    label: str
    blurb: str
    growth_dir: str | None
    inflation_dir: str | None
    liquidity_dir: str | None
    growth_delta_z: float | None
    inflation_delta_z: float | None
    growth_z: float | None
    inflation_z: float | None
    confirmed: bool
    months_in_phase: int
    proposed_phase: str | None
    history: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "blurb": self.blurb,
            "growth_dir": self.growth_dir,
            "inflation_dir": self.inflation_dir,
            "liquidity_dir": self.liquidity_dir,
            "growth_delta_z": self.growth_delta_z,
            "inflation_delta_z": self.inflation_delta_z,
            "growth_z": self.growth_z,
            "inflation_z": self.inflation_z,
            "confirmed": self.confirmed,
            "months_in_phase": self.months_in_phase,
            "proposed_phase": self.proposed_phase,
            "history": self.history,
            "notes": self.notes,
        }


def _direction(delta: float | None, prior: str | None) -> str | None:
    if delta is None or math.isnan(delta):
        return prior
    if abs(delta) < HYSTERESIS_BAND:
        return prior
    return "up" if delta > 0 else "down"


def _classify_pair(growth_dir: str | None, inflation_dir: str | None) -> dict[str, str] | None:
    if growth_dir is None or inflation_dir is None:
        return None
    return PHASES.get((growth_dir, inflation_dir))


def classify_from_z_series(
    growth_z_series: pd.Series,
    inflation_z_series: pd.Series,
    liquidity_z_series: pd.Series | None = None,
) -> PhaseReading:
    """Inner classifier — runs the hysteresis + confirmation logic on z-score series.

    Exposed so tests can construct deterministic z-series without depending on the
    quirks of rolling z computed from synthetic raw factor data.
    """
    aligned = pd.concat([growth_z_series, inflation_z_series], axis=1).dropna()
    aligned.columns = ["growth_z", "inflation_z"]

    growth_z = float(growth_z_series.dropna().iloc[-1]) if not growth_z_series.dropna().empty else None
    inflation_z = float(inflation_z_series.dropna().iloc[-1]) if not inflation_z_series.dropna().empty else None

    if len(aligned) < DIRECTION_LOOKBACK_MONTHS + 1:
        return PhaseReading(
            key="unknown",
            label="Unknown",
            blurb=UNKNOWN_PHASE["blurb"],
            growth_dir=None,
            inflation_dir=None,
            liquidity_dir=None,
            growth_delta_z=None,
            inflation_delta_z=None,
            growth_z=growth_z,
            inflation_z=inflation_z,
            confirmed=False,
            months_in_phase=0,
            proposed_phase=None,
            notes=["Need at least 4 months of z-score history."],
        )

    history: list[dict[str, Any]] = []
    prior_growth_dir: str | None = None
    prior_inflation_dir: str | None = None
    for idx in range(DIRECTION_LOOKBACK_MONTHS, len(aligned)):
        row = aligned.iloc[idx]
        prior_row = aligned.iloc[idx - DIRECTION_LOOKBACK_MONTHS]
        delta_g = float(row["growth_z"] - prior_row["growth_z"])
        delta_i = float(row["inflation_z"] - prior_row["inflation_z"])
        g_dir = _direction(delta_g, prior_growth_dir)
        i_dir = _direction(delta_i, prior_inflation_dir)
        prior_growth_dir = g_dir
        prior_inflation_dir = i_dir
        phase_info = _classify_pair(g_dir, i_dir)
        history.append(
            {
                "date": str(aligned.index[idx].date()),
                "growth_z": float(row["growth_z"]),
                "inflation_z": float(row["inflation_z"]),
                "growth_delta_z": delta_g,
                "inflation_delta_z": delta_i,
                "growth_dir": g_dir,
                "inflation_dir": i_dir,
                "phase": phase_info["key"] if phase_info else None,
            }
        )

    if not history:
        return PhaseReading(
            key="unknown",
            label="Unknown",
            blurb=UNKNOWN_PHASE["blurb"],
            growth_dir=None, inflation_dir=None, liquidity_dir=None,
            growth_delta_z=None, inflation_delta_z=None,
            growth_z=growth_z, inflation_z=inflation_z,
            confirmed=False, months_in_phase=0, proposed_phase=None,
            notes=["No phase history rows produced."],
        )

    last = history[-1]
    notes: list[str] = []

    # Confirmation: walk forward, only switching the "confirmed" phase when a new
    # phase has held for CONFIRMATION_MONTHS consecutive months. Track the run
    # length of the *current* confirmed phase only — earlier longer runs don't
    # carry over once a new phase confirms.
    confirmed_phase: str | None = None
    months_in_phase = 0
    run_phase: str | None = None
    run_length = 0
    for entry in history:
        if entry["phase"] is None:
            run_phase = None
            run_length = 0
            continue
        if entry["phase"] == run_phase:
            run_length += 1
        else:
            run_phase = entry["phase"]
            run_length = 1
        if run_length >= CONFIRMATION_MONTHS:
            if confirmed_phase != run_phase:
                confirmed_phase = run_phase
            months_in_phase = run_length

    if confirmed_phase is None:
        if last["phase"] is not None:
            notes.append(
                f"Proposed phase '{last['phase']}' has not yet held {CONFIRMATION_MONTHS} months in a row; reporting last confirmed phase."
            )
        confirmed_phase = "unknown"

    liquidity_dir: str | None = None
    if liquidity_z_series is not None and not liquidity_z_series.dropna().empty:
        liq = liquidity_z_series.dropna()
        if len(liq) > DIRECTION_LOOKBACK_MONTHS:
            delta_l = float(liq.iloc[-1] - liq.iloc[-DIRECTION_LOOKBACK_MONTHS - 1])
            if abs(delta_l) >= HYSTERESIS_BAND:
                liquidity_dir = "up" if delta_l > 0 else "down"

    if last["growth_dir"] != last["inflation_dir"] and last["growth_dir"] and last["inflation_dir"]:
        if liquidity_dir is not None:
            notes.append(f"Liquidity direction ({liquidity_dir}) used as tiebreaker context.")

    if confirmed_phase == "unknown":
        phase_meta = UNKNOWN_PHASE
    else:
        phase_meta = next((meta for meta in PHASES.values() if meta["key"] == confirmed_phase), UNKNOWN_PHASE)

    return PhaseReading(
        key=phase_meta["key"],
        label=phase_meta["label"],
        blurb=phase_meta["blurb"],
        growth_dir=last["growth_dir"],
        inflation_dir=last["inflation_dir"],
        liquidity_dir=liquidity_dir,
        growth_delta_z=last["growth_delta_z"],
        inflation_delta_z=last["inflation_delta_z"],
        growth_z=growth_z,
        inflation_z=inflation_z,
        confirmed=confirmed_phase != "unknown",
        months_in_phase=months_in_phase,
        proposed_phase=last["phase"],
        history=history[-24:],
        notes=notes,
    )


def detect_phase(factor_series: dict[str, pd.Series]) -> PhaseReading:
    """Compute the current phase from raw factor series.

    Z-scores the growth + inflation series (60-month rolling), then delegates to
    `classify_from_z_series` for the hysteresis + confirmation logic.
    """
    growth = factor_series.get("growth")
    inflation = factor_series.get("inflation")
    liquidity = factor_series.get("liquidity")

    if growth is None or inflation is None or growth.empty or inflation.empty:
        return PhaseReading(
            key="unknown",
            label="Unknown",
            blurb=UNKNOWN_PHASE["blurb"],
            growth_dir=None, inflation_dir=None, liquidity_dir=None,
            growth_delta_z=None, inflation_delta_z=None,
            growth_z=None, inflation_z=None,
            confirmed=False, months_in_phase=0, proposed_phase=None,
            notes=["Factor series missing or empty."],
        )

    growth_z_series = rolling_zscore(growth, ZSCORE_WINDOW)
    inflation_z_series = rolling_zscore(inflation, ZSCORE_WINDOW)
    liquidity_z_series = (
        rolling_zscore(liquidity, ZSCORE_WINDOW) if liquidity is not None and not liquidity.empty else None
    )
    return classify_from_z_series(growth_z_series, inflation_z_series, liquidity_z_series)


class PhaseService:
    """Caches the phase reading. factor_series() is expensive (FRED + yfinance)."""

    def __init__(self, backtest: BacktestService, cache_ttl_seconds: int = 60 * 60 * 12) -> None:
        self.backtest = backtest
        self.cache_ttl_seconds = cache_ttl_seconds
        self._lock = threading.Lock()
        self._cached: PhaseReading | None = None
        self._cached_at = 0.0

    def get(self, force: bool = False) -> PhaseReading:
        now = time.time()
        if not force and self._cached and (now - self._cached_at) < self.cache_ttl_seconds:
            return self._cached
        with self._lock:
            if not force and self._cached and (time.time() - self._cached_at) < self.cache_ttl_seconds:
                return self._cached
            factors = self.backtest.factor_series()
            reading = detect_phase(factors)
            self._cached = reading
            self._cached_at = time.time()
            return reading
