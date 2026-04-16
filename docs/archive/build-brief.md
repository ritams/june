# Build Brief — June

## What We're Building

An AI system that reads your Real Vision PDFs daily, extracts and tracks the macro views of your 4 analysts, synthesizes them into a single actionable briefing, and pairs that with mechanical trading signals for your asset universe.

Two components: a **research layer** and a **technical layer**.

---

## Research Layer

### Input
1-2 Real Vision PDFs per day. That's the primary data source.

### What It Does

**Step 1 — Extraction:** Reads each PDF, identifies which analyst is speaking, and pulls out their current positions, claims, and conviction levels — structured against 10 explicit questions:

1. Biggest macro risk next 90 days?
2. Where are we in the liquidity cycle — and what's your evidence?
3. BTC above or below $100k by end of Q3 2026? Conviction 1-10.
4. Top 3 highest conviction trades right now — rank 1 to 3 with brief reasoning.
5. What kills your thesis?
6. Rate-sensitive or commodity assets — which do you prefer and why?
7. US equities — add, hold or reduce? Why?
8. What is the market most wrong about right now?
9. Next significant drawdown — 30, 90 or 180+ days?
10. One thing you're watching that nobody else is talking about.

**Step 2 — Thesis Tracking:** Every new claim is compared against that analyst's last known position. The system flags when someone changes their view, reinforces it, or contradicts a prior call. No more reading between the lines across 30 pages — changes surface automatically.

**Step 3 — Cross-Analyst Synthesis:** The system looks across all four analysts and tells you:
- Where they agree (high conviction signal)
- Where they disagree (flag for your attention)
- A consensus view with dissent noted

### How The 4 Analysts Fit Together

Each analyst serves a different function in the stack:

| Analyst | Role | What We Extract |
|---------|------|-----------------|
| **Andreas Steno** | Daily weather report | Current regime (inflation × growth × liquidity), liquidity trend, leading indicators |
| **Julien Bittel** | The calendar | Current macro season (Spring/Summer/Fall/Winter), allocation shifts, ISM readings |
| **Raoul Pal** | The map | Cycle position, thesis narrative, structural view on liquidity + crypto |
| **Jamie Coutts** | The trigger | Re-entry signals, TD Sequential count, Chameleon state, RSI levels |

**Highest conviction signal:** All four aligned — Steno says bullish regime, Bittel says Spring/Summer, Raoul says Banana Zone, Coutts signals entry. That's when you get an immediate alert.

### Output — Daily Briefing

A structured report delivered daily:
- Sources ingested that day
- Consensus macro view
- Key disagreements between analysts
- Each of the 10 questions answered with per-analyst attribution
- Any thesis changes since last briefing
- Strategy recommendations (which flow into the technical layer)

---

## Technical Layer

### What It Does
Provides real-time scoring and entry signals for your asset universe: **BTC, HYPE, MU, LIN, UUUU, CRCL, COIN.**

Two different signal sources depending on the asset:

### BTC + HYPE — TradingView Webhooks

Uses PineScript alerts firing directly from your TradingView charts. Aligned to Jamie Coutts' re-entry framework:

| Signal | Priority |
|--------|----------|
| Weekly TD Sequential bar 9 | Primary re-entry signal |
| Chameleon trend reversal to GREEN | Primary confirmation |
| RSI below 40 | Secondary confirmation |
| Daily TD Sequential bar 9 | Shorter-term, not sufficient alone |

**Scoring:**
- **4/4** (Weekly TD9 + Chameleon GREEN + RSI <40 + at support) → **Strong buy — immediate alert**
- **3/4** → **Watch — morning brief**
- **2/4 or less** → **Wait — logged only**
- **Special:** Weekly TD9 + Weekly Chameleon GREEN together = maximum conviction alert regardless

**Requires:** Chameleon LV v2.0 (for BTC) and Chameleon HV v2.0 (for HYPE) on your TradingView charts. **Please confirm you have access to these.**

### Equities — Python-Calculated Indicators

For MU, LIN, UUUU, CRCL, COIN — indicators calculated directly from market data (Twelve Data API for equities, CCXT for crypto):

| Indicator | What It Tells You |
|-----------|-------------------|
| TD Sequential | Exhaustion count (1-9), bar 9 = potential reversal |
| Supertrend | Trend direction — GREEN (bullish) or RED (bearish) |
| RSI | Actual number (e.g. 38.2), not categories |
| Support / Resistance | 3 levels each, from recent swing highs/lows |

**Scoring:**
- **3/3** (TD9 + Supertrend GREEN + RSI <40) → **Buy — alert**
- **2/3** → **Watch — morning brief**
- **1/3 or less** → **Wait**

Note: Supertrend replaces Chameleon for non-TradingView assets. Different indicator, same purpose (trend direction). Chameleon is proprietary and can only be read from your TV charts via webhooks.

### Endpoints

**Levels check** — query any ticker, get back a clean snapshot:
```
MU — $355
TD Sequential: 5/9
Supertrend: GREEN
RSI: 38.2
Support: $340, $320, $300
Resistance: $375, $395, $415
Score: 2/3 — WATCH
```

**Signal log** — every signal logged with timestamp, price, score, and action. Outcomes recorded 20 days later automatically. This builds your track record over time.

### Alerts
When a signal hits threshold, you get an alert immediately (Telegram, or whatever channel you prefer). Morning briefs include all "watch" signals.

---

## How The Two Layers Connect

The research layer feeds into the technical layer:
- If all 4 analysts are macro-bullish, technical buy signals carry more weight
- If the macro backdrop is deteriorating, technical buy signals get flagged with caution
- Strategy recommendations from the research layer (e.g. "Coutts says watch for BTC re-entry") become active watchlist items in the technical layer

---

## Confirmed

- **Chameleon access** — Chameleon LV v2.0 and HV v2.0 available on TradingView. Full 4-signal scoring for BTC/HYPE.
- **Alerts** — Telegram.
- **Briefing** — Telegram message.
- **Historical data** — 12 months of Real Vision PDFs available for building initial analyst baselines.

## Questions For You

1. **Asset list** — BTC, HYPE, MU, LIN, UUUU, CRCL, COIN — anything to add or remove?
2. **Twelve Data** — We need a paid API tier for equity data. Free tier won't be enough for 7 tickers across multiple timeframes.
