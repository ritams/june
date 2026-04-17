
What It Is

A live web dashboard Dan can open on his phone or laptop anytime. Two sections. One big signal at the top.

The signal: 🟢 RISK ON or 🔴 RISK OFF

That's it. Everything else feeds into that.

───

What It Looks Like

╔══════════════════════════════════╗
║   📊 MACRO DASHBOARD             ║
║   🟢 RISK ON                     ║
║   Last updated: 14:32 GMT        ║
╠══════════════════════════════════╣
║ 💧 LIQUIDITY: EXPANDING 🟢       ║
║   DXY: 98.2 ↓ (weakening ✅)    ║
║   M2: +0.6% MoM ↑               ║
║   RRP: $182B ↓ (draining ✅)    ║
║   TGA: $418B ↓ (spending ✅)    ║
║   Fed Balance Sheet: $6.8T →     ║
╠══════════════════════════════════╣
║ 📈 CYCLE: MID-EXPANSION 🟢       ║
║   ISM PMI: 52.3 ✅               ║
║   Yield curve: +0.42% ✅         ║
║   Credit spreads: 312bps ✅      ║
║   Jobless claims: 215k stable ✅ ║
║   Korean exports: +8.3% YoY ✅   ║
╚══════════════════════════════════╝───

Stack (use exactly these tools)

| Job                | Tool                                                                        |
| ------------------ | --------------------------------------------------------------------------- |
| Macro data         | FRED API (free — register at fred.stlouisfed.org, add FRED_API_KEY to .env) |
| Live prices        | yfinance (already installed)                                                |
| Real-time web data | Perplexity sonar-pro (key already in hedge-brain/.env)                      |
| Dashboard UI       | Streamlit (Python only, no HTML needed)                                     |
| Hosting            | Streamlit Community Cloud (free — connects to GitHub, gives public URL)     |
| Alerts             | Existing Telegram bot                                                       |
| History log        | Existing Google Sheet (new tab: "Macro Dashboard")                          |New code goes in: ~/projects/hedge-brain/macro-api/

───

Section 1 — Liquidity Dashboard

These indicators tell us if there's more or less money flowing in the financial system.

───

1. DXY — US Dollar Index

The US dollar vs other currencies. When dollar weakens = money flows into assets. When it strengthens = money drains from assets.

• Fetch: yfinance ticker DX-Y.NYB
• 🟢 Risk on: DXY falling or below 101
• 🔴 Risk off: DXY rising or above 104
• Update: every 15 minutes

───

2. US M2 Money Supply

Total money in circulation in the US. When it grows, more money chases assets. BTC follows this with a ~12-week lag — most reliable crypto predictor.

• Fetch: FRED API, series M2SL
• Show: current value in trillions + % change month-over-month + % change year-over-year
• 🟢 Risk on: growing MoM
• 🔴 Risk off: flat or shrinking
• Update: weekly (FRED publishes Thursdays)

───

3. Reverse Repo (RRP)

Money banks park overnight at the Fed instead of putting into markets. Like a reservoir — when it drains, that money floods into markets.

• Fetch: FRED API, series RRPONTSYD
• Show: current balance in billions + direction arrow
• 🟢 Risk on: balance declining
• 🔴 Risk off: balance rising
• Update: daily (FRED publishes afternoon)

───

4. TGA — Treasury General Account

The US government's bank account at the Fed. When they spend (balance falls), money flows into the economy.

• Fetch: FRED API, series WTREGEN
• Show: current balance in billions + direction
• 🟢 Risk on: balance falling
• 🔴 Risk off: balance rising
• Update: weekly

───

5. Fed Balance SheetTotal assets held by the Federal Reserve. Growing = they're injecting money. Shrinking = draining.

• Fetch: FRED API, series WALCL
• Show: current total in trillions + direction
• 🟢 Risk on: growing or stable
• 🔴 Risk off: shrinking
• Update: weekly (Thursdays)

───

Calculated field — Global M2 Proxy

global_m2_proxy = us_m2 / dxy * 100Show current value + MoM % change. This is the key crypto predictor.

───

Liquidity Signal Logic:

if m2_mom > 0.3 and rrp_direction == "falling" and dxy < 101:
    liquidity = "EXPANDING 🟢"
elif dxy > 104 or m2_mom < 0:
    liquidity = "CONTRACTING 🔴"
else:
    liquidity = "NEUTRAL 🟡"───

Section 2 — Business Cycle Dashboard

These indicators tell us if the economy is growing or shrinking.

───

1. ISM Manufacturing PMI

Monthly survey of 300 US manufacturers. Above 50 = growing. Below 50 = shrinking. Best leading indicator of the economy.

• Fetch: Perplexity sonar-pro
• Prompt: "What is the latest ISM Manufacturing PMI reading? Just give me the number and the month."
• 🟢 Risk on: above 52
• 🟡 Neutral: 50–52
• 🔴 Risk off: below 50
• Update: every 2 hours via Perplexity (monthly data, but Perplexity catches it same day it's released)

───

2. Yield Curve (2yr vs 10yr US Treasury)

Difference between US 10-year and 2-year bond interest rates. Normally positive. When negative = recession warning. When it flips back positive from negative = economy recovering.

• Fetch: FRED API, series T10Y2Y
• Show: current spread as % (e.g. +0.42% or -0.18%)
• 🟢 Risk on: positive and stable
• 🟡 Watch: flipping from negative to positive (cycle turn signal)
• 🔴 Risk off: deeply negative
• Update: daily

───

3. High Yield Credit Spreads

Extra interest rate risky companies pay vs safe US government bonds. Rising = investors scared = early warning for market stress.

• Fetch: FRED API, series BAMLH0A0HYM2
• Show: current spread in basis points (bps)
• 🟢 Risk on: below 350 bps
• 🟡 Caution: 350–500 bps
• 🔴 Risk off: above 500 bps
• Update: daily

───

4. Jobless Claims (4-week average)

Americans filing for unemployment each week. Rising = companies firing people = economy slowing.

• Fetch: FRED API, series IC4WSA
• Show: 4-week average + direction vs prior month
• 🟢 Risk on: below 250k and stable
• 🔴 Risk off: rising for 4+ consecutive weeks
• Update: weekly (every Thursday)

───

5. South Korean Exports

South Korea's monthly export data (especially semiconductors). Their numbers predict global manufacturing 1-2 months ahead.

• Fetch: Perplexity sonar-pro
• Prompt: "Latest South Korea export data — give me the year-over-year % change and which month it covers."
• 🟢 Risk on: accelerating YoY
• 🔴 Risk off: decelerating or negative
• Update: monthly (Perplexity catches on release day)

───

Cycle Signal Logic:

if ism > 52 and yield_curve > 0 and spreads < 350:
    cycle = "EXPANSION 🟢"
elif ism > 50 and spreads < 450:
    cycle = "LATE CYCLE 🟡"
elif ism < 50 or spreads > 500:
    cycle = "CONTRACTION 🔴"───

Combined Signal

if liquidity == "EXPANDING" and cycle == "EXPANSION":
    signal = "✅ RISK ON"
elif liquidity == "CONTRACTING" or cycle == "CONTRACTION":
    signal = "❌ RISK OFF"
else:
    signal = "🟡 SELECTIVE"───

Update Schedule

| Data                                       | Frequency         | Tool                 |
| ------------------------------------------ | ----------------- | -------------------- |
| DXY, yields, gold, BTC                     | Every 15 min      | yfinance             |
| ISM, Korean exports                        | Every 2 hours     | Perplexity sonar-pro |
| RRP, credit spreads                        | Daily             | FRED API             |
| M2, TGA, Fed balance sheet, jobless claims | Weekly on release | FRED API             |───

FRED Series IDs

FRED_SERIES = {
    'yield_curve':    'T10Y2Y',
    'm2':             'M2SL',
    'fed_balance':    'WALCL',
    'rrp':            'RRPONTSYD',
    'tga':            'WTREGEN','credit_spreads': 'BAMLH0A0HYM2',
    'jobless_claims': 'IC4WSA',
}FRED fetch URL:

https://api.stlouisfed.org/fred/series/observations?series_id={ID}&api_key={KEY}&sort_order=desc&limit=5&file_type=json───

Instant Alerts (Telegram to Dan 7795075677)

Fire these immediately whenever triggered — not just on the morning card:

Yield curve flips positive after being negative
→ "🔄 CYCLE TURN: Yield curve confirmed positive. Recovery signal."

ISM crosses 50 either direction
→ "📊 ISM crossed 50 — cycle inflection point."

Global M2 proxy drops >3% in a month
→ "⚠️ LIQUIDITY WARNING: Global M2 contracting. Reduce risk."

Credit spreads above 500bps
→ "🚨 CREDIT STRESS: 500bps+. Risk off immediately."

DXY breaks above 105
→ "⚠️ DOLLAR SQUEEZE: DXY 105+. Crypto headwind."───

Daily Telegram Card (7:45am to Dan 7795075677)

📊 MACRO DASHBOARD — [Date]

💧 LIQUIDITY: EXPANDING 🟢
  M2: $21.4T (+0.6% MoM) ↑
  RRP: $182B ↓ (draining ✅)
  TGA: $418B ↓ (spending ✅)
  DXY: 98.2 (weakening ✅)
  Global M2 Proxy: 91.4 (+2.1% MoM) ↑

📈 CYCLE: MID-EXPANSION 🟢
  ISM PMI: 52.3 ✅
  Yield curve: +0.42% ✅
  Credit spreads: 312bps ✅
  Jobless claims: 215k stable ✅
  Korean exports: +8.3% YoY ✅

→ SIGNAL: ✅ RISK ON
→ Add to high conviction positions on dips.───

Definition of Done

• [ ] Public URL works on Dan's phone
• [ ] 🟢 RISK ON / 🔴 RISK OFF shows clearly at top
• [ ] DXY and yields update every 15 min
• [ ] Streamlit Community Cloud deployed, URL shared with Dan
• [ ] At least one alert trigger tested and fires to Telegram
• [ ] Google Sheet "Macro Dashboard" tab logs daily data


