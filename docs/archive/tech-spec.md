# Tech Spec — June

AI-native hedge fund intelligence system. Two agents: **Fred** (research) and **Warren** (technical signals).

---

## System Overview

```
Real Vision PDFs ──→ Fred (Research Agent) ──→ Daily Briefing
                                            ──→ Thesis Tracker
                                            ──→ Strategy Recommendations ──→ Warren

TradingView Webhooks ──→ Warren (Technical Agent) ──→ Alerts to Daniel
Twelve Data / CCXT   ──→ Warren                   ──→ /levels API
Yahoo Finance        ──→ Warren                   ──→ /backtest API
```

---

## Fred — Research Agent

### Purpose
Ingest 1-2 Real Vision PDFs per day. Extract, structure, and track the macro views of 4 analysts. Synthesize into a daily briefing that answers 10 explicit questions. Detect thesis changes over time.

### Analysts Tracked
| Analyst | Focus | Key Framework |
|---------|-------|---------------|
| Raoul Pal | Macro liquidity, Everything Code | Global liquidity → asset prices, 5.4yr cycle |
| Julien Bittel | Business cycle quant | MIT: 4 seasons (Spring/Summer/Fall/Winter) via growth + inflation |
| Jamie Coutts | Crypto technicals | DeMark Sequential + Chameleon + RSI re-entry framework |
| Andreas Steno Larsen | Nowcasting | 3 pillars (inflation/growth/liquidity) → 8 macro regimes |

### Workflow (fixed steps, agentic reasoning within each)

```
Step 1: PDF Ingestion
  - Input: PDF file (uploaded or watched from a directory)
  - Extract text (PyMuPDF / pdfplumber)
  - Identify author(s) and publication date

Step 2: Claim Extraction
  - For each of the 10 questions below, extract any relevant claims
  - Include direct quotes with page references
  - Tag each claim with: analyst, date, asset (if applicable), conviction (if stated)

Step 3: Thesis Diffing
  - Load analyst's last known positions from DB
  - Compare new claims against prior claims
  - Flag: NEW position, CHANGED position, REINFORCED position, CONTRADICTED position

Step 4: Cross-Analyst Synthesis
  - Where do analysts agree? (high conviction signal)
  - Where do they disagree? (flag for Daniel)
  - Generate consensus view with dissent noted

Step 5: Output
  - Structured JSON (for DB storage + Warren consumption)
  - Markdown briefing (for Daniel to read)
  - Strategy recommendations extracted (passed to Warren)
```

### The 10 Questions

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

### Data Model — Analyst Positions

```python
class AnalystClaim:
    analyst: str          # "raoul_pal" | "julien_bittel" | "jamie_coutts" | "andreas_steno"
    date: datetime
    source_pdf: str       # filename
    question_id: int      # 1-10, or null if general
    claim: str            # the actual claim text
    direct_quote: str     # verbatim from PDF
    asset: str | None     # "BTC", "SPX", "gold", etc.
    direction: str | None # "bullish" | "bearish" | "neutral"
    conviction: int | None # 1-10 if stated
    timeframe: str | None # "30d" | "90d" | "Q3_2026" etc.

class ThesisChange:
    analyst: str
    date: datetime
    previous_claim_id: int
    new_claim_id: int
    change_type: str      # "new" | "changed" | "reinforced" | "contradicted"
    summary: str          # human-readable description of what changed
```

### Output — Daily Briefing Format

```markdown
# Daily Macro Briefing — {date}

## Sources Ingested
- {pdf_name} by {analyst} ({date})

## Consensus View
{1-2 paragraph synthesis of where all 4 analysts align}

## Key Disagreements
{where analysts diverge — this is high signal}

## The 10 Questions
### 1. Biggest macro risk next 90 days?
- **Raoul Pal**: {claim} (conviction: X, date: Y)
- **Bittel**: {claim or "no update"}
- **Steno**: {claim}
- **Consensus**: {synthesis}

... (repeat for all 10)

## Thesis Changes Since Last Briefing
- ⚡ Raoul Pal CHANGED position on {X}: was {old}, now {new}
- ✅ Steno REINFORCED: {claim}

## Strategy Recommendations (for Warren)
- {extracted actionable recommendations with asset + direction + timeframe}
```

---

## Warren — Technical Agent

### Purpose
Provide real-time technical signals and scoring for Daniel's asset universe. Two signal sources: TradingView PineScript webhooks (BTC + HYPE) and Python-calculated indicators (everything else).

### Asset Universe
| Asset | Type | Data Source | Signal Source |
|-------|------|-------------|---------------|
| BTC | Crypto | CCXT | TradingView PineScript webhooks |
| HYPE | Crypto | CCXT | TradingView PineScript webhooks |
| MU | Equity | Twelve Data | Python indicators |
| LIN | Equity | Twelve Data | Python indicators |
| UUUU | Equity | Twelve Data | Python indicators |
| CRCL | Equity | Twelve Data | Python indicators |
| COIN | Equity | Twelve Data | Python indicators |

### Architecture

```
┌─────────────────────────────────────────────────┐
│                   FastAPI Server                 │
│                                                  │
│  POST /webhook/tradingview  ← PineScript alerts  │
│  GET  /levels/{ticker}      → scoring + levels   │
│  GET  /backtest/{ticker}    → backtest results   │
│  GET  /signals/log          → signal history     │
│                                                  │
│  ┌──────────────┐  ┌──────────────────────────┐  │
│  │  Webhook      │  │  Indicator Engine        │  │
│  │  Receiver     │  │  - TD Sequential (Python)│  │
│  │  (BTC, HYPE)  │  │  - RSI (Twelve Data)     │  │
│  │               │  │  - Supertrend (Python)   │  │
│  └──────┬───────┘  └──────────┬───────────────┘  │
│         │                     │                  │
│         ▼                     ▼                  │
│  ┌──────────────────────────────────────────┐    │
│  │            Scoring Engine                │    │
│  │  BTC/HYPE: TD9 + Chameleon + RSI + S/R   │    │
│  │  Equities: TD9 + Supertrend + RSI         │    │
│  └──────────────┬───────────────────────────┘    │
│                 │                                │
│                 ▼                                │
│  ┌──────────────────────────────────────────┐    │
│  │          Alert Engine                    │    │
│  │  Strong buy → immediate alert to Daniel  │    │
│  │  Watch → morning brief                   │    │
│  │  Wait → log only                         │    │
│  └──────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
```

### Signal Source 1: TradingView PineScript Webhooks (BTC + HYPE)

**Prerequisite:** Daniel must have Chameleon LV v2.0 (BTC) and Chameleon HV v2.0 (HYPE) on his TradingView charts. These are proprietary invite-only indicators.

**Alerts to configure on TradingView:**

BTC (Chameleon LV v2.0):
| # | Alert | Timeframe | Meaning |
|---|-------|-----------|---------|
| 1 | TD Sequential bar 9 completes | Weekly | Primary re-entry signal |
| 2 | Chameleon LV flips GREEN | Weekly | Primary confirmation |
| 3 | TD Sequential bar 9 completes | Daily | Shorter-term alert |
| 4 | Chameleon LV flips GREEN | Daily | Daily confirmation |
| 5 | Chameleon LV flips RED | Daily/Weekly | Exit / caution |
| 6 | RSI drops below 40 | Daily | Oversold confirmation |

HYPE (Chameleon HV v2.0):
| # | Alert | Timeframe | Meaning |
|---|-------|-----------|---------|
| 1 | TD Sequential bar 9 completes | Weekly | Primary re-entry signal |
| 2 | Chameleon HV flips GREEN | Weekly | Primary confirmation |
| 3 | TD Sequential bar 9 completes | Daily | Shorter-term alert |
| 4 | Chameleon HV flips GREEN | Daily | Daily confirmation |
| 5 | Chameleon HV flips RED | Daily/Weekly | Exit / caution |
| 6 | RSI drops below 40 | Daily | Oversold confirmation |

**Webhook payload format:**
```json
{
  "asset": "BTC",
  "timeframe": "1W",
  "signal": "TD9",
  "chameleon_version": "LV",
  "chameleon_state": "GREEN",
  "rsi": 38.2,
  "price": 70115,
  "timestamp": "2026-04-07T12:00:00Z"
}
```

**BTC/HYPE Scoring (Jamie Coutts framework):**
| Score | Conditions | Action |
|-------|-----------|--------|
| 4/4 | Weekly TD9 + Weekly Chameleon GREEN + RSI <40 + at support | **STRONG BUY** → alert Daniel immediately |
| 3/4 | Any 3 of above | **WATCH** → morning brief |
| 2/4 or less | | **WAIT** → log only |

Special combo: Weekly TD9 + Weekly Chameleon GREEN firing together = maximum conviction → alert immediately regardless of other conditions.

### Signal Source 2: Python Indicators (All Other Assets)

**Data sources:**
- Equities (MU, LIN, UUUU, CRCL, COIN): Twelve Data API
- Crypto (any not on TV): CCXT
- Backtesting: Yahoo Finance (yfinance)

**Indicators calculated in Python:**

**TD Sequential (Tom DeMark):**
- Count consecutive closes higher/lower than close 4 bars ago
- Setup phase: bars 1-9
- Bar 9 = potential exhaustion signal
- Implemented on OHLCV data from any source

**RSI (Relative Strength Index):**
- Standard 14-period RSI
- Return actual number (e.g., 38.2), not categories
- Source: Twelve Data API or calculated from OHLCV

**Supertrend:**
- ATR-based trend indicator (replaces Chameleon for non-TV assets)
- Parameters: period=10, multiplier=3.0 (standard)
- Returns: GREEN (bullish) or RED (bearish)

**Support / Resistance:**
- Calculated from recent swing highs/lows
- 3 support levels, 3 resistance levels

**Equity Scoring:**
| Score | Conditions | Action |
|-------|-----------|--------|
| 3/3 | TD9 + Supertrend GREEN + RSI <40 | **BUY** → alert Daniel |
| 2/3 | Any 2 of above | **WATCH** → morning brief |
| 1/3 or less | | **WAIT** → log only |

### API Endpoints

#### `POST /webhook/tradingview`
Receives TradingView alert webhooks. Scores signal. Triggers alerts if threshold met.

#### `GET /levels/{ticker}`
Returns current technical levels for any asset.

**Example response:**
```
MU — $355
TD Sequential: 5/9 ⚡
Supertrend: GREEN ✅
RSI: 38.2 ✅
Support: $340, $320, $300
Resistance: $375, $395, $415

Score: 2/3 — WATCH
```

**Target latency:** <5 seconds

#### `GET /backtest/{ticker}?period=2y`
Runs TD Sequential + RSI backtest on historical data.

**Example response:**
```
MU — 2 Year Backtest
Strategy: Buy on TD9 + RSI <40
Signals: 12
Win rate: 67%
Avg return (20d): +4.2%
Best entry zones: $310-$320, $340-$350
```

**Target latency:** <10 seconds
**Data source:** Yahoo Finance (yfinance)

#### `GET /signals/log`
Returns all logged signals with timestamps, prices, and outcomes.

```json
[
  {
    "ticker": "BTC",
    "signal": "TD9",
    "timeframe": "1W",
    "price_at_signal": 70115,
    "date": "2026-03-15",
    "score": "4/4",
    "action": "STRONG_BUY",
    "price_20d_later": 78500,
    "return_20d": "+11.9%"
  }
]
```

### Signal Logger
- Logs every signal with: timestamp, ticker, signal type, price, score, action taken
- After 20 days, records the outcome (price at signal + 20 days)
- Cron job checks daily for signals that are 20 days old and fills in outcomes

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12 |
| Web framework | FastAPI |
| LLM | Claude API (claude-sonnet-4-6 for extraction, claude-opus-4-6 for synthesis) |
| PDF parsing | PyMuPDF (fitz) or pdfplumber |
| Database | SQLite (v1) → PostgreSQL (v2) |
| Market data (equities) | Twelve Data API |
| Market data (crypto) | CCXT |
| Market data (backtest) | yfinance |
| BTC/HYPE signals | TradingView PineScript webhooks |
| Alerts to Daniel | Telegram bot (or webhook to preferred channel) |
| Task scheduling | APScheduler or cron |
| Deployment | Single server (v1) |

---

## Environment Variables

```env
ANTHROPIC_API_KEY=...
TWELVE_DATA_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
WEBHOOK_SECRET=...        # verify TradingView webhook authenticity
DATABASE_URL=sqlite:///june.db
```

---

## Project Structure

```
june/
├── main.py                    # FastAPI app entrypoint
├── pyproject.toml
├── docs/
│   ├── tech-spec.md           # this file
│   ├── analyst-frameworks.md  # detailed analyst research
│   └── feasibility.md         # analysis and tradeoffs
├── fred/                      # research agent
│   ├── __init__.py
│   ├── ingest.py              # PDF text extraction
│   ├── extract.py             # claim extraction via LLM
│   ├── diff.py                # thesis change detection
│   ├── synthesize.py          # cross-analyst synthesis
│   ├── questions.py           # the 10 questions definition
│   ├── prompts/               # LLM prompt templates
│   │   ├── extract_claims.md
│   │   ├── diff_thesis.md
│   │   └── synthesize.md
│   └── models.py              # AnalystClaim, ThesisChange, etc.
├── warren/                    # technical agent
│   ├── __init__.py
│   ├── webhook.py             # TradingView webhook receiver
│   ├── indicators/
│   │   ├── td_sequential.py   # DeMark Sequential implementation
│   │   ├── rsi.py             # RSI calculation
│   │   ├── supertrend.py      # Supertrend calculation
│   │   └── support_resistance.py
│   ├── scoring.py             # signal scoring engine
│   ├── levels.py              # /levels endpoint logic
│   ├── backtest.py            # backtest engine
│   ├── alerts.py              # Telegram / notification dispatch
│   └── logger.py              # signal logger + outcome tracker
├── db/
│   ├── __init__.py
│   ├── models.py              # SQLAlchemy models
│   └── migrations/
└── tests/
```

---

## Build Order (v1)

### Phase 1 — Warren (Technical Agent) — ~1 week
1. FastAPI skeleton + webhook receiver
2. TD Sequential implementation in Python
3. RSI + Supertrend calculations
4. Support/resistance calculation
5. /levels endpoint
6. Scoring engine
7. Telegram alerts
8. Signal logger

### Phase 2 — Fred (Research Agent) — ~1 week
1. PDF ingestion pipeline
2. Claim extraction prompts (iterate until quality is high)
3. Database for analyst positions
4. Thesis diffing logic
5. Cross-analyst synthesis
6. Daily briefing output

### Phase 3 — Integration + Polish — ~3 days
1. Fred's strategy recommendations → Warren's watchlist
2. Signal logger outcome tracking (20-day cron)
3. Backtest engine
4. End-to-end testing on real PDFs

---

## Open Questions

1. **Chameleon access:** Does Daniel have Chameleon LV v2.0 / HV v2.0 on his TradingView? If not, TV webhook scoring for BTC/HYPE falls back to TD9 + RSI only (2-signal scoring instead of 4).
2. **Alert channel:** Telegram? Discord? SMS? Email?
3. **Briefing delivery:** Does Daniel want the daily briefing as a PDF? Telegram message? Web dashboard?
4. **Historical PDFs:** How many historical PDFs are available for building initial analyst position baselines?
5. **Twelve Data tier:** Free tier is 800 calls/day. With 7 tickers across timeframes, may need paid tier ($29/mo for 5000/day).
