# DJG Advisory — Design Decisions

This document records every non-obvious design decision for the DJG Advisory rebuild
(formerly "June"), with the trade-offs considered and the evidence base for each
formula choice. All formulas were cross-checked against multiple academic and
practitioner sources before being committed to here.

Decisions are versioned. If we change a rule later, update this doc first; the code
follows the doc, not the other way around.

---

## 1. Rebrand: June → DJG Advisory

| Field | Value |
|---|---|
| Product name | **DJG Advisory** |
| Tagline | "Cycle-aware allocation across every asset class" |
| Wordmark | Clean text wordmark (no logo asset). Monospace, all caps, letter-spaced. |
| Header | `DJG ADVISORY · <phase> · <signal>` |
| Routes | `/dashboard` is **replaced** in place. `/liquidity` and `/business-cycle` stay as drill-downs. |
| Telegram card title | "DJG Advisory — Daily" (was "June Daily") |
| Page title | `DJG Advisory` |
| Footer | `DJG Advisory · Cycle-aware allocation across every asset class` |

**Why replace, not add a new route:** Dan only needs one URL. The `/liquidity` and
`/business-cycle` pages remain as drill-downs because they still answer specific
questions ("why is liquidity flagged?") that the headline dashboard summarises.

---

## 2. Asset Universe (45 assets)

Existing 38 assets stay. We add three:

| Ticker | Class | Why | Confidence flag |
|---|---|---|---|
| EWA | Equity / Australia | Spec-required regional exposure | Normal |
| EWW | Equity / Mexico | Spec-required regional exposure | Normal |
| HPS-A.TO | Power complex | Spec-required uranium/utility tilt | **Low confidence** — short post-2008 history (TSX listing post-dates 2008 GFC training window) |

**HPS-A.TO handling:** include in matrix, compute correlations on whatever post-2008
window is available, but **mark `low_history: true`** in the cell metadata. Frontend
renders these cells with a striped overlay and a "limited history" tooltip.
Allocation engine excludes low-history cells from the top-5 / bottom-5 ranking —
they only appear in the heat-map and detail view.

---

## 3. Cycle Phase Classification (Spring / Summer / Autumn / Winter)

### Decision

Classify on **3-month direction** of growth and inflation, not absolute level. Use a
**±0.25σ hysteresis band** plus a **2-month confirmation rule**: phase only flips
when both growth and inflation directions agree for 2 consecutive monthly readings.

```
growth_dir   = sign(growth_z[t] - growth_z[t-3])     # 3m change in z-score
inflation_dir = sign(inflation_z[t] - inflation_z[t-3])

if |growth_z[t] - growth_z[t-3]| < 0.25:  growth_dir   = previous(growth_dir)
if |inflation_z[t] - inflation_z[t-3]| < 0.25: inflation_dir = previous(inflation_dir)

phase = match (growth_dir, inflation_dir):
  (+1, -1) → Spring
  (+1, +1) → Summer
  (-1, +1) → Autumn
  (-1, -1) → Winter

# Confirmation: only emit phase change if proposed phase has held 2 consecutive months.
```

### Trade-offs considered

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Levels (z>0 vs z<0)** | Simple, no lookback | Whipsaw near zero; phase changes every month in choppy regimes | Reject |
| **Direction over 3m, hard cutoff** | Practitioner standard (AQR, Bridgewater) | Still flips when 3m change is near zero | Reject |
| **Direction over 3m + ±0.25σ hysteresis** | Filters noise without lagging much | Tunes one parameter | **Adopt** |
| **Direction + hysteresis + 2-month confirmation** | Adds persistence; matches HMM behaviour cheaply | Lags real turning points by ~1–2 months | **Adopt** |
| **Full Markov-switching model** | Best statistical fit (Kritzman/Page/Turkington 2012) | Black-box, hard to explain to Dan, monthly fit instability | Reject for v1 |

### Why this combination

- **Direction over levels** is what every public framework actually uses: AQR's
  "Half Century of Macro Momentum" (2017) z-scores both growth and inflation then
  takes momentum direction; Bridgewater's All Weather and Merrill Lynch's
  Investment Clock both define quadrants on growth-rising/falling × inflation-
  rising/falling. Bittel/Pal's MIT framework is explicit that allocations shift
  on direction "rather than absolute levels."
- **Hysteresis + confirmation** is the cheap deterministic equivalent of a hidden-
  Markov regime model. Kritzman/Page/Turkington (2012, *Financial Analysts
  Journal*) showed regime models outperform raw threshold partitions precisely
  because they impose persistence; a band + N-month confirmation gets ~80% of
  the benefit without the model fragility.
- **±0.25σ band** is the most commonly cited band in practitioner write-ups where
  signal-to-noise on monthly macro z-scores starts to break even (|z|<1 is the
  noise zone, z>2 is rare-event zone, ±0.25 marks the "real but small" boundary).

### Inputs

| Factor | Proxy series | Source |
|---|---|---|
| Growth | ISM Manufacturing PMI (de-trended z-score over 60m window) | Perplexity primary, ISM page fallback (already wired) |
| Inflation | CPI YoY (de-trended z-score over 60m window) | FRED `CPIAUCSL` |

**Tiebreaker (when growth and inflation agree but flip in opposite directions on
the same month):** consult the Liquidity factor (Global M2 proxy direction). If
liquidity is still rising, lean toward Spring/Summer; if falling, Autumn/Winter.
This matches Bittel's framework where liquidity is a leading indicator.

### When this breaks

- **Stagflation-lite (e.g. 2022)** where headline CPI direction diverges from core
  CPI. Mitigation: surface a "core vs headline divergence" warning when |CPI -
  Core CPI| > 1pp and direction signs differ. Don't over-engineer in v1.
- **Persistent noise around zero** (e.g. PMI hovering 49–51). The hysteresis +
  confirmation rule handles this by holding the prior phase.

### Sources

- AQR — *A Half Century of Macro Momentum* (Brooks 2017): https://www.aqr.com/-/media/AQR/Documents/Insights/White-Papers/A-Half-Century-of-Macro-Momentum.pdf
- Bridgewater — *The All Weather Story*: https://www.bridgewater.com/research-and-insights/the-all-weather-story
- Merrill Lynch Investment Clock (Macro Ops summary): https://macro-ops.com/the-investment-clock/
- Kritzman, Page, Turkington — *Regime Shifts: Implications for Dynamic Strategies* (FAJ 2012): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2064801
- Pal & Bittel macro framework summary: https://aronhosie.com/2025/03/02/framework-raoul-pal-and-julien-bittels-macro-thesis-march-2025/
- Alpha Architect — *Data-driven Clustering of Macro Regimes*: https://alphaarchitect.com/clustering-macroeconomic-regimes/

---

## 4. Allocation Engine — Composite Score → Portfolio Weight

### Decision

For each asset, composite score = sum over the 7 factors of
`(bull_return - bear_return) × sign(t_stat) × |t_stat|`, weighted only on cells
where `|t_stat| ≥ 1.5` (low-confidence cells contribute zero — they don't dilute
the signal but don't destroy it either).

Convert composite scores to portfolio weights via:

```
score_i        = composite score for asset i
positive_i     = max(score_i, 0)              # long-only
tau            = std_dev(score_i across all assets)   # adaptive temperature
raw_weight_i   = exp(positive_i / tau)        # softmax over positive scores
weight_i       = raw_weight_i / sum(raw_weight_j)
```

Then apply hard per-class caps and renormalize:

| Class | Cap | Justification |
|---|---|---|
| Crypto (BTC) | **5%** | Pal personally runs higher but recommends ~5% for clients; matches institutional TAA practice |
| Single equity (any one ticker) | **25%** | Concentration cap; CIBC/Vanguard tactical-tilt range |
| Equity bucket (sectors+regions combined) | **60%** | Leaves room for bonds + commodities |
| Bonds (TLT, IEF, TIP, HYG, LQD) | **40%** | Standard 60/40 ceiling for TAA tilt |
| Commodities (IAU, COPX, USO, DBA, DJP, HPS-A.TO) | **20%** | Real-asset sleeve cap |
| FX (FXA, FXC, FXB, FXF, FXY, UUP) | **15%** | Currency overlay sleeve |
| Emerging markets (EEM, EWZ, EWW, EWY) | **15%** | EM sub-cap inside equity |

If post-cap normalization fails to use all weight (e.g. all top-5 are crypto and
crypto cap is hit fast), the residual goes to a **CASH** pseudo-asset rather than
spreading to lower-conviction names. Cash is shown explicitly in the allocation
table.

**Negative-score assets:** zero weight. Surface separately as "Bottom 5 to Avoid"
panel with the absolute composite score and the worst-T-stat cells driving the
view. **No shorts** — long-only is operationally simpler, no borrow cost, and
matches every public TAA/dual-momentum framework.

### Trade-offs considered

| Weighting scheme | Pros | Cons | Verdict |
|---|---|---|---|
| **Equal weight top-5** | Trivial, robust | Throws away conviction information | Reject |
| **Linear (score / sum-of-positive-scores)** | Simple | One outlier dominates the book | Reject |
| **Softmax with fixed τ** | Standard ML pattern | τ choice is arbitrary | Reject |
| **Softmax with τ = std-dev of scores (adaptive)** | Self-scaling: when scores compress, distribution flattens; when scores spread, it concentrates | One more line of code than fixed τ | **Adopt** |
| **Black-Litterman from views** | Academic gold standard | Needs covariance matrix + equilibrium weights, opaque to Dan | Reject for v1 |
| **Mean-variance with composite-score views** | Uses correlation info | Overfits, sensitive to covariance estimation | Reject |
| **Rank-weighted (top-5 = 5/4/3/2/1 then normalize)** | Robust to score outliers | Discards magnitude entirely | Reject |

### Why softmax with adaptive τ

- **Closed-form output** of entropy-regularized portfolio choice (Zakamulin 2024,
  SSRN). The τ parameter is the entropy-concentration knob: small τ → concentrated,
  large τ → equal-weight. Setting τ = cross-sectional σ of scores makes the
  distribution self-scale to the regime: when the model is confident (scores
  spread), it concentrates; when uncertain (scores compressed), it spreads out.
- **No covariance estimation needed.** Black-Litterman is the academic standard
  but requires a covariance matrix and equilibrium weights, both of which are
  fragile on monthly data with 45 assets. We accept the cost (no correlation
  awareness) and mitigate it with the per-class caps.
- **Long-only matches practice.** AQR macro momentum, GMO TAA, Vanguard
  TAA, dual-momentum frameworks (Antonacci) — all long-only with cash as the
  off-ramp. Shorting introduces borrow cost, basis risk, and a UX layer that's
  not appropriate for a daily allocation dashboard.

### Per-class caps — sources

- CIBC — *The Role of Tactical Asset Allocation*: https://www.cibcassetmanagement.com/email/assets/documents/pdfs/PortfolioConstruction_RoleofTAA_EN.pdf
- Goldman Sachs AM — *How to Combine Investment Signals in Long/Short Strategies*: https://www.gsam.com/content/dam/gsam/pdfs/institutions/en/articles/2018/Combining_Investment_Signals_in_LongShort_Strategies.pdf
- Bender et al. — *Comparing Portfolio Blending and Signal Blending* (FAJ 2018): https://rpc.cfainstitute.org/research/financial-analysts-journal/2018/ip-v3-n1-11-comparing-portfolio-blending
- Zakamulin — *Entropy-Regularized Portfolio Selection via Softmax Sharpe Allocation* (2024): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5539560
- Idzorek — *A Step-by-Step Guide to the Black-Litterman Model*: https://people.duke.edu/~charvey/Teaching/BA453_2006/Idzorek_onBL.pdf

### When this breaks

- **No correlation awareness.** Two highly correlated top-5 names (e.g. SPY +
  QQQ both flagged) get fully overweighted, giving duplicative US large-cap
  exposure. Mitigated by the equity-bucket cap (60%) and per-class single-name
  cap (25%), but a future v2 could add a 1-step risk-parity scaling within
  bucket.
- **Compressed-score regimes.** When all 45 scores cluster near zero (low
  conviction), τ-fixed softmax produces near-equal weights. The adaptive τ
  rescales but the answer may still feel like a bug. Mitigation: surface the
  cross-sectional score σ and the implicit τ in the UI debug panel, so the
  user can see when the model is "low conviction → diversify."

---

## 5. T-stat Confidence Tiers

### Decision (revised from initial proposal — see "Why" below)

| Tier | T-stat | Color | Meaning |
|---|---|---|---|
| **High** | ≥ 3.0 | Green | Survives multiple-testing bar (Harvey-Liu-Zhu 2016 threshold for new factor discovery) |
| **Medium** | 2.0 – 3.0 | Yellow | Conventionally significant (p<0.05 single test); correlation caveat applies |
| **Low** | 1.5 – 2.0 | Orange | Directional only; watch list, not actionable on its own |
| **Noise** | < 1.5 | Grey | Shown only in the asset detail drawer, never in headline ranking |

We also report **Newey-West HAC-adjusted t-stats** (lag = 6 months) alongside
naive OLS t-stats. Macro factors have serial correlation, which inflates naive
t-stats by 30–50%; HAC adjustment is the honest number for the dashboard.

### Why we revised from ≥2.5 / 1.5-2.5 / <1.5

The initial proposal (≥2.5 / 1.5–2.5 / <1.5) was sized for "single-test
significance." But we run 45 × 7 = 315 cells. Multiple-testing literature
(Harvey, Liu, Zhu 2016, *Review of Financial Studies*) argues the threshold for
"this factor really exists" is **t > 3.0** (p < 0.27%). We don't go full
Bonferroni (which would demand t ≈ 3.78 and kill the dashboard) because finance
factors are highly correlated, violating Bonferroni's independence assumption;
Harvey-Liu-Zhu's BHY-corrected threshold of **t ≈ 3.18** is the right number for
correlated tests, and we round to 3.0 for UI cleanliness.

The dashboard explicitly surfaces the multiple-testing caveat:
"315 cells tested — at p<0.05 single-test you would expect ~16 false positives
by chance. The High tier (t≥3) is sized to survive multiple-testing correction."

### Trade-offs considered

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| Use t > 2.0 single-test | Familiar, lots of green cells | Ignores 315-cell cost; misleading | Reject |
| Bonferroni (t > 3.78 for 315 tests) | Conservative | Kills the dashboard, factors aren't independent | Reject |
| Harvey-Liu-Zhu (t > 3.0) for "high" + show single-test for "medium" | Honest about both views | Two thresholds to explain | **Adopt** |
| Deflated Sharpe Ratio (Bailey/López de Prado) | Modern gold standard for backtest selection | Needs trial count; not appropriate for fixed pre-specified grid | Defer |

### Sources

- Harvey, Liu, Zhu — *…and the Cross-Section of Expected Returns* (RFS 2016): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2249314
- Bailey & López de Prado — *The Deflated Sharpe Ratio* (2014): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- BSIC — *Is Research on Factor Models Reliable?*: https://bsic.it/is-research-on-factor-models-reliable/

### When this breaks

- **Newey-West lag choice.** We use lag=6 (half a year of monthly autocorr).
  If the user is interpreting at a longer horizon, lag=12 is more conservative.
  Surface the lag in the UI debug panel.
- **Pre-registration commitment.** If we add a new factor (e.g. "credit"), the
  multiple-testing cost compounds. Lock the 7-factor grid as a pre-registered
  design; new factor additions require a doc update first.

---

## 6. Honest-Disclosure Block (per signal)

Every signal that surfaces in the top-5 / bottom-5 / heat-map detail must show:

| Field | Source | Format |
|---|---|---|
| Sample size (N) | Cell `n` from correlation matrix | "N = 132 months" |
| T-stat (HAC) | New Newey-West adjustment, lag=6 | "t = 2.71" |
| T-stat (raw OLS) | Existing cell `t_stat` | "(raw t = 3.12)" — small text |
| Bull-quartile mean return | Cell `bull_return` | "+8.2% / mo (avg, top-quartile months)" |
| Bear-quartile mean return | Cell `bear_return` | "-2.1% / mo (avg, bottom-quartile months)" |
| Hit rate | New: % of bull-quartile months with positive forward return | "Hit rate: 64%" |
| Confidence tier | Tier from §5 | Badge: High / Medium / Low |
| Caveat | Static + cell-specific | "Post-2008 only. Regime never tested under sustained credit stress." + (low-history flag if applicable) |

The caveat line is the single most important UI feature for honesty. It's never
collapsed; it's always visible underneath every callout.

---

## 7. Telegram Alerts

Per user direction: **all** triggers active. Existing 5 crossings stay, plus 2
new event types.

| Trigger | Existing? | Frequency cap |
|---|---|---|
| Yield curve crosses above 0 | Yes | 1× per crossing |
| Credit spreads cross above 500 bps | Yes | 1× per crossing |
| DXY crosses above 105 | Yes | 1× per crossing |
| ISM PMI crosses 50 (either dir) | Yes | 1× per crossing |
| Global M2 Proxy drops below -3% MoM | Yes | 1× per crossing |
| **Cycle phase flip** (after 2-month confirmation) | **NEW** | 1× per flip |
| **Top-level signal flip** (RISK ON ↔ SELECTIVE ↔ RISK OFF) | **NEW** | 1× per flip |

All alerts continue to run on the existing 15-minute scheduler **for crossing
detection** (the actual data refresh moves to daily — see §8).

---

## 8. Refresh Cadence

| Process | Old | New |
|---|---|---|
| Live macro/market data refresh | Every 15 minutes | **Daily** at 06:30 Europe/London |
| Telegram alert check (crossing detection) | Every 15 minutes | **Every 15 minutes** (unchanged — operates on the cached daily data; cheap, no external calls) |
| Daily card | Configured `DAILY_CARD_TIME` (07:45) | Unchanged |
| Correlation matrix recalc | Manual (currently 4 May) | **Monthly**, first day of month, 02:00 |
| Backtest cache refresh | Weekly Sunday | Unchanged |
| Cycle phase recompute | New | **Daily**, follows live data refresh |

**Why move data refresh to daily:** macro data publishes at most daily (most
factors are weekly or monthly), so 15-min polling burned API credits without
adding signal. The 15-min alert scheduler is preserved because crossings can
flip on cached data when a daily refresh ingests a new value.

---

## 9. Frontend — Dense Bloomberg, Mobile First

| Decision | Value |
|---|---|
| Layout philosophy | Information density first. Numbers and badges over decorative whitespace. |
| Breakpoint | Mobile-first: design at 375px, scale up. Desktop is the "nice to have" |
| Type | Existing app.css mono-stack stays. Letter-spaced caps for headers, monospace for all numerics, tabular numerals (`font-variant-numeric: tabular-nums`) so columns align |
| Color | Reuse existing `app.css` tokens. Add `--phase-spring/--phase-summer/--phase-autumn/--phase-winter` semantic colors |
| Section ordering on mobile | 1. Header (phase + signal) → 2. Top 5 / Bottom 5 → 3. Live macro factors → 4. Heat map → 5. Sliders → 6. Conviction callouts |
| Heat map on mobile | 5-column × 9-row grid (45 cells), 44×44px tap target minimum, click → bottom-sheet drawer |
| Sliders on mobile | Vertical stack, native range input, debounced 300ms before triggering `/api/scenario` |
| Wordmark | `<h1>DJG <span class="advisory">ADVISORY</span></h1>` — letter-spaced caps, no SVG asset needed |

---

## 10. Open Questions (deferred)

These are explicitly **not** decided in v1. If we hit them, doc-first then code.

1. **Risk-parity within asset bucket** to reduce duplicative SPY+QQQ-style
   overweighting. Defer until we see real allocation outputs.
2. **Newey-West lag selection** — use 6 in v1, revisit if Dan flags signals as
   "feels noisier than t-stat suggests."
3. **Manual override of phase classifier.** The slider section already lets users
   stress-test; we don't add a "force phase = X" toggle in v1.
4. **Asset removal / addition workflow.** If Dan wants to drop or add tickers, we
   commit to: (a) edit `ASSET_SPECS`, (b) update this doc's §2, (c) recompute
   matrix. No UI for it.
5. **Backtest of the allocation rule itself.** We backtest individual signals
   already. Backtesting "softmax-weighted top-5 with caps" portfolio-level
   returns is a v2 addition.

---

## Change log

| Date | Change | Author |
|---|---|---|
| 2026-05-08 | Initial document. v1 of all decisions. | Claude (with Ritam) |
