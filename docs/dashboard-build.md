
🏗️ BUILD 1 — Macro Dashboard 

───

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
• Show: current b'credit_spreads': 'BAMLH0A0HYM2',
    'jobless_claims': 'IC4WSA',
}
FRED fetch URL:
https://api.stlouisfed.org/fred/series/observations?series_id={ID}&api_key={KEY}&sort_order=desc&limit=5&file_type=json
───

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
→ "⚠️ DOLLAR SQUEEZE: DXY 105+. Crypto headwind."
───

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
→ Add to high conviction positions on dips.
───

Definition of Done

• [ ] Public URL works on Dan's phone
• [ ] 🟢 RISK ON / 🔴 RISK OFF shows clearly at top
• [ ] DXY and yields update every 15 min
• [ ] Streamlit Community Cloud deployed, URL shared with Dan
• [ ] At least one alert trigger tested and fires to Telegram
• [ ] Google Sheet "Macro Dashboard" tab logs daily data