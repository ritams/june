# Analyst Frameworks — Deep Reference

The 4 analysts tracked by June, their models, and how they interconnect.

---

## 1. Raoul Pal — CEO, Real Vision / Global Macro Investor

### Background
Founded Global Macro Investor (GMI) in 2005. Co-founded Real Vision in 2014. Former Goldman Sachs hedge fund manager. Manages EXPAAM (crypto fund-of-funds).

### Core Thesis: The Everything Code
Published 2023 as the "culmination of everything he's worked on since 2005."

**Fundamental equation:**
```
GDP Growth = Population Growth + Productivity Growth + Debt Growth
```

- Developed world has declining population growth (US working-age growth fell from 8% to 1%)
- Productivity stagnant
- Debt fills the gap (~370% of GDP)
- Central banks must inject liquidity to service this debt → ~8% annual liquidity growth since 2008
- Combined with 3-4% inflation = **12% annual hurdle rate** that most traditional assets can't beat
- Only scarce assets (crypto, tech) structurally outperform this debasement

### Key Metrics & Correlations
| Asset | Correlation to Global Liquidity |
|-------|---------------------------------|
| NASDAQ | 97.5% |
| Bitcoin | 85% (rising with institutional adoption) |

### The Cycle Model
- **4-year debt refinancing cycle** — synchronized with BTC halvings + US elections
- Now stretched to **5.4-year cycle**
- Current view: peak extends into **2026** because $10T US debt rollover is scheduled for 2026

### Macro Seasons (via Bittel's MIT)
Raoul uses Bittel's framework for tactical allocation:
- **Spring** (rising growth, falling inflation): tech, BTC, consumer discretionary
- **Summer** (peak growth, stable inflation): cyclicals, small caps, materials, altcoins + tech/crypto
- **Fall** (declining growth, rising inflation): defensive (gold) + speculative altcoins
- **Winter** (contraction): bonds, cash

### The Banana Zone
Term for explosive growth phases when liquidity + ISM + adoption all align simultaneously. Crypto experiences this more intensely than tech due to higher beta.

### Bitcoin Price Targets (as of March 2025, tied to ISM)
| ISM Level | BTC Target | Label |
|-----------|-----------|-------|
| ~52 | $215,000 | Base case (trend) |
| ~58 | $420,000 | +1 Standard Deviation |
| ~60 | $820,000 - $1M | +2 Standard Deviations |

### Key Rules
- "Dips are for buying" during Summer/Fall
- Hold cash for pullbacks rather than panic selling
- De-risk before Q4 to capture upside without overextension
- Expect 15x post-correction moves (2013/2014 analogue)

### Crypto Market Size Projections
| Timeframe | Market Cap | Wallets |
|-----------|-----------|---------|
| Current | ~$2.5T | ~500M |
| End 2025 | $10-15T | ~1.1B |
| 2030-2032 | $100T | ~4B |

### What He Gets Wrong
- Timing. Raoul's directional calls are usually right, but his timing and magnitude targets overshoot. He called for much higher ETH prices that haven't materialized. His "Banana Zone" timing has shifted multiple times.

---

## 2. Julien Bittel — Head of Macro Research, Global Macro Investor

### Background
31 years in global macro. Goldman Sachs, GLG Partners. Built the Macro Investing Tool (MIT) for Real Vision.

### Core Framework: Macro Investing Tool (MIT)

**Foundation:** 70 years of business cycle data correlating asset performance to growth + inflation regimes.

**The Four Seasons:**

| Season | Growth | Inflation | Best Assets | Worst Assets |
|--------|--------|-----------|-------------|--------------|
| **Spring** | Rising ↑ | Falling ↓ | Long-duration: tech, BTC, consumer discretionary | Commodities, energy |
| **Summer** | Peak ↑↑ | Stable → | Cyclicals: small caps, materials, altcoins + tech/crypto | Bonds, defensive |
| **Fall** | Declining ↓ | Rising ↑ | Defensive: gold, speculative altcoins | Growth stocks, bonds |
| **Winter** | Contracting ↓↓ | Any | Risk-off: bonds, cash | Everything else |

**Key indicator:** ISM PMI
- Above 50 = expansion
- Below 50 = contraction
- ISM peak ~58-60 historically correlates with market peaks

### How MIT Works
1. Classify current macro regime (season) by country
2. Rank assets by historical performance within that regime
3. Provide allocation recommendations
4. Update weekly with business cycle data

### Publications via Real Vision
- **MIT Business Cycle Update** — weekly report
- **MIT Monthly Video Report** — monthly deep dive
- **"Shooting the Shit"** — informal market discussions

### Relationship to Raoul Pal
Bittel is the quantitative backbone to Raoul's narrative. Raoul provides the high-level thesis; Bittel provides the data-driven framework and allocation model. Their views are tightly aligned — when Bittel's MIT says "Summer," Raoul's narrative supports it.

### Key Insight for Fred
When ingesting Bittel's content, the most important extraction is: **what season does MIT say we're in, and has it changed?** Season transitions are the highest-signal events.

---

## 3. Jamie Coutts — Chief Crypto Analyst, Real Vision

### Background
Chartered Market Technician (CMT). Former Bloomberg crypto analyst. Specializes in bridging traditional technical analysis with crypto markets.

### Core Framework: DeMark Sequential + Chameleon Re-Entry System

**Jamie's exact words:**
> "The re-entry framework is unchanged: I am looking for a completed weekly DeMark Sequential count and a Chameleon trend reversal on Bitcoin or Ethereum."

**Signal priority (in order):**
1. **Weekly DeMark Sequential bar 9** — primary re-entry signal
2. **Chameleon trend reversal to GREEN** — must confirm alongside weekly TD9
3. **RSI below 40** — secondary confirmation
4. **Daily DeMark Sequential** — shorter-term, not sufficient alone

### DeMark Sequential (TD Sequential)
- Counts consecutive closes compared to close 4 bars ago
- **Setup phase:** Bars 1 through 9
- **Bar 9 = potential exhaustion / reversal signal**
- Weekly TD9 is the primary signal; daily TD9 is secondary
- Can also count to 13 (countdown phase) for stronger exhaustion signals
- Jamie uses DeMark 13 exhaustion as well — printed Dec 31 indicating selling pressure depletion

### Chameleon Indicator
**Origin:** Developed by Alex Cole for Bloomberg Terminal. Ported to TradingView.

**Base logic (public Trend Chameleon):**
Evaluates 4 conditions:
1. MACD value > 0
2. SMA 50 of open prices > SMA 50 of close prices
3. Rate of Change (ROC) > 0
4. Current close > SMA 50

**Scoring:**
| Conditions Met | Color | Meaning |
|---------------|-------|---------|
| 0 | Purple | Strongest bearish |
| 1 | Red | Bearish |
| 2 | Yellow | Mixed / transitional |
| 3 | Green | Bullish |
| 4 | Teal | Strongest bullish |

**Jamie's custom versions:**
- **Chameleon LV v2.0** (Low Volatility) — for equities and BTC
- **Chameleon HV v2.0** (High Volatility) — for HYPE and altcoins
- These are **proprietary / invite-only on TradingView** — not publicly available
- Likely modified parameters or additional conditions tuned for different volatility regimes
- The output is simplified to GREEN or RED (binary trend direction)

### On-Chain Overlay
Jamie also factors on-chain data into his framework:
- **Cost basis of coins moved 6-12 months ago** — currently near $100K for BTC
- This level historically separates downtrends from uptrends
- BTC must reclaim this cost basis as a necessary (not sufficient) condition for bullish reversal

### Key Insight for Fred
Jamie's content should be parsed for: current TD Sequential count (what bar are we on?), Chameleon state (GREEN/RED), RSI level, and any changes to his re-entry conditions. Also flag if he mentions cost basis levels shifting.

### Key Insight for Warren
- Cannot replicate Chameleon LV/HV in Python (proprietary)
- CAN fire PineScript alerts from them if Daniel has access on his TradingView
- For non-TV assets, use **Supertrend** as the trend direction proxy (different signal, accept the gap)

---

## 4. Andreas Steno Larsen — Founder, Steno Research / Nowcast IQ

### Background
Former Chief Strategist at Nordea (largest Nordic bank). Founded Steno Research (independent macro research). Founded Nowcast IQ. CIO of Asgard-Steno Global Macro Fund.

### Core Framework: Nowcasting Model

**Three Pillars:**
1. **Inflation** — is it accelerating or decelerating (MoM)?
2. **Growth** — measured via PMI and similar indicators
3. **Liquidity** — central bank balance sheets (USD, EUR, GBP, SEK), monitored daily

These three pillars combine into **8 macro regimes** (2³ = 8 combinations of each being positive or negative).

### Key Regime: "Gung-Ho"
- Increased liquidity + lower inflation + higher growth
- Allows "ALL types of risk-taking including monkey jpgs and dog-coins"
- Most bullish regime for crypto and risk assets

### Leading Indicators

**Short-term inflation (months ahead):**
- NFIB Small Business Survey — pricing plans component
- Currently: ~20% of firms plan price increases → translates to ~2-3% inflation
- Warning level: above 50% → inflation concern

**Long-term inflation (1-2 years ahead):**
- Chinese credit cycle
- "The leading indicator for inflation in the West with one to two years of lead"

### Liquidity Analysis
- Monitors central bank balance sheets **daily**
- Tracks across currencies: USD, EUR, GBP, SEK
- End-of-day values incorporated into nowcasting model
- **2026 thesis:** Shift from central bank-driven to **privately-created liquidity** as the defining market dynamic
- This is a significant departure from Raoul Pal's framework which centers on central bank liquidity

### How Steno Differs from Raoul Pal
| Dimension | Raoul Pal | Andreas Steno |
|-----------|-----------|---------------|
| Liquidity source | Central bank QE / debt refinancing | Central bank + private credit creation |
| Update frequency | Monthly / quarterly thesis | Daily nowcasting |
| Methodology | Narrative + correlation | Quantitative nowcasting models |
| Crypto view | Structural mega-bull | Regime-dependent (bullish in Gung-Ho) |
| 2026 view | Cycle peak via $10T debt rollover | Private liquidity creation replaces CB-driven |

### Key Insight for Fred
Steno's content should be parsed for: current regime classification, liquidity trend direction, leading indicator readings (NFIB, Chinese credit), and any regime transitions. His daily updates are the highest-frequency signal of the four analysts.

---

## Cross-Analyst Matrix

| Question | Raoul Pal | Julien Bittel | Jamie Coutts | Andreas Steno |
|----------|-----------|---------------|--------------|---------------|
| What drives markets? | Global liquidity (8%/yr) | Business cycle seasons | Technical exhaustion signals | Nowcast regime (inflation × growth × liquidity) |
| Primary indicator | Global M2 / liquidity | ISM PMI | Weekly TD9 + Chameleon | Central bank balance sheets |
| When to buy? | During Spring/Summer dips | When ISM is rising | TD9 + Chameleon GREEN + RSI <40 | In "Gung-Ho" regime |
| When to sell? | Pre-Q4 / Winter | When ISM peaks / season shifts to Winter | Chameleon RED / TD exhaustion | When regime shifts bearish |
| BTC view (2026) | $215k-$820k+ | Tied to ISM level | Bullish if re-entry signals fire | Regime-dependent |
| Unique edge | Cycle length analysis (5.4yr) | 70yr backtest data | Mechanical entry framework | Daily nowcasting frequency |

---

## How They Fit Together in Fred's Pipeline

```
Steno (daily)     → Current regime + liquidity trend (the "weather report")
Bittel (weekly)   → Current season + allocation model (the "calendar")
Raoul (monthly)   → Thesis narrative + cycle position (the "map")
Coutts (as needed)→ Entry/exit signals (the "trigger")
```

**Highest conviction signal:** All four aligned.
- Steno says Gung-Ho + Bittel says Spring/Summer + Raoul says Banana Zone + Coutts signals TD9+GREEN
- This is the "alert Daniel immediately" scenario.
