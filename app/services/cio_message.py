"""Weekly CIO Telegram message — deterministic template per build-28th-may.md §11.

Generates the Monday-morning message body and diffs against the prior week's
state (stored in `monitor_state.json` under key `hermes_weekly`). The future
GPT 5.5 Hermes agent can replace `_what_changed` and the closing "Hermes view"
line with richer prose; everything else stays deterministic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services.hermes_state import HermesState
from app.services.state import StateStore


# Factor labels & sign convention for human-readable commentary. "+" direction
# means the factor is supportive for risk; "-" means it's a headwind. This is the
# same convention the Risk Budget engine uses (cio devlog §2.2).
_FACTOR_DESCRIPTORS = {
    "liquidity":   {"label": "Liquidity (M2 YoY)",      "pos": "expanding",  "neg": "contracting"},
    "growth":      {"label": "Growth (ISM YoY)",        "pos": "rising",     "neg": "rolling over"},
    "risk_on_off": {"label": "Risk appetite (SPY MoM)", "pos": "firm",       "neg": "soft"},
    "dollar":      {"label": "Dollar (DXY)",            "pos": "weakening",  "neg": "strengthening", "invert": True},
    "short_rates": {"label": "Rates (2Y)",              "pos": "easing",     "neg": "tightening",    "invert": True},
    "inflation":   {"label": "Inflation",               "pos": "cooling",    "neg": "rebounding",    "invert": True},
    "oil":         {"label": "Oil",                     "pos": "stable/soft", "neg": "hot",          "invert": True},
}


def _factor_riders(inputs: dict[str, float | None]) -> list[str]:
    """Emit one short bullet per factor that is *clearly tilted* (|z| ≥ 0.5).

    These are deterministic facts — no LLM. They give the weekly message the
    "what's actually driving this" texture the spec asks for in §11.
    """
    bullets: list[str] = []
    for key, meta in _FACTOR_DESCRIPTORS.items():
        z = inputs.get(key)
        if z is None or abs(z) < 0.5:
            continue
        # "Risk-supportive" means z aligned with the supportive direction once we
        # account for the inverted factors (dollar/rates/inflation/oil).
        supportive = (z < 0) if meta.get("invert") else (z > 0)
        label = meta["label"]
        word = meta["pos"] if supportive else meta["neg"]
        bullets.append(f"{label} {word} (z={z:+.2f}).")
    return bullets


def _llm_what_changed(prev: dict[str, Any] | None, curr: HermesState) -> list[str] | None:
    """Try Hermes for richer 'what changed' prose. Returns None on any failure;
    caller falls back to the deterministic bullets below."""
    try:
        from app.services import hermes_llm as _hermes_llm
        if not _hermes_llm.available():
            return None
        curr_summary = {
            "risk_budget": curr.risk_budget,
            "stance": curr.stance,
            "liquidity_state": curr.liquidity_state,
            "cycle_state": curr.cycle_state,
            "macro_season": curr.macro_season,
            "factor_inputs": (curr.risk_budget_detail or {}).get("inputs", {}),
        }
        prose = _hermes_llm.ask(_hermes_llm.what_changed_prompt(curr_summary, prev))
        if not prose:
            return None
        bullets = [line.strip() for line in prose.splitlines() if line.strip()]
        # Normalize: every bullet starts with "- " (the renderer will skip the
        # prefix for any line that starts with whitespace).
        return [b if b.startswith(("- ", " ")) else f"- {b}" for b in bullets]
    except Exception:
        return None


def _what_changed(prev: dict[str, Any] | None, curr: HermesState) -> list[str]:
    """Bullet list of week-over-week deltas. Returns plain strings (no emoji).

    Tries Hermes LLM enrichment first; falls back to the deterministic
    template if Hermes is unavailable or the call fails.
    """
    llm = _llm_what_changed(prev, curr)
    if llm:
        # Strip the leading "- " we add later in render(); LLM output already
        # has bullets and render() prefixes one anyway.
        return [b.lstrip("- ").strip() if b.startswith("- ") else b for b in llm]
    bullets: list[str] = []

    if prev:
        prev_score = prev.get("risk_budget")
        if isinstance(prev_score, int):
            delta = curr.risk_budget - prev_score
            if delta != 0:
                direction = "rose" if delta > 0 else "fell"
                bullets.append(f"Risk Budget {direction} {prev_score} → {curr.risk_budget} ({delta:+d}).")

        prev_stance = prev.get("stance")
        if prev_stance and prev_stance != curr.stance:
            bullets.append(f"Stance changed: {prev_stance} → {curr.stance}.")

        prev_liq = prev.get("liquidity_state")
        if prev_liq and prev_liq != curr.liquidity_state:
            bullets.append(f"Liquidity state: {prev_liq} → {curr.liquidity_state}.")

        prev_cycle = prev.get("cycle_state")
        if prev_cycle and prev_cycle != curr.cycle_state:
            bullets.append(f"Cycle state: {prev_cycle} → {curr.cycle_state}.")

        prev_season = prev.get("macro_season")
        if prev_season and prev_season != curr.macro_season:
            bullets.append(f"Macro season turned: {prev_season} → {curr.macro_season}.")
    else:
        bullets.append("Initial print — no prior week to compare.")

    # Append factor commentary in BOTH cases (initial print and week-over-week)
    # so the message reads with texture rather than as a structured form.
    inputs = curr.risk_budget_detail.get("inputs", {}) if curr.risk_budget_detail else {}
    factor_lines = _factor_riders(inputs)
    if factor_lines:
        bullets.append("Driving factors:")
        bullets.extend(f"  · {line}" for line in factor_lines)

    if len(bullets) == 1 and bullets[0] == "Initial print — no prior week to compare.":
        # Edge case: no factors clearly tilted AND no prior — explicit fall-through.
        bullets.append("All factors near neutral — Risk Budget close to 50.")
    elif prev and not any(b.startswith(("Risk Budget", "Stance", "Liquidity state", "Cycle state", "Macro season")) for b in bullets[:5]):
        bullets.insert(0, "No regime-level changes this week. Trend holds.")
    return bullets


def _add_risk_if(stance: str) -> str:
    if stance in {"Fortress Mode", "Defensive"}:
        return "Liquidity turns positive, dollar weakens, rates ease, credit remains calm."
    return "Liquidity confirms (M2 momentum positive, dollar lower), cycle remains in expansion, credit spreads stay tight."


def _cut_risk_if(stance: str) -> str:
    if stance in {"Constructive Risk-On", "Full Risk-On"}:
        return "Liquidity deteriorates, dollar breaks higher, credit spreads widen, cycle rolls over."
    return "Liquidity contracts further, credit stress builds, growth turns lower."


def render(curr: HermesState, prev: dict[str, Any] | None) -> str:
    """Render the weekly Telegram message body. Pure text, no markdown."""
    bullets = _what_changed(prev, curr)
    # Sub-bullets (factor-rider lines) start with whitespace — we keep them
    # as-is rather than re-prefixing with "- " so the hierarchy reads cleanly.
    rendered_bullets = [b if b.startswith(" ") else f"- {b}" for b in bullets]

    lines = [
        "DJG HERMES CIO WEEKLY",
        "",
        f"Risk Budget: {curr.risk_budget} / 100",
        f"Stance: {curr.stance}",
        f"Deployment: {curr.deploy_pct}%",
        f"Cash Reserve: {curr.cash_pct}%",
        f"Macro Season: {curr.macro_season}",
        f"Liquidity: {curr.liquidity_state}",
        f"Cycle: {curr.cycle_state}",
        f"Confidence: {curr.confidence}",
        "",
        "What changed:",
        *rendered_bullets,
        "",
        "Action:",
        curr.summary,
        "",
        f"Add risk if: {_add_risk_if(curr.stance)}",
        f"Cut risk if: {_cut_risk_if(curr.stance)}",
        "",
        # build-28th-may.md §11 closes the weekly note with a "Hermes view:" line.
        # The deterministic version is keyed off stance; the LLM-enriched version
        # writes its own through hermes_llm.
        f"Hermes view: {_hermes_view(curr)}",
        "",
        f"MIT overlay: {curr.mit_overlay}",
        curr.slr_note,
    ]
    return "\n".join(lines)


def _hermes_view(curr: HermesState) -> str:
    """Closing one-liner per spec §11. Deterministic fallback when no LLM."""
    if curr.stance in {"Full Risk-On", "Constructive Risk-On"}:
        return (
            "The long-term exponential-age thesis remains intact, and the macro throttle "
            "is mostly green. Run risk."
        )
    if curr.stance == "Cautious Risk-On":
        return (
            "The long-term exponential-age thesis remains intact, but the macro throttle "
            "is not full green yet. Stay engaged, not aggressive."
        )
    return (
        "Capital preservation dominates the next leg. The exponential-age thesis still "
        "holds long-term, but the immediate signals say defence."
    )


def snapshot_for_store(curr: HermesState) -> dict[str, Any]:
    """The subset of state stored so next week can diff against it."""
    return {
        "risk_budget": curr.risk_budget,
        "stance": curr.stance,
        "liquidity_state": curr.liquidity_state,
        "cycle_state": curr.cycle_state,
        "macro_season": curr.macro_season,
        "saved_at": datetime.now(ZoneInfo("Europe/London")).isoformat(timespec="minutes"),
    }


def generate_and_persist(curr: HermesState, state_store: StateStore) -> str:
    """Render this week's message AND store the snapshot so next week can diff."""
    state = state_store.load()
    prev = state.get("hermes_weekly")
    message = render(curr, prev)
    state["hermes_weekly"] = snapshot_for_store(curr)
    state_store.save(state)
    return message
