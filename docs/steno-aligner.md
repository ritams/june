
-- pdf spec sort
Steno Portfolio Mirror Dashboard
Purpose: Track Steno’s latest thematic portfolio, compare it against a live IBKR portfolio, and provide simple
actionable outputs: Add / Trim / Hold / Missing / Overweight
1. Steno PDF Upload
• Upload each Steno PDF into the system.
• AI extracts portfolio weights, tickers, entry prices, add/trim commentary, risk-on/risk-off positioning, macro indicators,
and new or removed positions.
2. Steno Model Portfolio
• Create a clean model portfolio with ticker, asset type, target weight, entry price, and latest commentary.
3. IBKR Live Portfolio Sync
• Connect to IBKR in read-only mode.
• Pull live holdings, portfolio value, average cost, current price, weightings, cash balance, and P&L.;
4. Mirror Engine
• Compare Steno portfolio weights against live IBKR portfolio.
• Calculate overweight, underweight, missing positions, and rebalance suggestions.
5. Suggested Actions
• Provide simple instructions such as Add / Trim / Hold.
• Calculate exact capital amounts required to align with the target model.
6. Macro Summary
• Display overall risk environment: Risk On / Risk Off / Neutral.
• Show key macro indicators Steno is monitoring such as DXY, ISM, liquidity, bond yields, oil, and credit spreads.
7. Alerts
• Telegram alerts for portfolio changes and rebalance suggestions.
• Weekly alignment reports showing percentage alignment with the Steno model.
8. Tech Stack
• Frontend: Next.js
• Backend: Python FastAPI
• Database: PostgreSQL
• AI Layer: OpenAI or Claude
• Broker Integration: IBKR Client Portal API
• Alerts: Telegram Bot
• Hosting: Render, Railway, or AWS
9. MVP Build Order
• Phase 1: PDF Upload + AI Extraction
• Phase 2: IBKR Read-Only Integration
• Phase 3: Mirror Engine
• Phase 4: Telegram Alerts
• Phase 5: Weekly Alignment Reports
10. Key Philosophy
• Keep the system simple and actionable.
• Avoid overcomplicated dashboards or auto-trading initially.
• The dashboard should answer:
• • What did Steno change?
• • Am I aligned?
• • What should I add or trim?
Example Portfolio Alignment Table
Ticker Steno Weight Your Weight Gap Action
XYZ 5% 1% +4% Add
ABC 3% 7% -4% Trim
DEF 4% 


-- another message

So I want to add a separate section to the dashboard that ingests the Steno PDFs, connects via API to my stock portfolio, and then sends clear signals such as “add this stock”, “trim this position”, or “sell this stock”.

Ideally, I want this to sit separately from the main dashboard, but still allow me to constantly check whether my portfolio is aligned with Steno’s positioning.

I would also like to include the broader macro view from Julian, so I can understand the overall market environment alongside the portfolio signals


Because how it works now is I’m reading steno reports and then adding or trimming based on what he recommends but I would be far more precise if we can use AI to do this


So it’s get the signals from steno, then calls IBKR stock portfolio (API) and then ensure the portfolio is aligned with his recommendations


But do this in another section of the dashboard


Let me know when you understand it bro because we don’t this to be fragile


--- another message

But happy for you to make suggestions bro because I don’t want this to be fragile


This needs to be very clean


Basically we want to follow steno on his portfolio weights. I.e steno has 6% to Gold, does Dan have 6% exposure?


-- another message
This needs to be a very slick part of what we are doing as it’s very difficult to align the portfolio adds directly with steno at the moment by just looking at the PDFs


His intelligence needs to speak to my portfolio and then give me actionable advice


Also I suggest we use GPT 5.5


--- another message.


I want this built as a simple Steno Portfolio Signal Dashboard.
The goal is not to manage my cash. I will always have cash elsewhere.
The system’s job is to read Steno’s latest portfolio updates, compare them against my IBKR holdings, and tell me the exact percentage exposure I need to buy, sell, add, trim, hold, or remove.
The key output should be simple:
Buy 5% XYZ
Add 3% ABC
Trim 2% DEF
Sell/remove GHI
Hold everything else
Core features:
Upload Steno PDFs.
AI extracts tickers, model weights, entry prices, commentary, risk tone, and portfolio changes.
IBKR connects in read-only mode.
Dashboard calculates my current portfolio weights automatically.
System compares Steno target weights against my current weights.
Dashboard outputs clean portfolio signals:
Buy
Add
Trim
Sell
Hold
Remove
Missing
The dashboard should show:
Steno target weight
My current weight
Difference
Action
Commentary
Latest Steno change
Risk-on / risk-off / neutral
Important: Do not make this overcomplicated with cash management or automatic trading at MVP stage.
I want a clean signal engine that tells me what exposure I need.
Example:
Ticker: XYZ Steno Target: 7% My Current Weight: 2% Gap: +5% Signal: Buy 5% XYZ
Ticker: ABC Steno Target: 3% My Current Weight: 6% Gap: -3% Signal: Trim 3% ABC
Ticker: DEF Steno Target: 4% My Current Weight: 0% Gap: +4% Signal: Buy 4% DEF
This should feel like a portfolio co-pilot, not a spreadsheet.


That percentage is based on the portfolio size I.e if it’s 1m portfolio and the allocation is 5% then the wait is 50k