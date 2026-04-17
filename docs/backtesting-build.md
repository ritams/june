───

Ritam — Phase 2: Backtesting Engine + Allocation Playbook

───

Dashboard fixes first (quick):

1. Confirm FRED data is actually pulling — showing "Waiting for live data"
2. Deploy to Streamlit Community Cloud — Cloudflare tunnel is temporary
3. Test Telegram card push — send one manually and confirm it arrives

Don't block on these. Keep moving to Phase 2.

───

What we're building:

Add a backtesting section to both dashboards. Takes the live macro signal, shows what historically happened to each asset after that signal fired across a 6-month to 2-year window, and outputs a specific allocation recommendation.

Dan is a medium to long-term investor. No short-term windows. Everything is 6 months minimum.

───

The output Dan sees:

📊 CURRENT SIGNAL: RISK ON 🟢
Liquidity: Expanding | Cycle: Mid-Expansion

📖 HISTORICAL PLAYBOOK (2000–2026)

Asset  | 6m      | 1yr     | 18m     | 2yr     | Win%(1yr)
-------|---------|---------|---------|---------|----------
S&P    | +16.4%  | +22.1%  | +31.4%  | +38.2%  | 82% 🟢
Nasdaq | +24.3%  | +34.6%  | +51.2%  | +67.8%  | 86% 🟢
Gold   | +7.3%   | +11.2%  | +16.8%  | +19.4%  | 71% 🟡
BTC    | +94%    | +187%   | +312%   | n/a ⚠️  | 75% 🔴

⚠️ BTC: 8 signals only (2017+). Direction only — never use for precise sizing.
⚠️ 18m/2yr windows exclude recent signals where the window hasn't elapsed yet.

💡 At RISK ON, Nasdaq has averaged +67.8% over 2 years in 19 out of 22 cases.
   Short-term dips are noise. Stay invested.

🎯 ALLOCATION RECOMMENDATION
→ Deploy 80% of available capital | Keep 20% cash
→ Favour: Nasdaq, AI infrastructure, crypto on dips
→ Trim: Cash overweight, defensive positions───

6 signals to backtest:

Signal 1 — RISK ON
Condition: Liquidity = EXPANDING AND Cycle = EXPANSION
Test on: SPY, QQQ, IAU, BTC-USD

Signal 2 — RISK OFF
Condition: Liquidity = CONTRACTING OR Cycle = CONTRACTION
Test on: SPY, QQQ, IAU, BTC-USD (shows expected drawdown)

Signal 3 — Yield Curve Uninversion
Condition: T10Y2Y crosses from negative to positive (yesterday < 0, today >= 0)
Test on: SPY, QQQ, BTC-USD
Why: Raoul's primary cycle recovery signal. FRED data back to 1976.

Signal 4 — M2 Acceleration
Condition: US M2 MoM > 0.5% for 2 consecutive months
Test on: BTC-USD, QQQ, Gold
Why: Jamie Coutts' primary BTC liquidity predictor

Signal 5 — Dollar Weakness
Condition: DXY below 100 AND 3-month trend negative
Test on: BTC-USD, Gold, QQQ

Signal 6 — Credit Stress
Condition: HY spreads (BAMLH0A0HYM2) cross above 500bps
Test on: SPY, QQQ (shows expected drawdown)

Signal 7 — Macro Summer Entry (Raoul's framework)
Condition: T10Y2Y crosses positive from negative AND ISM PMI rising AND credit spreads contracting
Test on: QQQ, BTC-USD
Horizons: 1yr, 18m, 2yr only — this is a cycle signal, not short-term
Add to Business Cycle dashboard only

───

How to build it:

Step 1 — Data

from fredapi import Fred
import yfinance as yf

fred = Fred(api_key=FRED_API_KEY)

yield_curve = fred.get_series('T10Y2Y')
m2 = fred.get_series('M2SL')
credit_spreads = fred.get_series('BAMLH0A0HYM2')
rrp = fred.get_series('RRPONTSYD')
tga = fred.get_series('WTREGEN')
fed_balance = fred.get_series('WALCL')

assets = {
    'SPY': yf.download('SPY', start='2000-01-01', auto_adjust=True),
    'QQQ': yf.download('QQQ', start='2000-01-01', auto_adjust=True),
    'IAU': yf.download('IAU', start='2005-01-01', auto_adjust=True),
    'BTC': yf.download('BTC-USD', start='2017-01-01', auto_adjust=True),
    'DXY': yf.download('DX-Y.NYB', start='2000-01-01', auto_adjust=True),
}Step 2 — Find signal events
Scan history and find dates when each signal fired for the FIRST time — transition events only, not every day it was active. Store as list of dates.

Step 3 — Forward return calculator

horizons = [180, 365, 540, 730]  # 6m, 1yr, 18m, 2yr

def calculate_forward_returns(event_dates, asset_prices, horizons):
    results = {}
    for horizon in horizons:
        returns = []
        for event_date in event_dates:
            try:exit_date = event_date + timedelta(days=horizon)
                if exit_date > datetime.today():
                    continue  # Window not complete — skip entirely
                entry = asset_prices['Close'].asof(event_date)
                exit_p = asset_prices['Close'].asof(exit_date)
                returns.append((exit_p - entry) / entry * 100)
            except:
                continue
        if returns:
            results[horizon] = {
                'avg': round(np.mean(returns), 1),
                'win_rate': round(len([r for r in returns if r > 0]) / len(returns), 2),
                'n': len(returns),
                'best': round(max(returns), 1),
                'worst': round(min(returns), 1),
            }
    return resultsStep 4 — Allocation rules

ALLOCATION = {
    'RISK_ON':  {'deploy_pct': 80, 'cash_pct': 20, 'action': 'Add to risk assets on dips. Stay invested.'},
    'NEUTRAL':  {'deploy_pct': 65, 'cash_pct': 35, 'action': 'Hold. No new risk until signal improves.'},
    'RISK_OFF': {'deploy_pct': 40, 'cash_pct': 60, 'action': 'Raise cash. Wait for signal to flip.'},
}Step 5 — Cache results
Save to: ~/projects/hedge-brain/data/backtest/results.json
Recalculate weekly on Sunday. Load from cache on every page load — never recalculate on page visit.

{
  "last_calculated": "2026-04-17",
  "RISK_ON": {
    "SPY": {
      "180": {"avg": 16.4, "win_rate": 0.82, "n": 21},
      "365": {"avg": 22.1, "win_rate": 0.82, "n": 20},
      "540": {"avg": 31.4, "win_rate": 0.79, "n": 19},
      "730": {"avg": 38.2, "win_rate": 0.78, "n": 18}
    },
    "QQQ": { ... },
    "IAU": { ... },
    "BTC": { ... }
  }
}Step 6 — Streamlit display
Add "📖 Historical Playbook" section below current signal on both pages.
Show: results table + allocation box + conviction callout.
BTC gets 🔴 badge + warning when n < 10.
Any horizon with n < 5 — show "Insufficient data" not a number.

───

Confidence badges:

| Asset     | Data from | Confidence                            |
| --------- | --------- | ------------------------------------- |
| SPY / QQQ | 2000      | 🟢 HIGH — 20+ complete cycles         |
| IAU       | 2005      | 🟡 MEDIUM                             |
| BTC       | 2017      | 🔴 LOW — direction only, never sizing |───

What goes on each page:

Liquidity dashboard → Signal 1 (RISK ON) + Signal 4 (M2) + Signal 5 (DXY) + allocation rec

Business Cycle dashboard → Signal 2 (RISK OFF) + Signal 3 (Yield Curve) + Signal 6 (Credit) + Signal 7 (Macro Summer) + allocation rec

───

Definition of done:

• [ ] 7 signals backtested across SPY, QQQ, IAU, BTC
• [ ] Horizons: 6m, 1yr, 18m, 2yr only
• [ ] Results table on both dashboard pages
• [ ] Allocation recommendation live — updates on signal flip
• [ ] BTC warning when n < 10, "Insufficient data" when n < 5
• [ ] Results cached in JSON, recalculates weekly Sunday
• [ ] Conviction callout box showing the 2-year number prominently
• [ ] Tested end to end
