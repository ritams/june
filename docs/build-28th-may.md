Corrected Final Dev Spec


DJG Advisory Dashboard — Final Build Specification
Objective
Finish the dashboard as a simple, professional macro allocation tool.

The dashboard should answer:

1. Where are we in the macro cycle?
2. Should we be pressing risk, staying balanced, or protecting capital?
3. What assets are favoured by the current regime?
4. What happened historically when conditions looked like this?
5. If we had invested £100k into an asset or basket, what would it be worth today?
6. What should Hermes tell us each week?
Do not turn this into a short-term trading system.

Do not add more tabs or unnecessary indicators.

1. Navigation
Final navigation should be:

Allocation
Liquidity
Cycle
Remove:

Steno Mirror
No replacement news/noise tab.

Hermes does not need its own full tab unless we want a memo archive later. For now, Hermes should live inside the Allocation page and send weekly updates.

2. Add Hermes CIO Card
Add this at the very top of the Allocation page.

Title:

HERMES CIO VIEW
Purpose:

This is the final decision layer of the dashboard.

It should summarise the whole dashboard in plain English.

Fields
Current Stance
Risk Budget
Capital Deployment
Cash Reserve
Macro Season
Liquidity State
Cycle State
Confidence
Last Updated
Example
HERMES CIO VIEW

Current Stance: Cautious Risk-On
Risk Budget: 52 / 100
Capital Deployment: 55%
Cash Reserve: 45%
Macro Season: Summer
Liquidity State: Contracting but not broken
Cycle State: Late cycle but still supportive
Confidence: Medium

Summary:
Maintain core exposure, but do not press maximum risk until liquidity confirms. Deploy new capital in tranches. No leverage.
This card is the most important addition.

The user should open the dashboard and immediately understand the current stance.

3. Risk Budget Logic
Create one score:

Risk Budget: 0 to 100
Meaning:

0 = maximum defence
100 = maximum risk-on
Use existing dashboard inputs only.

Do not add new indicators.

Inputs
Liquidity
Cycle
Growth
Inflation
Rates
Dollar
Oil
Risk Appetite
Suggested weighting
Risk Budget =
  30% Liquidity
  25% Cycle / Growth
  15% Risk Appetite
  10% Dollar
  10% Rates
  10% Inflation / Oil
All factors must be converted so that:

Positive = supportive for risk assets
Negative = bad for risk assets
Example:

Dollar falling = positive
Dollar rising = negative

Rates falling = positive
Rates rising = negative

Liquidity rising = positive
Liquidity falling = negative
4. Risk Budget Output
Map the Risk Budget to a simple stance.

Risk Budget	Stance	Deployment
0–20	Fortress Mode	20% deployed / 80% cash
21–40	Defensive	35% deployed / 65% cash
41–60	Cautious Risk-On	50–60% deployed / 40–50% cash
61–80	Constructive Risk-On	70–80% deployed / 20–30% cash
81–100	Full Risk-On	85–95% deployed / 5–15% cash
This is not meant to give exact personal portfolio weights.

It is a risk throttle.




6. Scenario Section — Keep Existing, Add One Simple Outcome Card
You already have a scenario engine and historical playbook.

Keep those.

Add one new block under the Hermes CIO card:

WHAT-IF OUTCOME
Purpose:

Show what a fixed investment would have done historically.

User Inputs
Initial Investment Amount
Asset or Basket
Start Date
End Date
Mode
Defaults
Initial Investment Amount: £100,000
End Date: Today / Latest Available Data
Mode: Buy and Hold
Asset / Basket Options
Use only assets already in the dashboard.

Examples:

Gold
QQQ / Nasdaq
SMH / Semiconductors
S&P 500
HYG / High Yield Credit
LQD / IG Credit
TLT / Long Bonds
Cash / T-Bills
BTC,  asset data
Framework Portfolio


7. What-If Outcome Output
The output should be simple.

Example:

WHAT-IF OUTCOME

Asset: Gold
Initial Investment: £100,000
Start Date: 1 Jan 2024
End Date: Latest Available
Mode: Buy and Hold

Ending Value: £126,400
Total Return: +26.4%
Annualised Return: +14.2%
Max Drawdown: -8.7%
Best Month: +6.1%
Worst Month: -4.8%
Add one small chart:

Growth of £100,000
No need for complicated charts.

8. Framework Portfolio Outcome
Add one option:

Framework Portfolio
This shows how the dashboard’s own allocation framework would have performed.

Inputs:

Initial Investment: £100,000
Start Date
End Date
Rebalance Frequency: Monthly
Output:

Ending Value
Total Return
Annualised Return
Max Drawdown
Average Cash Level
Best 12 Months
Worst 12 Months
This is the important one.

It answers:

“If I had followed this dashboard, would it have protected capital and compounded well?”

9. Hermes Agent Role
Hermes should not become another dashboard.

Hermes should be a simple CIO assistant that reads the dashboard and explains the current stance.

Hermes should be able to answer:

What is our current stance?
Why is the dashboard risk-on or risk-off?
What changed this week?
What should we watch?
What would make us add risk?
What would make us cut risk?
What would £100k invested in X have done?
How did the framework portfolio perform over this period?
Hermes should call the backtest engine for numbers.

Hermes should not invent returns.

10. Hermes Functions Needed
Dev should expose these functions to Hermes:

get_current_dashboard_state()
get_current_risk_budget()
get_current_macro_season()
get_liquidity_state()
get_cycle_state()
get_current_allocation()
run_what_if_outcome(asset_or_basket, start_date, end_date, amount, mode)
run_framework_portfolio_outcome(start_date, end_date, amount)
generate_weekly_cio_message()
send_weekly_telegram_message()
That is enough.

Do not build a multi-agent system.

One Hermes CIO agent is fine.

11. Weekly Hermes Message
Send one message per week.

Recommended timing:

Monday morning
Use the latest available weekly close.

Weekly message template
DJG HERMES CIO WEEKLY

Risk Budget: 52 / 100
Stance: Cautious Risk-On
Deployment: 55%
Cash Reserve: 45%
Macro Season: Summer
Liquidity: Contracting but not broken
Cycle: Still supportive

What changed:
- Liquidity weakened slightly.
- Growth remains supportive.
- Dollar and rates remain key.
- Oil is still creating noise.

Action:
Maintain core exposure. Deploy new capital in tranches. Do not press maximum risk until liquidity confirms.

Add risk if:
Liquidity turns positive, dollar weakens, rates ease, credit remains calm.

Cut risk if:
Liquidity deteriorates, dollar breaks higher, credit spreads widen, cycle rolls over.

Hermes view:
The long-term exponential-age thesis remains intact, but the macro throttle is not full green yet.
Keep it short.

No long essays.

12. MIT Report Treatment
Do not build MIT into the scoring model.

Use it as a qualitative overlay for Hermes.

The uploaded MIT report supports this approach because it says the business cycle has not broken and liquidity remains volatile around a still-rising trend, while also highlighting AI infrastructure capex as a key cycle driver.

So Hermes can say:

MIT Overlay:
Latest MIT view is broadly constructive on the business cycle, but liquidity remains volatile. Use as context, not as a model override.
MIT is published monthly on real vision by Julien Bittel
—————————————————////////////
13. SLR / eSLR Treatment

Do not add a big SLR/eSLR model.

The current dashboard liquidity indicators probably do not directly account for SLR/eSLR. They track liquidity flows and market signals, not bank balance-sheet capacity.

SLR/eSLR should be treated as a policy/plumbing note, not a core liquidity indicator.

Add one small line in the Liquidity page or Hermes memo:

Bank Plumbing / SLR: Supportive / Neutral / Restrictive
That is enough.

The reason is that eSLR affects large-bank balance-sheet constraints and Treasury market intermediation, but it is not the same as M2, TGA, RRP, Fed balance sheet or DXY liquidity. U.S. regulators finalised changes to the enhanced supplementary leverage ratio for large banking organisations, with effectiveness from April 1, 2026 and early adoption permitted from January 1, 2026.

So the final dashboard treatment should be:

SLR/eSLR = Hermes weekly context note
Not a main scoring factor
Not a new dashboard module
This avoids overbuilding.

14. Final Dashboard Layout
Allocation Page
Add:

1. Hermes CIO View
2. What-If Outcome
3. Existing Optimal Allocation
4. Existing Live Macro Factors
5. Existing Heat Map
6. Existing Stress Test
7. Existing High Conviction
Keep the order clean.

The user should see the CIO view first.

Liquidity Page
Keep existing:

DXY
US M2
RRP
TGA
Fed Balance Sheet
Global M2 Proxy
Add only one small note:

Bank Plumbing / SLR: Supportive / Neutral / Restrictive
No full SLR module.

Cycle Page
Keep existing:

Business Cycle
Historical Playbook
Macro Season
Scenario Engine
No big change needed.

15. Acceptance Criteria
The final build is complete when:

1. Steno Mirror is removed.
2. No ETH/SOL/altcoin logic exists.
3. No new BTC directional module exists.
4. Hermes CIO View appears at the top of Allocation.
5. Dashboard outputs one Risk Budget score from 0–100.
6. Risk Budget maps to deployment and cash reserve.
7. What-If Outcome section exists.
8. User can input £100k, asset/basket, start date and end date.
9. Output shows ending value, return, annualised return and max drawdown.
10. Framework Portfolio outcome exists.
11. Hermes can call the What-If engine.
12. Hermes sends one weekly CIO message.
13. MIT report is used as context only.
14. SLR/eSLR is shown only as a small policy/plumbing note.
15. No new tabs are added except removing Steno Mirror.
Final Direction
The final build should be:

Existing dashboard
+ Hermes CIO View
+ Risk Budget
+ What-If Outcome
+ Weekly Hermes message

That is the cleanest version.

You already have the sophisticated part. The missing piece is making the dashboard answer the practical capital question:

“What is the current risk stance, and what would my money have done if I followed the framework?”


--