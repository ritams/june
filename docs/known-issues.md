# Known Issues & Caveats

Things Daniel should know about the Phase 4 build before going live. None are blockers — they are documented limitations and edge cases worth being aware of.

## 1. Credit spreads only have ~3 years of history

**Symptom:** The `BAMLH0A0HYM2` series (BofA US High Yield Option-Adjusted Spread) returns data only from 2023-04-25 onward.

**Why:** ICE/BofA changed their FRED data licensing in April 2026. From the FRED metadata: *"Starting in April 2026, this series will only include 3 years of observations. For more data, go to the source."*

**Impact:**
- The "Credit Stress" backtest signal works, but with a smaller event sample than the other signals.
- The credit-spreads metric on the live dashboard still updates daily — only the deep historical view is truncated.
- Did NOT affect the Phase 4 correlation matrix (we removed `credit_spreads` from the `dropna` subset so the other 6 factors retain full history back to 2000).

**Fix options if it matters later:**
- Swap to `BAMLC0A0CM` (BofA US Corporate Index) which has longer history but is investment-grade rather than high-yield.
- Scrape ICE directly (requires licensing).
- Use `BAA10Y` (Moody's BAA Corporate vs 10yr Treasury) as a longer-history credit-stress proxy.

## 2. Style and Crypto buckets are tiny

**Symptom:** The "Style factors" bucket has only 3 assets (IWF, IWD, MTUM) and the "Crypto" bucket has only 1 (BTC). Top 3 = Bottom 3 in these buckets, which looks redundant in the UI.

**Why:** This is what the asset list contains.

**Fix:** Add more tickers. Suggested additions:
- Style: `QUAL` (quality), `USMV` (low volatility), `SIZE` (small cap factor)
- Crypto: `ETH-USD` (Ethereum), `SOL-USD` (Solana). These would need a longer-history caveat similar to BTC's "since 2014" note.

Add them to `ASSET_SPECS` in `app/services/backtest.py`, then run `POST /api/actions/recalculate-correlations`.

## 3. First page load after fresh install takes ~30 seconds

**Symptom:** On a clean install with no `runtime/correlation_matrix.json`, the scenario panel shows "Cache pending" and an empty-state CTA telling the user to click **Recalculate matrix**.

**Why:** The correlation matrix downloads ~45 yfinance tickers (39 assets + 3 benchmarks + 1 cash proxy + 1 internal DXY) and computes the full 7-factor × 44-asset matrix. Cold build is 60–90 seconds depending on yfinance latency.

**Workaround:** The matrix is built async on app startup (`correlation_service.ensure_cache_async()`), so by the time Daniel opens the page after `launchctl kickstart`, it should usually be ready. If not, the empty-state message guides him to click the recalc button.

**Subsequent loads:** Instant. The matrix is cached at `runtime/correlation_matrix.json` and recalculated monthly (1st of each month at 06:30).

## 4. Monthly cron job not yet observed firing in production

**Symptom:** The matrix-recalc cron job (`day=1, hour=6, minute=30`) is wired and tested via the manual `/api/actions/recalculate-correlations` endpoint, but the cron trigger itself only runs on the 1st of the month so we have not directly observed it firing.

**Why:** Confidence is high — APScheduler is the same library already running the weekly Sunday backtest job successfully — but it has not yet had a "1st of the month" to prove itself.

**What to check on May 1st 2026:**
```bash
# Verify the matrix file timestamp is fresh
ls -la /Users/ritam/workspace/services/daniel/june/runtime/correlation_matrix.json

# Check the launchd stdout log for any APScheduler errors
tail -50 /Users/ritam/workspace/services/daniel/june/runtime/launchd.stdout.log
```

If it didn't fire, run the manual endpoint and investigate the scheduler logs.

## 5. Auto-fill scenario uses 5-yr rolling z-score

**Symptom:** The auto-fill values you see on page load are not Bittel's exact normalisation method — we picked `5-yr rolling z-score clipped to ±1` as a sensible default, per the locked decision in `docs/phase-4-plan.md`.

**Why:** Bittel doesn't publish his exact scenario-input mapping. This is a heuristic. It's defensible (5y captures the full post-COVID regime) but not gospel.

**If you want to change it:** edit `app/services/scenario_inputs.py:14` (`_ZSCORE_WINDOW_MONTHS`) or replace the function entirely.

## 6. Training window is full-history exponentially-weighted, not a hard 2008 cutoff

**Symptom:** The original `docs/next-build.md` spec said `CORRELATION_START = "2008-01-01"` — pre-2008 data should be excluded entirely. What we shipped uses **full history with exponential time-weighting (~10y half-life)** instead.

**Why we deviated:** Locked in `docs/phase-4-plan.md` decision #8, based on the Bittel transcript: *"I'm more heavily weighting the performance and all the stats to more recent than than historical but I still want that historical data within the model."* So full history with recent-bias matches Bittel; the hard 2008 cutoff matches Dan's original spec.

**Practical impact:** small. Most of the asset universe (currency ETFs, BIL, COPX, MTUM, etc.) launched after 2008 anyway, so for those buckets the two methods produce essentially identical numbers. The difference shows up most for SPY/QQQ/XL-sector ETFs and macro factors with deep history — pre-2008 observations still nudge the math, but contribute ~16× less than today's data.

**If Dan wants the literal spec** (hard 2008 cutoff), one-line change in `app/services/correlations.py` `_compute_cell`:

```python
aligned = aligned[aligned.index >= "2008-01-01"]
```

Then run `POST /api/actions/recalculate-correlations` to rebuild the matrix.

## 7. Composite score is `expected_return × avg_t_stat`

**Symptom:** The "Composite" column on each bucket is the headline ranker.

**Why:** This is the formula in the original `docs/next-build.md` spec. It's a heuristic — averaging T-stats across factors is statistically loose. The transcript suggests Bittel exposes both rankings (return + significance) separately.

**What we ship:** Both rankings are available in the `/api/scenario` response (`top_by_return`, `top_by_significance`, plus `composite`/`top_3`/`bottom_3`). The frontend currently only renders the composite top/bottom — the dual rankings are exposed via API for future UI work.

## Handover steps

```bash
# 1. Pull latest from this branch
cd /Users/ritam/workspace/services/daniel/june
git pull

# 2. Sync deps (numpy was made explicit; playwright was added to dev deps)
uv sync

# 3. Restart launchd so the new code is picked up
launchctl kickstart -k gui/$(id -u)/com.june.dashboard

# 4. Smoke check
curl http://127.0.0.1:8000/api/health
open http://127.0.0.1:8000/business-cycle
```

If the scenario panel says "Cache pending", click **Recalculate matrix** once and wait ~60 seconds.
