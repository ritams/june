JUNE → DJG Advisory — Phase 4 Spec for Ritam

Rebrand

• Change product name from "June" to "DJG Advisory"
• Update header, page title, footer, all UI references
• Tagline suggestion: "Cycle-aware allocation across every asset class"

───

Phase 4 — The Real Build

This is what makes DJG Advisory a genuine quant fund tool, not a single-signal dashboard.

Core Concept

Stop thinking "buy BTC when X." Start thinking "where do I allocate across 45 assets given the current cycle phase."

The dashboard should answer: "What's the optimal allocation right now, given the current macro regime?"

───

Asset Universe (45 assets, post-2008)

Equity Regions (10):
SPY, QQQ, IWM, EWJ, EWY, EFA, EEM, EWU, EWL, EWZ

Equity Sectors (9):
XLK, XLY, XLE, XLF, XLV, XLU, XLP, XLI, SMH

Fixed Income (5):
TLT, IEF, TIP, HYG, LQD

Currencies (6 ETF proxies):
FXA, FXC, FXB, FXF, FXY, UUP

Commodities (5):
IAU, COPX, USO, DBA, DJP

Style Factors (3):
IWF, IWD, MTUM

Crypto (1):
BTC-USD

+ EWA (Australia), EWW (Mexico), HPS-A.TO (power complex)

= 45+ assets

───

7 Macro Factors

1. Liquidity (M2, RRP, TGA, Fed BS, Global M2 proxy)
2. Growth (ISM PMI)
3. Inflation (CPI YoY)
4. Short-term rates (2yr yield)
5. Dollar (DXY)
6. Oil (WTI direction)
7. Risk regime (yield curve shape, credit spreads)

───

The Correlation Matrix

For every asset × factor pair, calculate:

• Pearson correlation
• T-stat (statistical significance)
• Bull quartile return (when factor strong)
• Bear quartile return (when factor weak)

Training data: 2008-01-01 onwards (post-GFC regime only)

Cache result in runtime/correlation_matrix.json. Recalculate monthly.

───

The Scenario Engine

Endpoint: GET /api/scenario

For any input scenario (auto-derived from current dashboard state OR manually adjusted by user via sliders), output:

Top 5 assets to OWN (ranked by composite score = expected return × T-stat)
Bottom 5 assets to AVOID (negative composite score)
Confidence indicator for each (T-stat threshold)

───

Cycle Phase Detector

Map current factor readings to one of 4 phases:

| Phase  | Signature            | Optimal Assets                         |
| ------ | -------------------- | -------------------------------------- |
| Spring | Growth ↑ Inflation ↓ | Tech, growth, risk-on                  |
| Summer | Growth ↑ Inflation ↑ | Cyclicals, semis, EM, commodity FX     |
| Autumn | Growth ↓ Inflation ↑ | Energy, gold, hard assets              |
| Winter | Growth ↓ Inflation ↓ | Long bonds, defensive equities, dollar |Display current phase prominently on dashboard.

───

Display Layout (Updated)

Header:
DJG Advisory | Current Phase: [Macro Summer] | Signal: [SELECTIVE]

Section 1 — Optimal Allocation Right Now:

• Top 5 assets to own (with weight %, expected return, confidence)
• Bottom 5 to avoid (with reason)

Section 2 — Live Macro Factors:

• 7 factor readings with Z-scores
• Trend direction
• Color-coded status

Section 3 — Sector Heat Map:

• Visual grid of all 45 assets
• Green/yellow/red based on current optimal weight
• Click any asset for detailed view

Section 4 — Manual Scenario Input (for stress testing):

• 7 sliders (one per factor)
• "What if liquidity drains?" → instant ranking shift
• "What if dollar strengthens 5%?" → see impact

Section 5 — Backtest Conviction Callouts:

• Highest-confidence current signals (>2.5 T-stat)
• Historical performance of similar setups
• Sample size disclosure

───

Honest Disclosures

Each signal must show:

• Sample size (how many times this fired historically)
• T-stat (statistical confidence)
• Hit rate (% of times it worked)
• Average return (expected outcome)
• Caveat: regime never tested under sustained credit stress



----

next message (mostlikely continuation)

3. Expand asset universe to 45+
4. Build correlation matrix (post-2008 training)
5. Build scenario engine + cycle phase detector
6. New display layout with allocation engine front and center
7. Manual scenario sliders
8. Honest disclosures (sample size, T-stat, caveats)

───

Cadence

• Live data refresh: daily (NOT every 15 minutes — macro is monthly)
• Correlation matrix recalc: monthly
• Backtest cache refresh: weekly (Sunday)
• Telegram alerts: only on regime change or signal flip

───

What This Achieves

DJG Advisory becomes a real quant fund tool. Dan opens it, sees:

• What macro phase we're in
• What allocation is optimal across all 45 assets
• Top 5 to buy, bottom 5 to avoid
• Statistical confidence on every call
• Manual stress test capability

Decision time: 60 seconds.
Output: complete cross-asset allocation guidance.
Edge: 18+ years of data informing every decision.

That's the Bittel MIT model in code, owned by Dan, customised to his portfolio.

───

