# Technical Signals Layer — Build Guide

Module for existing investment dashboard | v1.2 | July 2026

> v1.2: added Source Intake (Steno Research / Real Vision name ingestion).
> v1.1: fully native engine, on-demand ticker lookup.

---

## 1. Objective

A low-noise technical scoring engine embedded in the existing dashboard. It ingests recommended names from research sources (Steno Research, Real Vision), maintains a watchlist (~30–50 tickers across equities, ETFs, commodities and crypto), scores each name 0–100 for entry quality (and separately for exit risk) on a daily-close basis, supports on-demand lookup of any ticker, and pushes alerts through the Hermes agent only when something genuinely actionable happens.

Technicals are one input in a wider process — this module times entries and exits on names selected fundamentally. **It does not pick stocks.**

**Design principles (non-negotiable):**

- **Fully self-contained.** No inbound webhooks, no scraping, no login automation. Every signal computed in-house from raw price data; source names arrive by email pipeline (§7). If the data API and the service are up, the system works.
- **Daily timeframe engine, weekly context.** No intraday noise.
- **State-change alerts only.** No repeat pings.
- **Human approves every watchlist addition** — one tap.
- **Lean stack.** Build small, extend later.

---

## 2. In scope / out of scope

**In scope (v1):**

- Source intake: weekly ingestion of Steno / Real Vision research via email pipeline → ticker extraction → one-tap review queue → watchlist, with provenance tags
- Watchlist management with source attribution (Steno / RV / Cowan / Own)
- Daily indicator computation and 0–100 entry + exit scoring — all native
- Ticker lookup: search any stock or crypto, run the full stack live, see firing signals
- Dashboard widget: intake queue + ranked watchlist table
- Alert dispatch to Hermes (outbound POST only)

**Out of scope (v1):**

- Website scraping or automated logins of any kind, intraday signals, auto-trading, options data, portfolio tracking, fundamental data, TradingView integration.

---

## 3. Architecture decisions

### 3.1 Native signal engine (no inbound webhooks)

Webhook-based integrations proved fragile in a previous build, so TradingView is removed from the data path entirely:

- **TD Sequential → replicated natively.** DeMark's methodology is public: Buy/Sell Setup (9 consecutive closes vs the close 4 bars earlier), Countdown to 13, setup perfection, TDST levels — all computable from OHLCV. Daily + weekly counts for every ticker, every run, and for any searched ticker on demand. *Acceptance test:* native counts must match the paid TradingView indicator on 10 reference charts (Fred supplies screenshots) before Phase 1 signs off. Fully backtestable.
- **Chameleon → cannot be replicated** (proprietary). Replaced in the score by a native **Regime Composite** at the same weight (§4). Chameleon remains Fred's manual confirmation on the TradingView chart before executing a Prime signal — each ticker has an "Open in TradingView" link.

### 3.2 Source intake by email, not scraping

Steno Research and Real Vision are paywalled platforms behind authenticated logins. Scraping them weekly would mean automating logins past bot protection against changing page structures — strictly more fragile than the webhooks already rejected — and would breach both platforms' terms of use, risking the subscriptions themselves. Neither offers a public API for portfolio data.

The robust route is the channel that already delivers the research to Fred: **email**. Both services email notes/publications to subscribers. Email delivery is stable, authenticated by them, and terms-clean — it's Fred's own subscription content arriving at Fred's own address, parsed for his personal use. Design in §7.

### 3.3 Price data — the single external dependency

- **Twelve Data** (equities, ETFs and crypto in one API) or **Polygon.io** + a crypto source.
- Daily OHLCV, ~300 bars history per ticker; fetched nightly ~21:30 UK; weekly bars aggregated locally; on-demand fetch for lookups (~1–2s), session-cached.
- If the data API is down, the run flags **"stale"** on the dashboard and skips alerts — never scores silently on old data.

---

## 4. Indicator stack (all native)

| Indicator | Role |
| --- | --- |
| TD Sequential, daily + weekly (native DeMark counts) | Exhaustion/reversal timing — 9 setup, 13 countdown, both directions |
| Regime Composite | Trend/regime state: price vs 21 EMA / 50 / 200 SMA structure, 50>200 slope, 20-day ROC, ATR trend → Bullish / Neutral / Bearish + flip events |
| EMA/SMA structure (21 EMA, 50 & 200 SMA) | Trend context and pullback zones |
| ATR-normalised stretch + z-score vs 50D | Mean-reversion deviation, comparable across asset classes |
| RSI(14), daily + weekly confluence | Pullback-in-uptrend entries; divergence for exits |
| Relative volume (RVOL) | Capitulation/confirmation filter |
| 200-day regime gate (weekly confirmed) | Not scored — caps counter-trend entry scores at 60 |

Nothing else in v1.

---

## 5. Scoring model

### Entry Score (0–100)

| Component | Max | Logic (summary) |
| --- | --- | --- |
| TD Sequential | 25 | Daily buy setup 7–8: 10 · Daily 9 complete: 18 · Daily 13 countdown: 25 · Weekly 9/13 confluence: +5 (capped 25) |
| Regime Composite | 25 | Bearish→Neutral flip: 12 · Neutral→Bullish flip: 25 · Established Bullish, pullback context: 15 |
| Mean reversion stretch | 20 | Z ≤ −2 vs 50D or ≥ 2.5 ATR below 21 EMA: 20 · Z ≤ −1.5: 12 · Z ≤ −1: 6 |
| Trend structure | 15 | Above rising 200D: 8 · 50D > 200D: +4 · Pullback into 21 EMA/50D zone: +3 |
| RSI | 10 | Daily < 30 turning up: 10 · Daily < 35 with weekly > 40: 7 · Bullish divergence: 8 |
| Volume | 5 | RVOL ≥ 2 on signal day: 5 · RVOL ≥ 1.5: 3 |

**Regime gate:** below a falling 200D, entry score capped at 60, badged "counter-trend."

**Event freshness:** TD completions and regime flips score fully for 3 trading days, then decay linearly to zero over 10.

**Bands:** 85–100 Prime · 70–84 Strong · 50–69 Watch · <50 no signal.

### Exit Score (0–100), mirrored

TD sell 9/13 (25) · Regime deterioration (25) · stretch ≥ +2 z / +2.5 ATR above 21 EMA (20) · loss of 21 EMA / 50D on close (15) · daily RSI > 75 or weekly bearish divergence (10) · distribution volume (5). Exit alerts only on names toggled **held**.

Weights and thresholds live in a config file. Phase 4 (optional): because the stack is fully native, replay the engine over historical OHLCV and report hit-rates per band per asset class, then tune.

---

## 6. Ticker lookup — on-demand analysis

Search bar in the widget header; type any equity or crypto symbol:

1. Autocomplete via the data API reference endpoint.
2. Engine fetches 300 daily bars live, runs the identical scoring code (< 2s).
3. Result card: entry score + band, exit score, "Firing now" list in plain terms ("TD buy setup on bar 8 of 9 · Daily", "Z-score −1.7 vs 50D"), component breakdown, mini chart with 21E/50/200.
4. Actions: "Add to watchlist" (with source = Own or chosen) · "Open in TradingView".

Lookups never trigger alerts.

---

## 7. Source intake — Steno / Real Vision names

Pipeline (email-based, zero scraping):

1. **Dedicated intake mailbox** (e.g. `signals@`your domain, or a Gmail alias). Fred sets one-time forwarding rules: Steno Research emails → intake; Real Vision publication emails → intake. (~10 min setup, done once.)
2. **Inbound email service** (Postmark / Mailgun / SendGrid inbound — pick whatever the stack already uses) converts each email to a JSON POST at `/api/intake/email`. Attached/linked PDFs are pulled in where the mail includes them.
3. **Extraction:** the service passes the note's text to an LLM extraction call (Claude API) with a strict JSON schema: `{source, published_date, actions:[{ticker, action: add|trim|exit|reiterate, instrument_note}]}`. Non-portfolio emails (event invites, marketing) are classified out and dropped silently.
4. **Review queue** — nothing auto-enters the watchlist. Detected names land in a pending strip at the top of the widget: "STENO · Portfolio note · 26 Jul — +HPQ, +S, trim ONDS → [Approve all] [Review] [Dismiss]". One tap approves; approved adds are tagged with source + date. This keeps LLM extraction errors and non-actionable mentions out of the system, and keeps the fundamental-selection decision where it belongs — with Fred.
5. **Manual intake fallback:** a "Paste a note" box on the same strip — paste text or upload a PDF from any source (Cowan videos, anything), same extraction, same queue. Bulletproof when an email format changes or a source isn't emailed.
6. **Weekly rhythm:** Sunday 18:00 UK digest line via Hermes: "Intake this week: 2 notes parsed, 3 names pending approval" (only if pending > 0 — silence otherwise).

**Provenance on the watchlist:** every ticker carries a source tag (STENO / RV / COWAN / OWN) and date added, filterable. When a source note says exit/trim a name currently on the watchlist, the queue shows a removal suggestion — again one-tap, never automatic.

**Failure surface:** if no email arrives from a source for 21 days, the dashboard shows a quiet "no Steno note received in 3 weeks" notice — catches a broken forwarding rule without nagging.

---

## 8. Alerting — low-noise rules

1. Alerts fire only on **band transitions** (Watch → Strong, Strong → Prime, Exit crossing 70). Never on wobble within a band.
2. Max **one alert per ticker per day**, hard cap; multiple drivers on one name merge into one message.
3. One **daily digest ~21:45 UK**: top 5 entry scores, Prime signals, exit warnings on held names, TD counts line. Real-time pushes reserved for Prime (≥85) transitions.
4. **TD completion override (default ON):** TD9/TD13 completions, daily and weekly, both sides, always notify regardless of band. Weekly completions and any TD13 → real-time push; daily TD9s → digest "TD counts" line. Config: `td_override.enabled`, `td_override.realtime_scope`, per-ticker opt-out.
5. **Intake digest:** Sunday 18:00 UK, only when names are pending (see §7).
6. **Payload to Hermes:** `{type: signal|intake, ticker, direction, score, band, drivers:[...], timestamp}` — outbound POST, retried, delivery logged. Hermes owns transport.

---

## 9. Dashboard widget spec

Single embedded component, dark-navy aesthetic (companion HTML mockup is the visual spec):

- **Header:** title, equity/crypto regime chips, last-run stamp, ticker search field.
- **Intake strip** (only visible when pending): pending source detections with Approve / Review / Dismiss; "Paste a note" affordance.
- **Ranked watchlist table:** ticker · asset-class + source tag · entry score as segmented component bar · band badge · signal badges · exit score (held) · 90-day sparkline · days since last signal.
  - *Row expand / lookup card:* component breakdown, mini chart 21E/50/200, "Firing now" list, signal history, data-freshness line, Open-in-TradingView.
  - *Filters:* asset class, band, held-only, source.

---

## 10. Architecture

```
Steno / RV emails ─► intake mailbox ─► inbound email service
                                                     │ JSON POST
Twelve Data API (daily OHLCV)                        ▼
           │ pull (nightly + on-demand)        /api/intake/email ─► LLM extraction ─► review queue
           ▼                                         │ (approve)
FastAPI service ────────────────────────────────┴──► Postgres (Supabase)
    ├─ daily job 21:30 UK: fetch OHLCV → compute natives (TD counts,
    │     Regime Composite, stretch, RSI, RVOL) → score → persist
    ├─ /api/watchlist            (scores, history, sources)
    ├─ /api/lookup/{symbol}      (on-demand scoring)
    ├─ /api/intake/*             (queue: list / approve / dismiss / paste)
    └─ alert dispatcher ──► Hermes webhook (outbound only)
```

One Python service + one cron job + one DB + one inbound-email provider. Tables: `watchlist`, `sources`, `intake_events`, `ohlcv_daily`, `scores_daily`, `signal_events`, `alerts_log`.

The only inbound endpoint is the email provider's signed POST (verify provider signature) and the dashboard's own authenticated API.

---

