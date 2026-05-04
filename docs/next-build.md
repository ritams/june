June Dashboard — Phase 4: Bittel MIT Model

Goal

Emulate Julian Bittel's MIT macro model inside June. Takes 6 macro factor inputs, calculates historical correlations with all asset classes using Z-scores + T-stats, outputs ranked TOP 3 / BOTTOM 3 for any macro scenario.

Additive to existing code — nothing gets thrown away. Phase 3 first, then this.

───

The 6 Macro Input Factors

| Factor           | Proxy            | Source                  |
| ---------------- | ---------------- | ----------------------- |
| Risk On/Off      | Beta/directional | Existing signal         |
| Growth           | ISM PMI          | Already in June         |
| Inflation        | CPI YoY          | CPIAUCSL (FRED) — new   |
| Short-term rates | 2yr yield        | DGS2 (FRED) — new       |
| Liquidity        | Global M2        | Already in June         |
| Dollar           | DXY              | Already in June         |
| Oil              | WTI direction    | DCOILWTICO (FRED) — new |Add CPI, 2yr yield, and oil to _load_macro_data. Three new FRED series.

───

Full Asset Universe

Expand ASSET_SPECS:

Equity regions: SPY, QQQ, IWM, EWJ (Japan), EWY (Korea), EFA (Dev ex-US), EEM (EM), EWU (UK), EWL (Switzerland), EWZ (Brazil)

Equity sectors: XLK (Tech), XLY (Discretionary), XLE (Energy), XLF (Financials), XLV (Healthcare), XLU (Utilities), XLP (Staples), XLI (Industrials), SMH (Semis)

Fixed income: TLT, IEF, TIP (inflation-protected), HYG (high yield), LQD (investment grade)

Currencies (ETF proxies): FXA (AUD), FXC (CAD), FXB (GBP), FXF (CHF), FXY (JPY), UUP (dollar)

Commodities: IAU (gold), COPX (copper), USO (oil), DBA (agriculture), DJP (broad)

Style factors: IWF (growth), IWD (value), MTUM (momentum)

Crypto: BTC-USD

───

The Correlation Matrix

New file: app/services/correlations.py

Training data: 2008-01-01 onwards only. Pre-2008 is a different regime — no QE, different Fed behaviour, correlations aren't representative of the world we operate in now.

For every pair of (macro_factor × asset), calculate:

1. Pearson correlation
2. T-stat on that correlation
3. Average asset return when factor was in top quartile (bullish)
4. Average asset return when factor was in bottom quartile (bearish)

CORRELATION_START = "2008-01-01"

def build_correlation_matrix(assets, factors):
    results = {}
    for factor_key, factor_series in factors.items():
        results[factor_key] = {}
        for asset_key, asset_prices in assets.items():
            asset_returns = asset_prices.pct_change().dropna()
            aligned = pd.concat([factor_series, asset_returns], axis=1).dropna()
            if len(aligned) < 24:
                continue
            x = aligned.iloc[:, 0]
            y = aligned.iloc[:, 1]
            corr = float(x.corr(y))
            n = len(aligned)
            t = corr * math.sqrt(n-2) / math.sqrt(1 - corr**2)
            bull_returns = float(y[x >= x.quantile(0.75)].mean() * 100)
            bear_returns = float(y[x <= x.quantile(0.25)].mean() * 100)
            results[factor_key][asset_key] = {
                "correlation": round(corr, 3),
                "t_stat": round(t, 2),
                "n": n,
                "bull_return": round(bull_returns, 1),
                "bear_return": round(bear_returns, 1),
            }
    return resultsCache to runtime/correlation_matrix.json. Recalculate monthly.

───

The Scenario Engine

New endpoint: GET /api/scenario

Each factor input is -1 to +1 (bearish to bullish). Auto-populated from live snapshot, or manually adjustable via sliders.

Composite score = expected return × T-stat

This is the critical insight from Bittel: high confidence beats high return. An asset with +15% expected return at T-stat 1.0 ranks below one with +10% at T-stat 3.5.

def rank_assets(scenario, correlation_matrix):
    scores = {}
    for asset_key in ALL_ASSETS:
        weighted_return = 0
        weighted_t = 0
        count = 0for factor_key, factor_value in scenario.items():
            data = correlation_matrix.get(factor_key, {}).get(asset_key)
            if not data:
                continue
            expected = data["bull_return"] * factor_value if factor_value > 0 \
                       else data["bear_return"] * abs(factor_value)
            weighted_return += expected
            weighted_t += data["t_stat"]
            count += 1
        if count > 0:
            scores[asset_key] = {
                "expected_return": round(weighted_return / count, 1),
                "avg_t_stat": round(weighted_t / count, 2),
                "composite_score": round((weighted_return/count) * (weighted_t/count), 1),
            }
    ranked = sorted(scores.items(), key=lambda x: x[1]["composite_score"], reverse=True)
    return {"top_3": ranked[:3], "bottom_3": ranked[-3:], "full": ranked}───

Dashboard Display

Add "Scenario Analysis" section to both pages:

🎯 SCENARIO ANALYSIS
Current macro: Risk On | Growth ↑ | Liquidity Expanding | Dollar Weak

OWN THESE:
1. Semis (SMH)       +12.4%  T-stat 3.6 ✅ Strong
2. South Korea (EWY) +11.1%  T-stat 3.2 ✅ Strong
3. Emerging Mkts     +9.8%   T-stat 2.8 ✅ Reliable

AVOID THESE:
1. Dollar (UUP)      -8.3%   T-stat 3.1 ✅ Strong
2. Utilities (XLU)   -6.1%   T-stat 2.4 ✅ Reliable
3. Long Bonds (TLT)  -4.2%   T-stat 1.9 ⚠️ Moderate

Ranked by composite score (return × T-stat). Data: 2008–2026.Add manual scenario sliders so Dan can ask "what if liquidity drains?" and see the ranking shift live.

───

Storage

runtime/correlation_matrix.json  (new)

{
  "last_calculated": "2026-04-18",
  "training_start": "2008-01-01",
  "factors": {
    "liquidity": {
      "SMH": {"correlation": 0.68, "t_stat": 4.2, "n": 195,
              "bull_return": 8.3, "bear_return": -6.1},
      "EWY": {"correlation": 0.71, "t_stat": 4.8, "n": 195,
              "bull_return": 9.1, "bear_return": -7.2}
    }
  }
}───

Build Order

1. Phase 3 first — T-stats, Z-scores, new assets, cycle map, charts, bug fixes
2. Phase 4a — Add CPI, 2yr yield, oil to macro data layer
3. Phase 4b — Build correlations.py + cache
4. Phase 4c — /api/scenario endpoint + ranking engine
5. Phase 4d — Scenario display + manual sliders on frontend
6. Phase 4e — Auto-fill from live snapshot

───

Definition of Done

• [ ] 3 new macro factors: CPI, 2yr yield, WTI oil
• [ ] 30+ assets across all classes
• [ ] Correlation matrix built — all asset × factor pairs
• [ ] Training data: 2008 only
• [ ] Cached in runtime/correlation_matrix.json, monthly recalc
• [ ] /api/scenario endpoint
• [ ] Composite score = return × T-stat
• [ ] Top 3 / Bottom 3 on both dashboard pages
• [ ] Manual scenario sliders on frontend
• [ ] Auto-fill from live snapshot
• [ ] Z-scores on all live metrics
• [ ] T-stats on all backtest results




One addition to the Phase 4 spec — data cadence:

Macro is monthly. Bittel says it explicitly. 15-minute refreshes create noise, not signal. Here's the right cadence:

| Data                             | Refresh                  |
| -------------------------------- | ------------------------ |
| DXY, yield curve, credit spreads | Daily                    |
| M2, RRP, TGA, Fed balance sheet  | On FRED release (weekly) |
| ISM PMI, CPI, Korean exports     | On release (monthly)     |
| Correlation matrix               | Monthly                  |
| Backtest cache                   | Weekly (Sunday)          |
| Scenario engine output           | Daily                    |
| Telegram alert                   | On regime change only    |Three layers of dynamism:

1. Data refreshes daily
DXY, yield curve, credit spreads pull fresh each day. M2 and Fed data update on FRED release. ISM and CPI update on monthly release. No manual intervention needed.

2. Scenario auto-fills from live snapshot
When the macro state changes — DXY breaks above 102, ISM drops below 50, yield curve flips — the scenario inputs update, the ranking engine reruns, and the Top 3 / Bottom 3 shifts automatically. The dashboard reflects today's world every time you open it.

3. Correlation matrix recalculates monthly
As new data accumulates, the historical relationships update. If BTC becomes more sensitive to 2yr yields as adoption grows, the model picks it up. It learns over time.

Key instruction:
The scenario engine reruns daily with fresh data. The Top 3 / Bottom 3 updates automatically — no manual refresh needed. Telegram only fires when the regime actually changes — not on every refresh. No noise.

The dashboard is a live instrument, not a static report. Simple, clean, signal only. 👊