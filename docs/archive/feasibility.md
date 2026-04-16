# Feasibility Analysis — June

---

## What's Being Built
An AI system that:
1. Reads 1-2 Real Vision PDFs daily and extracts macro intelligence from 4 analysts
2. Tracks thesis changes over time and synthesizes cross-analyst views
3. Provides mechanical technical signals for BTC, HYPE, and 5 equities
4. Alerts Daniel when high-conviction signals fire

---

## Feasibility by Component

### Fred (Research Agent)

| Component | Feasible? | Risk | Notes |
|-----------|-----------|------|-------|
| PDF text extraction | ✅ Yes | Low | PyMuPDF/pdfplumber handle RV PDFs well |
| Analyst identification | ✅ Yes | Low | LLM can identify author from content/headers |
| Claim extraction (10 questions) | ✅ Yes | Medium | Quality depends on prompt engineering. PDFs don't always address all 10 questions — the LLM must correctly identify "no data for this question" vs. implicit answers. Requires iteration. |
| Thesis change detection | ✅ Yes | Medium | Needs a structured DB of prior positions. Diffing works well for explicit claims ("BTC to $200k") but is harder for nuanced thesis shifts. |
| Cross-analyst synthesis | ✅ Yes | Medium | This is where the real value is. LLM can do this well given structured inputs from the extraction step. |
| Daily briefing generation | ✅ Yes | Low | Templated output from structured data. Straightforward. |

**Overall: Feasible. The quality ceiling is high but requires prompt iteration with real PDFs.**

### Warren (Technical Agent)

| Component | Feasible? | Risk | Notes |
|-----------|-----------|------|-------|
| TradingView webhook receiver | ✅ Yes | Low | Standard webhook endpoint. Well-documented. |
| PineScript alert configuration | ✅ Yes | Low | But requires manual setup on Daniel's TradingView account |
| TD Sequential in Python | ✅ Yes | Low | Well-known algorithm. Multiple open-source implementations. |
| RSI calculation | ✅ Yes | Low | Standard indicator. Twelve Data provides it directly. |
| Supertrend calculation | ✅ Yes | Low | Standard ATR-based indicator. |
| Support/resistance levels | ✅ Yes | Low-Med | Swing high/low detection. Reasonable accuracy. |
| /levels endpoint | ✅ Yes | Low | FastAPI endpoint aggregating the above. |
| Scoring engine | ✅ Yes | Low | Simple threshold logic. |
| Telegram alerts | ✅ Yes | Low | python-telegram-bot library. Well-documented. |
| Backtest engine | ✅ Yes | Medium | yfinance for data. The engine itself is straightforward but results need careful interpretation. |
| Signal logger + 20-day outcomes | ✅ Yes | Low | DB write on signal, cron job for outcome fill. |
| Chameleon LV/HV replication | ❌ No | - | Proprietary. Cannot replicate in Python. Use via TV webhooks only. |

**Overall: Very feasible. Most components are standard and well-understood.**

---

## What Could Go Wrong

### Fred Risks

**1. PDF format changes**
Real Vision could change their PDF layout, breaking extraction. Mitigation: use LLM-based extraction (robust to format changes) rather than regex/template-based parsing.

**2. Implicit vs. explicit claims**
Analysts don't always state positions explicitly. "I'm watching the ISM closely" is not the same as "ISM will rise to 58." The LLM needs to distinguish between: stated positions, implied positions, and mere observations. This is the hardest prompt engineering challenge.

**3. Thesis drift vs. thesis change**
Is a subtle shift in tone a thesis change? If Raoul goes from "extremely bullish" to "bullish," is that a change worth flagging? Requires calibration with Daniel on what threshold matters.

**4. Missing context**
A PDF might reference a chart, video, or prior report that Fred doesn't have access to. The LLM will need to flag when it can't fully assess a claim due to missing context.

### Warren Risks

**1. Chameleon access**
If Daniel doesn't have Chameleon LV/HV on TradingView, the BTC/HYPE scoring drops from 4 signals to 2 (TD9 + RSI only). This significantly reduces the framework's fidelity to Coutts' actual system.

**2. False signals from mechanical rules**
TD9 fires in choppy markets too. Without a regime filter (trending vs. ranging), expect false signals. Mitigation: use Fred's macro context as a filter — only trust Warren's buy signals when Fred says the macro backdrop supports it.

**3. Twelve Data rate limits**
Free tier: 800 calls/day. 7 tickers × multiple timeframes × periodic checks = potential overage. Paid tier ($29/mo for 5000/day) likely needed.

**4. Supertrend ≠ Chameleon**
For non-TV assets, Supertrend replaces Chameleon. These are different indicators with different signals. The scoring systems are not directly comparable across TV and Python assets. Accept this gap.

---

## Agent vs. Pipeline — The Right Answer

**Warren = deterministic pipeline.** No LLM reasoning needed. Data in → indicators calculated → score → alert. This should be pure Python with no AI.

**Fred = constrained agentic workflow.** Each step requires LLM reasoning, but the steps themselves are fixed:

```
1. Ingest PDF → extract text              (no LLM)
2. Identify analyst + date                 (LLM — simple classification)
3. Extract claims for 10 questions         (LLM — complex reasoning)
4. Diff against prior positions            (LLM — comparison reasoning)
5. Synthesize cross-analyst view           (LLM — synthesis reasoning)
6. Generate briefing                       (LLM — templated generation)
```

This is NOT an open-ended agent that decides what to do next. The workflow is fixed. The LLM provides reasoning within each step, but the orchestration is code.

Why not a fully autonomous agent? Because:
- Predictability matters. Daniel needs to trust the output.
- Debugging matters. When a briefing is wrong, you need to know which step failed.
- Cost matters. Open-ended agents burn tokens exploring dead ends.

---

## Competitive Landscape

| Product | What They Do | How June Differs |
|---------|-------------|-----------------|
| Toggle AI | AI macro research for institutions | Toggle is general-purpose. June is specific to 4 analysts Daniel already follows. |
| Real Vision MIT | Bittel's allocation tool | MIT is one framework. June synthesizes across all 4 analysts. |
| Reflexivity Research | Manual crypto macro research | Manual, single-analyst. June is automated, multi-analyst. |
| TradingView signal bots | TD Sequential alerts via Telegram | Commodity. June adds the macro context layer (Fred → Warren integration). |
| Numerai / QuantConnect | Crowdsourced quant signals | Completely different approach. Quant, not macro-fundamental. |

**June's edge:** Not the signals themselves (those are commodity), but the synthesis of 4 specific analysts' macro views into actionable intelligence, combined with mechanical entry signals. The value is in the distillation + cross-referencing.

---

## Cost Estimate (Monthly)

| Item | Cost |
|------|------|
| Claude API (Fred: ~50 PDFs/mo × ~$0.50 each) | ~$25 |
| Claude API (synthesis, 30 briefings/mo) | ~$15 |
| Twelve Data API (paid tier) | $29 |
| Server (small VPS) | $10-20 |
| TradingView (Daniel's existing sub) | $0 (already paid) |
| Real Vision (Daniel's existing sub) | $0 (already paid) |
| **Total** | **~$70-90/mo** |

---

## Timeline Estimate

| Phase | Scope | Duration |
|-------|-------|----------|
| Phase 1: Warren | Webhooks, indicators, /levels, scoring, alerts | ~1 week |
| Phase 2: Fred | PDF ingestion, extraction, diffing, synthesis | ~1 week |
| Phase 3: Integration | Fred → Warren, backtest, signal logger, polish | ~3 days |
| Phase 4: Iteration | Prompt tuning with real PDFs, false signal analysis | Ongoing |

---

## Open Questions for Daniel

1. Does he have Chameleon LV v2.0 / HV v2.0 on his TradingView?
2. Preferred alert channel? (Telegram / Discord / SMS / email)
3. Preferred briefing format? (PDF / Telegram message / web dashboard)
4. How many historical PDFs available for initial baseline?
5. Any other analysts to add beyond the 4?
6. Specific assets to add/remove from the watchlist?
