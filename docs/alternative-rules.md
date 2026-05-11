# Alternative classifier rules (off by default, for reference)

This document preserves the rule sets that were active prior to 2026-05-12,
so we can roll back if the more-responsive defaults prove too noisy.

---

## 1. Phase classifier — 2-month confirmation rule (PREVIOUS DEFAULT)

**Where**: `app/services/phase.py`
**Previous value**: `CONFIRMATION_MONTHS = 2`
**Current value**: `CONFIRMATION_MONTHS = 1` (flip on the same month the direction agrees, subject only to ±0.25σ hysteresis)

### Why we had 2-month confirmation

Kritzman/Page/Turkington (2012 FAJ) showed that single-month direction rules
produce whipsaw at regime inflection points. The 2-month rule mimicked their
HMM persistence finding cheaply. The cost: real regime changes lag by ~1–2
months.

### Why we dropped it

Dan flagged that the dashboard reading "Phase: Winter" while growth and
inflation were both rising (proposed: Summer) was confusing, even with the
"Pending · proposed Summer" badge. Bittel and Steno are publicly calling
Macro Summer; the dashboard needs to mirror the live macro narrative.

### How to restore

In `app/services/phase.py`:
```python
CONFIRMATION_MONTHS = 2  # was 1
```

Tests in `tests/test_phase_detector.py::test_one_month_of_new_phase_does_not_flip`
specifically exercise this rule — they will fail when reverting and must be
re-enabled (currently they assert the more-permissive behavior).

---

## 2. Master signal — conjunctive (AND) rule (PREVIOUS DEFAULT)

**Where**: `app/services/dashboard.py::summarize_section`

### Previous logic

```python
# Liquidity EXPANDING required ALL of:
m2_mom > 0.3 AND rrp_direction == "falling" AND dxy < 101

# Liquidity CONTRACTING required:
dxy > 104 OR m2_mom < 0

# Cycle EXPANSION required ALL of:
ism > 52 AND yield_curve > 0 AND spreads < 350
```

### Why we had conjunctive rules

High specificity — RISK ON only fires when every supporting condition aligns.
Reduces false positives in choppy regimes.

### Why we dropped it

Dan: "Should escalate to RISK ON when ≥80% of metrics positive." Conjunctive
rules can hold the signal SELECTIVE for weeks even when most metrics are
clearly green — e.g. M2 MoM 0.26% (just below the 0.3% threshold) kept
Liquidity NEUTRAL despite DXY weak, RRP draining, Fed balance-sheet stable,
TGA spending, Global M2 expanding (5 of 6 positive).

### How to restore

Replace `summarize_section` body with the explicit conjunctive checks
documented above. Keep the count-based logic available as an alternate path.

### Current count-based thresholds

| Section | Status | Condition |
|---|---|---|
| Liquidity | EXPANDING | ≥ 80% of 6 metrics positive (5+/6) |
| Liquidity | CONTRACTING | ≥ 50% of 6 metrics negative (3+/6) |
| Liquidity | NEUTRAL | everything else |
| Cycle | EXPANSION | ≥ 80% of 5 metrics positive (4+/5) |
| Cycle | LATE CYCLE | exactly 1 of 5 negative (4/5 positive but not all) |
| Cycle | CONTRACTION | ≥ 40% of 5 metrics negative (2+/5) |
| Cycle | TRANSITION | everything else |

`overall_signal`:
- RISK ON: liquidity EXPANDING AND cycle EXPANSION
- RISK OFF: liquidity CONTRACTING OR cycle CONTRACTION
- SELECTIVE: everything else

---

## 3. Cold-start UX — silent "Calculating…" placeholder (PREVIOUS DEFAULT)

**Where**: `static/dashboard.html` initial markup + `static/djg.js`

### Previous behavior

The allocation panel showed "Calculating…" with no animation while
`/api/scenario?auto=true` was warming the FRED + yfinance caches (30–60s
on a cold server boot). Looked indistinguishable from a broken page.

### Why we dropped it

Dan opened the dashboard during cold-start and reported it as a render bug.

### How to restore

Remove the spinner overlay element from `static/dashboard.html`, the
`.djg-spinner-overlay` CSS in `static/djg.css`, and the spinner show/hide
logic in `static/djg.js`.

---

## Rollback procedure

To restore any of the above to its previous form:

1. Read the **How to restore** section.
2. Make the listed code change.
3. Run `uv run pytest -q` and fix any test failures (some tests are pinned to
   the current rule set and need their assertions inverted).
4. Update this doc's "current/previous" labels so it stays an accurate record.
5. Update `docs/djg-design-decisions.md` if a rule change overturns the
   rationale recorded there.
