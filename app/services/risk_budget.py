"""Risk Budget — single 0..100 score and 5-band stance per build-28th-may.md §3-4.

Formula (verbatim from the spec):
  Risk Budget =
    30% Liquidity
    25% Cycle / Growth
    15% Risk Appetite
    10% Dollar
    10% Rates
    10% Inflation / Oil

Inputs arrive as a dict of -1..+1 floats (the same shape `auto_fill_scenario()`
returns from `app/services/scenario_inputs.py`). Sign convention:

  liquidity     +   (rising M2 supports risk)
  growth        +   (rising ISM supports risk)
  risk_on_off   +   (rising SPY MoM = appetite up)
  dollar        -   (rising DXY hurts risk)
  short_rates   -   (rising rates hurt risk)
  inflation/oil -   (rising inflation hurts risk)  -- combined as mean(inflation, oil)

The weighted sum lives in [-1, +1]; we shift to [0, 100] and round.

See docs/devlog-hermes-build.md §2 for the design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


WEIGHTS = {
    "liquidity":     0.30,
    "growth":        0.25,
    "risk_on_off":   0.15,
    "dollar":        0.10,   # inverted
    "short_rates":   0.10,   # inverted
    "inflation_oil": 0.10,   # inverted, derived from mean(inflation, oil)
}

# Verbatim from build-28th-may.md §4. Tuple: (lower_inclusive, upper_inclusive, stance, deploy_pct, cash_pct).
# deploy/cash are the midpoints of the spec's ranges (50-60 → 55, 70-80 → 75, 85-95 → 90).
BANDS: list[tuple[int, int, str, int, int]] = [
    (0,   20,  "Fortress Mode",         20, 80),
    (21,  40,  "Defensive",              35, 65),
    (41,  60,  "Cautious Risk-On",       55, 45),
    (61,  80,  "Constructive Risk-On",   75, 25),
    (81,  100, "Full Risk-On",           90, 10),
]


@dataclass
class RiskBudget:
    score: int                       # 0..100
    stance: str                      # "Fortress Mode" / "Defensive" / ...
    deploy_pct: int                  # whole percent
    cash_pct: int                    # whole percent
    confidence: str                  # "High" / "Medium" / "Low"
    components: dict[str, float] = field(default_factory=dict)   # signed, weighted contributions
    weighted_sum: float = 0.0
    inputs: dict[str, float | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "stance": self.stance,
            "deploy_pct": self.deploy_pct,
            "cash_pct": self.cash_pct,
            "confidence": self.confidence,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "weighted_sum": round(self.weighted_sum, 4),
            "inputs": {k: (round(v, 3) if v is not None else None) for k, v in self.inputs.items()},
        }


def _band_for(score: int) -> tuple[str, int, int]:
    for lo, hi, stance, deploy, cash in BANDS:
        if lo <= score <= hi:
            return stance, deploy, cash
    # score is clamped to [0,100] before this is called, so this is unreachable.
    return BANDS[2][2], BANDS[2][3], BANDS[2][4]


def _confidence(scenario: dict[str, float | None]) -> str:
    """Confidence reflects how broad-based the macro signal is.

    Counts factors with |value| >= 0.5 — i.e., factors that are clearly tilted
    rather than sitting near neutral. A high-conviction stance needs multiple
    factors agreeing; an all-neutral environment lands near 50 and should not
    be sold as a confident call.
    """
    strong = sum(1 for v in scenario.values() if v is not None and abs(v) >= 0.5)
    if strong >= 5:
        return "High"
    if strong >= 3:
        return "Medium"
    return "Low"


def compute(scenario: dict[str, float | None]) -> RiskBudget:
    """Compute Risk Budget from a 7-factor scenario dict (-1..+1 per factor).

    Missing factors are treated as 0 (neutral) so the score is always defined.
    """
    def _v(key: str) -> float:
        val = scenario.get(key)
        return 0.0 if val is None else float(val)

    inflation_oil = (_v("inflation") + _v("oil")) / 2.0

    components = {
        "liquidity":     WEIGHTS["liquidity"]     *  _v("liquidity"),
        "growth":        WEIGHTS["growth"]        *  _v("growth"),
        "risk_on_off":   WEIGHTS["risk_on_off"]   *  _v("risk_on_off"),
        "dollar":        WEIGHTS["dollar"]        * -_v("dollar"),
        "short_rates":   WEIGHTS["short_rates"]   * -_v("short_rates"),
        "inflation_oil": WEIGHTS["inflation_oil"] * -inflation_oil,
    }
    weighted_sum = sum(components.values())
    # weighted_sum ∈ [-1, +1] by construction (weights sum to 1, each factor ∈ [-1,+1])
    # → score = (w + 1) / 2 * 100
    raw_score = (weighted_sum + 1.0) / 2.0 * 100.0
    score = max(0, min(100, int(round(raw_score))))
    stance, deploy, cash = _band_for(score)

    return RiskBudget(
        score=score,
        stance=stance,
        deploy_pct=deploy,
        cash_pct=cash,
        confidence=_confidence(scenario),
        components=components,
        weighted_sum=weighted_sum,
        inputs={k: scenario.get(k) for k in ("liquidity", "growth", "risk_on_off", "dollar", "short_rates", "inflation", "oil")},
    )


def band_for_score(score: int) -> dict[str, Any]:
    """Public helper — used by the framework portfolio backtest to map a historical
    Risk Budget score to its stance and deploy/cash split."""
    s = max(0, min(100, int(round(score))))
    stance, deploy, cash = _band_for(s)
    return {"score": s, "stance": stance, "deploy_pct": deploy, "cash_pct": cash}
