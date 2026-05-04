# Phase 4 Plan — Bittel MIT Macro Model

This is the build contract. Anything not in here is out of scope. Red-line before code starts.

Sources reconciled:
- `docs/next-build.md` — original spec
- `docs/bittel.md` — Bittel's video transcript explaining the actual MIT methodology

When the two disagree, the transcript wins (it is the source of truth for Bittel's method).

---

## Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | Phase 3 scope | Full — Z-scores on live metrics, T-stats on backtest results, new assets, cycle map (4-seasons quadrant), charts, bug-fix pass |
| 2 | Live snapshot → scenario input mapping | 5-yr rolling z-score per factor, clipped to ±1 |
| 3 | Regime-change alert trigger | Top-1 ranked asset flips OR any macro factor z-score crosses ±1 |
| 4 | Methodology | Bittel transcript method (see "Methodology" below) |
| 5 | Korean exports source | Perplexity (key already in `.env`) |
| 6 | LLM dependency | None for Phase 4. Anthropic key not required. |
| 7 | Asset universe | 39 tickers, exact list below |
| 8 | Training window for correlations | Full history per asset; performance stats exponentially weighted toward post-2008 |
| 9 | Cache cadence | Macro daily, correlation matrix monthly, scenario engine daily, regime-change alert event-driven |

---

## Methodology (from `docs/bittel.md`)

Six elements that go beyond the original `next-build.md` spec:

1. **Dual ranking.** Every asset is ranked two ways under a scenario:
   - **Performance rank** — expected return = `Σ (factor_value × bull_or_bear_return)` averaged across factors
   - **Significance rank** — average T-stat (or Z-score) across factors
   These often disagree and the disagreement is information. Composite score = `expected_return × avg_t_stat` is the headline tie-breaker.

2. **Absolute vs relative correlations.** Different asset buckets are correlated against different baselines:
   - Asset classes (equities/credit/commodities/cash/bonds/crypto) → **absolute returns**
   - Equity regions / sectors / style factors → **excess return vs MSCI ACWI** (proxy: `ACWI`)
   - Fixed income → **excess return vs Bloomberg Global Agg** (proxy: `AGG`)
   - Currencies → already vs USD (FX ETFs are already relative)
   - Commodities → **excess return vs S&P GSCI** (proxy: `GSG`)

3. **Time weighting, not a hard cutoff.** Use full available history for correlation/T-stat math. Apply exponential weighting on returns so post-2008 carries more weight (half-life: ~10 years).

4. **Nine pre-programmed scenarios** as one-click buttons:
   Spring, Summer, Fall, Winter, Dollar Wrecking Ball, Tightening+Rate Hikes, Easing+Rate Cuts, Oil Shock, Market Melt-Up.

5. **Heat map view** — full asset × factor correlation grid, alongside Top 3 / Bottom 3.

6. **Six asset classes including crypto** — fixes the "cash appears twice" quirk Bittel called out. Asset classes: equities, credit, commodities, cash, bonds, crypto.

---

## Macro factors (7 inputs)

| Factor | Proxy | Source | Status |
|---|---|---|---|
| Risk On/Off | Beta / directional view | User input | Manual only |
| Growth | ISM PMI | Existing (Perplexity / FRED `IPMAN`) | In place |
| Inflation | CPI YoY | FRED `CPIAUCSL` | **New** |
| Short-term rates | 2yr Treasury yield | FRED `DGS2` | **New** |
| Liquidity | Global M2 proxy + net liquidity | Existing (`M2SL`, `RRPONTSYD`, `WTREGEN`, `WALCL`) | In place |
| Dollar | DXY | Existing (`DX-Y.NYB` via yfinance) | In place |
| Oil | WTI direction | FRED `DCOILWTICO` | **New** |

Three new FRED series, no new credentials.

---

## Asset universe (39 tickers)

**Equity regions (10):** SPY, QQQ, IWM, EWJ, EWY, EFA, EEM, EWU, EWL, EWZ
**Equity sectors (9):** XLK, XLY, XLE, XLF, XLV, XLU, XLP, XLI, SMH
**Fixed income (5):** TLT, IEF, TIP, HYG, LQD
**Currencies (6):** FXA, FXC, FXB, FXF, FXY, UUP
**Commodities (5):** IAU, COPX, USO, DBA, DJP
**Style factors (3):** IWF, IWD, MTUM
**Crypto (1):** BTC-USD

**Benchmarks (added internally for relative-return calc):** ACWI, AGG, GSG

Known data caveats — accept and document, don't drop:
- BTC-USD only goes back to ~2014; correlations restricted to that window
- Currency ETFs (FXA/FXB/FXC/FXF/FXY) have inconsistent pre-2010 history
- COPX is copper-miners (equity), not copper spot — keep but label clearly

---

## Build order

| Phase | Deliverable | Est (agentic) |
|---|---|---|
| **3** | Z-scores helper, T-stats helper, integrate into existing snapshot + backtest output, 4-seasons quadrant component, chart wiring, bug-fix sweep | 6–10h |
| **4a** | Add `CPIAUCSL`, `DGS2`, `DCOILWTICO` to `_load_macro_data` | 0.5h |
| **4b** | Expand `ASSET_SPECS` to 39 tickers + 3 benchmarks; verify yfinance pulls; flag data gaps | 1–2h |
| **4c** | New file `app/services/correlations.py`: build matrix with Pearson corr, T-stat, bull/bear quartile returns, exponentially time-weighted. Cache to `runtime/correlation_matrix.json`. Absolute vs relative based on asset bucket. | 2–3h |
| **4d** | New endpoint `GET /api/scenario`: input = 7 factor values in [-1, +1]; output = dual ranking (return rank, significance rank, composite) per asset bucket + heat map data | 1h |
| **4e** | Frontend: scenario panel with 7 sliders, 9 pre-programmed scenario buttons, Top 3 / Bottom 3 per bucket, full heat map. Add to both `/liquidity` and `/business-cycle` pages. | 4–6h |
| **4f** | Auto-fill scenario inputs from live snapshot (5-yr rolling z-score per factor, clipped ±1) | 1h |
| **4g** | Scheduler: monthly correlation matrix recalc; regime-change-only Telegram alert filter (Top-1 flip OR factor z-score crosses ±1) | 1h |
| **Test** | Browser smoke test, hand-verify a few correlation cells, end-to-end scenario flow | 2–3h |
| **Total** | | **18–27h** |

---

## Definition of Done

Phase 3:
- [ ] Z-scores rendered next to every live metric on both dashboard pages
- [ ] T-stats rendered next to every backtest forward-return cell
- [ ] 4-seasons quadrant visual on `/business-cycle`
- [ ] At least one chart per page (price + indicator overlay)
- [ ] Open bug list cleared

Phase 4:
- [ ] 3 new FRED series live in macro data layer
- [ ] 39 assets + 3 benchmarks pulling cleanly from yfinance
- [ ] Correlation matrix built for all (factor × asset) pairs
- [ ] Matrix cached at `runtime/correlation_matrix.json`, refreshed monthly
- [ ] `GET /api/scenario` returns dual ranking + heat map for any 7-factor input
- [ ] Composite score = `expected_return × avg_t_stat`
- [ ] Top 3 / Bottom 3 displayed per asset bucket on both dashboard pages
- [ ] 7 sliders + 9 pre-programmed scenario buttons on frontend
- [ ] Auto-fill from live snapshot on page load
- [ ] Full heat map view (asset × factor grid)
- [ ] Telegram fires on Top-1 flip or factor z-score crossing ±1, never on routine refresh

---

## Out of scope (explicitly)

- LLM-based features (no Anthropic dep this phase)
- PDF / transcript ingestion (Fred research agent — separate workstream)
- Portfolio sizing or order generation (allocation engine outputs ranked lists, not weights)
- Public-facing SaaS hardening (auth, multi-tenant, billing)
- Bittel-style "% of countries scoring in each quadrant" growth table — defer
- Any analyst other than Bittel for this phase

---

## Open risks

1. **T-stat denominator** — formula in original spec divides by `sqrt(1 - corr²)`. Guard against `|corr| → 1` (clip to corr ∈ [-0.999, 0.999]).
2. **Composite score is a heuristic** — Bittel's transcript shows performance and significance ranks separately and lets the user reconcile. Headline composite is convenience, not gospel.
3. **Uneven panels** — BTC starts 2014, currency ETFs patchy pre-2010. Use available data per pair; report `n` with every cell so confidence is visible.
4. **Live snapshot → factor mapping** — 5-yr rolling z-score is a sensible default but not Bittel-canonical. Revisit if outputs don't match Bittel's published positioning.
5. **Monthly cadence vs crypto** — BTC moves on shorter timescales than monthly macro. Acceptable trade-off for Phase 4; revisit if Daniel wants crypto-specific shorter-cadence overlays.

---

## What we are NOT changing

- Existing `/liquidity`, `/business-cycle`, `/dashboard` page structure stays. Scenario panel is additive.
- Existing 7 backtest signals stay. Phase 3 just decorates them with T-stats.
- Existing scheduler, alert, and Sheets logging unchanged except for the additions in 4g.
- `runtime/backtest_results.json` schema unchanged. New `runtime/correlation_matrix.json` is additive.

---

## Sign-off

Build does not start until this doc is acknowledged. Red-line by editing inline or replying with deltas.
