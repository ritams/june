# Statistical Significance and Dual Ranking

This note explains how to make the Phase 4 backtests more statistically useful and how to expose Bittel-style dual ranking in the scenario engine.

It is not a new build contract. It is the implementation guide for improving confidence without changing the intent of `docs/phase-4-plan.md`.

---

## Current state

The app has two related but separate statistical surfaces:

1. **Backtest playbook**
   - Code: `app/services/backtest.py`
   - Cache: `runtime/backtest_results.json`
   - Output: forward returns after named macro signal transitions.
   - Statistical field: `t_stat` on forward returns.

2. **Phase 4 scenario engine**
   - Code: `app/services/correlations.py`
   - Cache: `runtime/correlation_matrix.json`
   - Output: macro factor × asset correlations, expected returns, T-stats, rankings, and heat map.
   - Statistical field: `t_stat` on weighted factor/asset correlations.

The scenario API already returns:

- `top_3` / `bottom_3`: composite rank.
- `top_by_return`: performance rank.
- `top_by_significance`: significance rank.

The frontend currently displays only the composite `top_3` / `bottom_3` lists.

---

## What "statistically significant" means here

Do not try to "make" a result significant by loosening thresholds until it passes. The goal is to make the test better and then let weak results stay weak.

Use these labels consistently:

| Label | Minimum bar | Meaning |
|---|---:|---|
| Anecdotal | `n < 10` | Useful as a case study only. Do not rank conviction from it. |
| Thin | `10 <= n < 20` | Directional read. Needs confirmation from other evidence. |
| Usable | `n >= 20` and `abs(t_stat) >= 2` | Good enough for dashboard display. |
| Strong | `n >= 30` and `abs(t_stat) >= 2.5` | Good enough to emphasize in UI. |
| Robust | `n >= 50` and `abs(t_stat) >= 3` | High-confidence relationship, still subject to regime drift. |

For the current dashboard, a practical rule is:

```text
show = n >= 10
mark_as_significant = n >= 20 and abs(t_stat) >= 2
mark_as_strong = n >= 30 and abs(t_stat) >= 2.5
hide_or_demote = n < 10
```

Why this matters:

- Monthly macro data has limited observations.
- Forward-return windows overlap, especially 1yr / 18m / 2yr windows.
- Some assets have short histories, especially `BTC-USD`, `MTUM`, `COPX`, and several currency ETFs.
- Rare transition signals can have low event counts even with decades of price history.

So `t_stat` should be treated as a confidence score, not a guarantee.

---

## How to get more statistically useful backtests

### 1. Separate "more data" from "more events"

The current backtest playbook fires on **transition dates**. Example: yield curve crosses from inverted to positive.

That means more asset price history does not automatically create many more events. A rare macro event remains rare.

There are two ways to increase sample size:

1. Extend history with longer asset/macro proxies.
2. Change the test design from transition events to regime-month samples.

Both are valid, but they answer different questions.

### 2. Use longer-history proxies

ETF history is convenient but short. For deeper tests, use index/factor/total-return series where possible.

Recommended proxy upgrades:

| Current | Problem | Better backtest proxy |
|---|---|---|
| `SPY` | starts in 1993 | S&P 500 total return / broad US equity return series |
| `QQQ` | starts in 1999 | Nasdaq / technology sector total return proxy |
| `IAU` | starts in 2005 | gold spot or futures continuous return |
| `TLT`, `IEF`, `TIP` | ETF era only | model bond returns from constant-maturity yields |
| sector ETFs | mostly 1998+ | Fama-French industry portfolios |
| style ETFs | short/inconsistent | Fama-French style/factor portfolios |
| `BTC-USD` | starts in 2014 | keep as-is; do not synthesize pre-BTC history |

This improves `n` for older macro cycles without pretending that short-history ETFs existed earlier.

### 3. Replace or supplement credit-spread history

The current `Credit Stress` signal can be starved if `BAMLH0A0HYM2` history is unavailable or truncated.

Practical options:

1. Use `BAA10Y` as a longer FRED credit-spread proxy.
2. Use `BAMLC0A0CM` for investment-grade corporate OAS if high-yield history is unavailable.
3. Use licensed ICE/BofA history only if Daniel has redistribution/use rights.

If switching proxies, avoid fixed `500 bps` thresholds. Use percentiles or z-scores:

```text
credit_stress = credit_spread_zscore >= 1.5
or
credit_stress = credit_spread >= rolling_10y_90th_percentile
```

That keeps the signal portable across different spread series.

### 4. Add a regime-sample backtest mode

Transition-event tests answer:

```text
What happened after the signal first flipped on?
```

Regime-sample tests answer:

```text
What happened during every month this regime was active?
```

This can materially increase observations for `RISK ON`, `RISK OFF`, `Dollar Weakness`, and `M2 Acceleration`.

Implementation outline:

1. Keep existing transition tests unchanged.
2. Add a second mode in `BacktestService`, for example:

```python
def calculate_regime_forward_returns(
    active_months: pd.Series,
    asset_prices: pd.Series,
    horizons: list[int],
) -> dict[str, dict[str, float | int]]:
    event_dates = list(active_months.index[active_months.fillna(False)])
    return calculate_forward_returns(event_dates, asset_prices, horizons)
```

3. Store both modes in the cache:

```json
{
  "signals": {
    "risk_off": {
      "transition_results": {},
      "regime_results": {}
    }
  }
}
```

4. Display transition results as the default, with regime results as a secondary confidence panel.

Caveat: monthly regime samples create overlapping forward windows. The simple `t_stat` will overstate precision. If this mode becomes important, add bootstrap confidence intervals or Newey-West adjusted T-stats.

### 5. Add confidence intervals or bootstrap checks

The current `t_stat_from_returns()` is simple and readable. For a more robust version:

- Bootstrap event returns to estimate a confidence interval around average return.
- Use block bootstrap for monthly regime samples.
- Add a `ci_low` / `ci_high` field to cached results.

Suggested cache extension:

```json
{
  "avg": 20.2,
  "win_rate": 0.90,
  "n": 30,
  "t_stat": 6.75,
  "ci_95_low": 12.1,
  "ci_95_high": 28.7,
  "significance": "strong"
}
```

### 6. Use point-in-time macro data where possible

Historical macro series can be revised. For serious backtesting, use ALFRED vintages for CPI, employment, claims, and other revised macro data.

This does not increase sample size, but it avoids look-ahead bias.

---

## Dual ranking vs dual weighting

Bittel-style **dual ranking** is not the same thing as a weighted blend.

Dual ranking means showing two separate rankings:

1. **Performance rank**
   - Sort by `expected_return`.
   - Answers: "What historically made the most money in this scenario?"

2. **Significance rank**
   - Sort by `avg_t_stat`.
   - Answers: "Which relationship is historically most reliable?"

The current composite rank is:

```text
composite_score = expected_return * avg_t_stat
```

That answers:

```text
What has a good return and a good statistical relationship?
```

Composite is useful, but it should not replace the two separate views.

---

## How to expose the existing dual ranking in the UI

No backend change is required for the basic version.

The API already returns this per bucket:

```json
{
  "top_3": [],
  "bottom_3": [],
  "top_by_return": [],
  "top_by_significance": []
}
```

Frontend path:

- File: `static/app.js`
- Current renderer: `renderBuckets(buckets)`
- Current lists: `bucket.top_3` and `bucket.bottom_3`

Minimal UI change:

1. Keep `Own these` / `Avoid these` as the composite lists.
2. Add two compact lists under each bucket:

```javascript
const byReturn = bucket.top_by_return || [];
const bySignificance = bucket.top_by_significance || [];
```

3. Render:

```text
Best return
1. BTC +248.9%
2. QQQ +20.2%
3. IAU +11.4%

Strongest signal
1. QQQ t +6.75
2. IAU t +3.34
3. BTC t +3.10
```

This matches the Phase 4 spec without changing the model.

---

## How to add true dual weighting

If Daniel wants adjustable weights, do not weight raw return and raw T-stat directly. They are on different scales.

Use bucket-relative percentile ranks:

```text
return_rank_score = percentile_rank(expected_return within bucket)
significance_rank_score = percentile_rank(avg_t_stat within bucket)
dual_weighted_score =
  return_weight * return_rank_score +
  significance_weight * significance_rank_score
```

Default:

```text
return_weight = 0.50
significance_weight = 0.50
```

Return-focused:

```text
return_weight = 0.70
significance_weight = 0.30
```

Confidence-focused:

```text
return_weight = 0.30
significance_weight = 0.70
```

Why percentile ranks:

- `expected_return` can range from small single digits to triple digits for BTC.
- `avg_t_stat` usually ranges from 0 to 6.
- Raw weighted sums would let BTC-scale returns dominate the result.
- Percentiles keep the blend interpretable.

Backend implementation path:

1. Add optional query params to `/api/scenario`:

```python
return_weight: float = 0.5
significance_weight: float = 0.5
```

2. Pass them into:

```python
correlation_service.rank_scenario(
    scenario_input,
    return_weight=return_weight,
    significance_weight=significance_weight,
)
```

3. In `CorrelationService.rank_scenario()`, after building each bucket, compute percentile scores:

```python
def _percentile_scores(items, key):
    ordered = sorted(items, key=lambda item: item[key])
    if len(ordered) <= 1:
        return {ordered[0]["key"]: 1.0} if ordered else {}
    return {
        item["key"]: idx / (len(ordered) - 1)
        for idx, item in enumerate(ordered)
    }
```

4. Add fields per asset:

```python
entry["return_rank_score"] = return_scores[entry["key"]]
entry["significance_rank_score"] = significance_scores[entry["key"]]
entry["dual_weighted_score"] = (
    return_weight * entry["return_rank_score"]
    + significance_weight * entry["significance_rank_score"]
)
```

5. Sort an additional list:

```python
bucket["top_by_dual_weight"] = sorted(
    bucket["assets"],
    key=lambda x: x["dual_weighted_score"],
    reverse=True,
)[:3]
```

6. Keep the original `composite_score` and `top_3` fields for backward compatibility.

Frontend implementation path:

1. Add two range controls near the scenario sliders:

```text
Return weight: 50%
Confidence weight: 50%
```

2. Ensure they sum to 100%.
3. Include them in the scenario query:

```javascript
return_weight=${scenarioState.returnWeight}
significance_weight=${1 - scenarioState.returnWeight}
```

4. Render `top_by_dual_weight` as an optional list or replace the composite list only when the user changes weights.

Recommended UX:

- Default view: composite `Own these` / `Avoid these`.
- Expandable detail: `Best return`, `Strongest signal`, `Weighted blend`.
- Do not make weighted blend the only visible ranking until Daniel is comfortable with it.

---

## Recommended next build order

1. Add significance labels to backtest cache output.
2. Add `BAA10Y` as a credit-stress fallback or replacement.
3. Add regime-sample mode alongside transition-event mode.
4. Expose `top_by_return` and `top_by_significance` in the frontend.
5. Add optional dual-weighted rank if Daniel wants an adjustable blend.
6. Add bootstrap confidence intervals if regime-sample mode becomes a decision input.

The key discipline: more samples are good only when they represent a clean test. Do not mix incompatible proxies, do not synthesize fake asset history, and do not hide low `n`.
