# Technical Signals Layer — build log

Implementation notes for `docs/technical-signals-layer-scope.md`.
Section references (§) throughout point at that scope doc.

Built 2026-07-29/30. **All of scope doc v1 is implemented**, including the
optional §5 Phase 4 backtest. 361 tests added; full suite 477 green.

The one thing outstanding is §3.1's reference-chart sign-off, which needs Fred's
screenshots — see `docs/td-sequential-verification.md` for what we did to ship
safely without it.

---

## Status

| Scope doc area | State |
| --- | --- |
| §3.3 Price data (OHLCV layer, staleness) | **Done** — yfinance behind a provider protocol |
| §3.1 TD Sequential (native DeMark) | **Done** — pending the reference-chart sign-off |
| §4 Indicator stack | **Done** — all six §4 rows |
| §5 Scoring model (entry + exit, gate, freshness, bands) | **Done** |
| §6 Ticker lookup | **Done**; §6.1 autocomplete limited — see below |
| §6.3 Mini chart (price · 21E · 50 · 200) | **Done** via `/api/signals/bars` |
| §7 Source intake | **Done** — incl. §7.2 PDF attachments |
| §8 Alerting | **Done** |
| §9 Dashboard widget | **Done** at `/signals` |
| §5 Phase 4 backtest replay | **Done** — `/api/signals/backtest` |
| §3.1 TD reference-chart sign-off | **Outstanding** — needs the screenshots |

---

## Scope-doc audit (2026-08-09)

Every concrete claim in the scope doc was exercised against a **live server with
real market data**, not just unit tests. Script kept at
`scratchpad/audit.py`; result **58/60**.

Cross-asset live scoring (§1) verified on one name per class:
`SPY 41/30 · NVDA 34/22 · GC=F 17/20 · AAPL 15/27 · BTC-USD 12/12` (entry/exit).
GC=F scores despite having no volume — §5's volume row awards 0 and says so.

**Three gaps the audit found, all now fixed:**

1. **§3.3 "session-cached" was not implemented.** `engine.lookup()` wrote to the
   bar cache but never read it, and `LOOKUP_CACHE_TTL_SECONDS` was dead config.
   Every lookup re-fetched. Now served from cache within the TTL:
   measured 0.599s cold → **0.004s warm**, identical scores on both paths.
   `?fresh=true` bypasses it, which is what the widget's "Live re-score" button
   passes — a button labelled *live* must not return a cached number.
2. **§7.4's "[Review]" button did not exist.** Only Approve all / Dismiss were
   built. Review now opens a per-name panel with each ticker, its action verb and
   the source's rationale, and approves a **subset** — the check on LLM
   extraction errors that §7.4 exists for. `POST /intake/{id}/approve?tickers=…`.
3. **§7.4 queue format diverged from the doc.** Rendered `~ONDS`; the doc's
   example is `trim ONDS`. Now matches verbatim:
   `Portfolio note · 26 Jul — +HPQ, +S, trim ONDS`.

**The two remaining are the known ones, neither fixable here:**

| § | Claim | Why |
| --- | --- | --- |
| §3.1 | reference-chart acceptance test | Needs Fred's counts. Mitigated — see `td-sequential-verification.md` |
| ~~§6.1~~ | ~~autocomplete~~ | **CLOSED** — live with the Twelve Data key |


---

## Twelve Data — live API findings (2026-08-11)

Tested against the real API with a live key, not documentation. **Six things
broke or surprised**, all now fixed. The unit tests had passed against a stubbed
response shape the API does not actually use — a reminder that an integration is
not tested until it has touched the real endpoint.

| # | Finding | Fix |
| --- | --- | --- |
| 1 | Unknown symbols return **HTTP 404**, not a 200 with `status:"error"`. Our code only inspected the body, so a bad ticker raised ProviderError → §3.3 treated a nonexistent symbol as an outage | `_request()` maps 404 → empty series |
| 2 | **8 requests/minute, 800/day** on the basic plan (via `/api_usage`). A 50-name run dies on symbol 9 | Module-level sliding-window throttle + 429 retry. Verified: 10/10 symbols, 62.9s, recovered from a real 429 |
| 3 | **Symbol format differs** for everything but equities/ETFs: `BTC/USD` not `BTC-USD`, `XAU/USD` not `GC=F`, `EUR/USD` not `EURUSD=X`. Indices unavailable on basic | `to_twelvedata_symbol()` translates at the provider boundary; the DB keeps canonical yfinance-style symbols |
| 4 | **Search returns one row per venue** — "SPY, SPY, SPY, SPY" | Deduped by symbol, US listings sorted first |
| 5 | The key was passed as a **query parameter**, and httpx logs full URLs at INFO — so it landed in journalctl on every call | Moved to `Authorization: apikey …` header |
| 6 | **Intraday volume lags badly.** Mid-session AAPL: 2% of median volume from Twelve Data vs 44% from yfinance. Prices agree to the cent; volume does not | §1 scores on a "daily-close basis" — partial bars now detected and warned |

### Provider comparison, same engine, live data

| Symbol | Provider | Entry | Exit | Regime | TD daily state |
| --- | --- | --- | --- | --- | --- |
| AAPL | yfinance | 15 | 32 | Neutral | buy setup 2, cd sell 8 |
| AAPL | twelvedata | 15 | 32 | Neutral | buy setup 2, cd sell 8 |
| SPY | yfinance | 35 | 18 | Bullish | sell setup 9, cd sell 0 |
| SPY | twelvedata | 35 | 18 | Bullish | sell setup 9, cd sell 0 |
| BTC-USD | yfinance | 8 | 20 | Neutral | buy setup 2, cd buy 3 |
| BTC-USD | twelvedata | 8 | 20 | Neutral | buy setup 2, cd buy 3 |

Identical scores, regimes and TD counts on both feeds, including crypto. RSI and
z-score differ only in the second decimal. The engine is genuinely
provider-independent — and this is the closest thing we have to independent
corroboration of the TD counts while §3.1's reference charts are outstanding.

### The default is now a hybrid

`SEARCH_PROVIDER` defaults to Twelve Data when a key exists; `OHLCV_PROVIDER`
stays yfinance. Rationale: the two have opposite strengths, and one search call
per keystroke-batch is trivially within 8/min while the nightly run stays
unthrottled and keeps the better intraday volume.

    bars   → yfinance    (no rate limit, better intraday volume, no search)
    search → twelvedata  (real prefix search = §6.1 closed, rate limit irrelevant)

Set `SIGNALS_OHLCV_PROVIDER=twelvedata` to move bars over too — everything works,
it is just slower and its intraday volume is worse.

### Two more fixes the live run surfaced

* **`POST /api/signals/run` could 504.** On Twelve Data a 50-name run takes ~6
  minutes, past nginx's 60s proxy timeout — returning a 504 while the run
  continues invisibly. The endpoint now estimates and refuses above 45s, pointing
  at the nightly cron or `?symbol=`. `?force=true` overrides.
* **Asset class defaulted to "equity".** `GC=F` and `BTC-USD` landed on the
  watchlist labelled equities, so §9's asset-class filter silently lied. Now
  inferred via `classify_asset()` unless passed explicitly.


---

## Module map

```
app/services/signals/
  config.py         §3.3 §4 §5 §7 §8   weights, thresholds, paths — all tunables
  db.py             §10                SQLite schema, migrations, connection policy
  providers.py      §3.3               OHLCV fetch behind OHLCVProvider protocol
  bars.py           §3.3               Bar/BarSeries, weekly aggregation, staleness
  indicators.py     §4                 RSI/ATR/EMA/SMA/z-score/RVOL/Regime Composite
  td_sequential.py  §3.1 §4            native DeMark: setup, countdown, perfection, TDST
  scoring.py        §5                 entry + exit scoring, freshness decay, regime gate
  store.py          §10                watchlist, bars, scores, signal events, alerts log
  engine.py         §10 §6             orchestration: nightly run + on-demand lookup
  alerts.py         §8                 band transitions, caps, TD override, digest, dispatch
  intake.py         §7                 email webhook, extraction, review queue, silence notice
  api.py            §10                /api/signals/* routes
static/
  signals.html/css/js  §9              the widget, served at /signals
```

Wired into `app/main.py`: router included, `init_db()` at import, `/signals`
page route, nav links on all pages, and three cron jobs —
`_schedule_signals_daily` (score + alert), `_schedule_signals_digest` (§8.3),
`_schedule_intake_digest` (§7.6).

---

## Decisions that depart from the scope doc

### SQLite instead of Postgres/Supabase (§10)

§10 specifies Postgres. We used SQLite: nothing else in this repo uses a database
(the Steno layer is JSON files), a hosted Postgres would be a new deployment
dependency on the EC2 box, and at §1's stated size (~30-50 tickers) a
single-writer SQLite file is comfortably sufficient. Schema is plain SQL with no
SQLite-only types, so a port is a dialect change rather than a redesign.

All seven §10 tables exist and are all now written to. `db.py` carries a
`MIGRATIONS` list because `CREATE TABLE IF NOT EXISTS` is a no-op on an existing
table — a column added to `SCHEMA` would never reach a database created by an
earlier version. `td_optout` (§8.4) went in that way.

### yfinance instead of Twelve Data (§3.3)

Deliberate first step, not a substitution. Everything consumes the
`OHLCVProvider` protocol, so adding Twelve Data is a new class plus a config
flag. Two things improve on the switch (tracked at `config.OHLCV_PROVIDER`):

1. **§6.1 autocomplete.** Twelve Data has a reference/symbol-search endpoint;
   yfinance has no supported equivalent. `/api/signals/search` returns
   `autocomplete: false` and the widget shows "Exact symbol · autocomplete needs
   Twelve Data" rather than silently offering a dead dropdown.
2. **A meaningful staleness contract.** yfinance can return an empty frame for a
   valid symbol under rate limiting, indistinguishable from a delisted ticker. We
   treat empty-and-unexplained as a fetch failure — the conservative reading, but
   it does mean a 503 where a 404 might be correct.

### Hermes transport falls back to Telegram (§8.6)

§8.6 says "outbound POST ... Hermes owns transport". No Hermes inbound webhook
exists in this deployment — the Hermes agent connects *to* this service over MCP.
So: if `SIGNALS_HERMES_WEBHOOK_URL` is set we POST the §8.6 payload there;
otherwise we use the repo's existing Telegram client, which is what actually
reaches Fred today. Either way delivery is logged to `alerts_log`, so the §8.6
contract holds regardless of sink.

### Routes namespaced under `/api/signals/*`

§10 sketches `/api/watchlist` and `/api/lookup/{symbol}`. The dashboard's `/api/*`
namespace already holds Steno, IBKR, mirror and Hermes routes.

### §9 visual spec

§9 says "companion HTML mockup is the visual spec". That mockup was not supplied,
so the widget is built on the existing design system (`static/djg.css`) and its
tokens, keeping `/signals` continuous with Allocation / Liquidity / Cycle / Steno.
The markup follows §9's element list, so applying the mockup should be a CSS
exercise rather than a rebuild.

---

## Interpretation calls

Where the scope doc specifies a behaviour but not its numbers. Each is a named
constant marked `**[INTERPRETATION]**` in source, and a tuning candidate for the
§5 Phase 4 backtest.

| What | Where | Our reading |
| --- | --- | --- |
| "Pullback into 21 EMA/50D zone" (§5) | `config.PULLBACK_ZONE_TOLERANCE` | Price between the 50 SMA and the 21 EMA, ±2% |
| "loss of 21 EMA / 50D on close (15)" (§5) | `scoring._exit_structure` | Split evenly, 7.5 per leg |
| Regime Composite banding (§4) | `indicators.REGIME_BULLISH_THRESHOLD` | \|composite\| ≥ 3 of 6 inputs to call a regime |
| "ATR trend" direction (§4) | `indicators._atr_trend_signal` | **Contracting** volatility is bullish; a crash must not read as a strengthening regime |
| "50>200 slope" (§4) | `indicators._slope_signal` | Level *and* 20-bar change must agree; a narrowing golden cross scores 0 |
| Entry-side downgrades (§8.1) | `alerts.is_entry_upgrade` | Do not alert. §8.1's examples are all upgrades; the exit score is the "get out" channel |
| `trim` on approval (§7.4) | `intake.approve` | **No watchlist change** — a source trimming still holds it, and exit timing on a partial position is what §5's exit score is for |
| Regime chips (§9) | `signals.js:renderRegimeChips` | Modal regime per asset class across scored names, with sample size shown. A summary of tracked names, not an index-level call |

---

## The TD Sequential acceptance test (§3.1) — still outstanding

§3.1: *"native counts must match the paid TradingView indicator on 10 reference
charts (Fred supplies screenshots) before Phase 1 signs off."*

**This has not run. It needs the screenshots.** It is the largest remaining risk
in the engine and it is calendar-blocked, not work-blocked.

What is in place meanwhile: 24 tests pinning the mechanics against
hand-derivable fixtures, including **both directions** of the bar-13 deferral
rule (defers when it should, completes when it should — a one-sided test would
pass on an engine that never prints a 13). Live frequency is the right order:
3-5 daily TD13s per 300 bars on AAPL/SPY/BTC-USD.

Every under-specified rule is a flag on `TDConfig` marked `**[AMBIGUOUS]**`.
**If a reference chart disagrees, work that list before suspecting the counter.**

| Flag | Default | Alternative reading |
| --- | --- | --- |
| `recycle_countdown_on_new_setup` | `False` | DeMark describes recycling on a same-direction setup of "equal or greater magnitude"; the condition is qualitative and most TradingView scripts don't recycle |
| `cancel_countdown_on_tdst_break` | `False` | Some readings cancel when price closes back through the spawning setup's TDST; §4 lists TDST as a study, not a gate |
| `cancel_countdown_on_opposite_setup` | `True` | Consistently documented; flagged only to isolate it |
| TDST range | bars 1-9 | Some implementations include the flip bar before setup bar 1 |

The trap worth naming: the bar-13 deferral test compares against the close of
**countdown** bar 8, not setup bar 8. Conflating them yields counts that look
right until they diverge exactly at 13s. `_twelve_countdown_bars()` pins it.

---

## The missing-data rule

The 2026-06-21 audit found a recurring "missing data = confident neutral signal"
bug in this codebase. It is not repeated here, and a large share of the new tests
exist to keep it out:

* Indicators return `None` where not computable — never 0, never neutral.
* Every §5 component awards **zero** for a `None` input and says so in its
  `detail`, so a low score reads as "we don't know yet", not "this looks bad".
* The Regime Composite refuses to report until **all six** inputs exist. A warmup
  region reporting "Neutral" would manufacture a Neutral→Bullish flip — worth 25
  points — on bar 200 of every symbol.
* `decay_factor(None)` is 0.0. Otherwise every symbol whose history contains no
  TD 13 would collect full TD-13 credit.
* An empty bar series is **stale by definition** — no data is worse than old data.
* Missing volume propagates as `None` through RVOL. Substituting 0 would depress
  the trailing average and manufacture a capitulation signal. Real case: §1
  includes commodities.
* The §5 regime gate does **not** apply when the 200 SMA is unknown. An unknown
  trend is not evidence of an adverse one, and the trend component already scored
  0 for the same unknown — one penalty, not two.
* `previous_band()` returns `None` on a first-ever score, so §8.1 cannot read it
  as a transition. This is what stops a DB reset alerting the entire watchlist.

---

## Structural guarantees (not policies someone has to remember)

**§6.4 "Lookups never trigger alerts."** `engine.lookup()` writes only to
`ohlcv_daily` — never `scores_daily` or `signal_events`, the tables §8 reads. So
there is no path from the §6 search bar to a push notification regardless of what
the alerting code later does. Pinned by
`test_lookup_does_not_write_scores_or_signal_events`.

**§7.4 "nothing auto-enters the watchlist."** Every ingestion path writes only a
`status='pending'` row. The sole function that touches the watchlist is
`intake.approve()`, reachable only from an authenticated dashboard POST. An LLM
hallucinating a ticker costs one dismissal, never a position.

**§8.2's daily cap survives failure.** `log_alert()` records *before* delivery is
attempted, so a flapping transport cannot re-alert the same ticker every cycle.

**§9 row-expand shows the persisted score, not a live recompute.** `/detail` and
`/lookup` are separate endpoints for this reason. During the build the expand card
did a live lookup and showed "Entry 45" inside a row reading "Entry 42" — the
session's volume had moved between the two fetches. Two numbers for one name
destroys trust in every other number on the page. Live re-score is now an explicit
button that renders into the lookup card, visibly apart from the row.

---

## Security notes

`/api/signals/intake/email` is the only externally-postable endpoint (§10). It is
exempt from the session gate via `SIGNED_WEBHOOK_PATHS` in `app/services/auth.py`
and verifies a provider HMAC signature instead:

* **Exact-path match, not prefix.** A prefix match would also exempt
  `/intake/pending` and `/intake/{id}/approve`, which mutate the watchlist.
  Pinned by `test_auth_middleware_exempts_only_the_email_path`.
* **Fails closed.** With no `SIGNALS_INTAKE_WEBHOOK_SECRET` configured the
  endpoint refuses email. `SIGNALS_INTAKE_ALLOW_UNSIGNED=true` overrides for local
  dev only and logs a warning every request.
* **Constant-time comparison** via `hmac.compare_digest`.
* **Timestamp included in the signed payload** when the provider sends one —
  signing the body alone permits unlimited replay.
* The signature scheme implemented is HMAC-SHA256 over the raw body (Mailgun-style).
  Postmark and SendGrid differ; only `verify_signature` changes.

---

## Operational notes

* **Timezones.** §3.3/§8.3/§7.6 all specify UK times. All three cron jobs read
  their time from config and are interpreted in `settings.app_timezone`, which is
  **UTC on the EC2 box** — one hour behind UK during BST. Set explicitly in `.env`:
  ```
  SIGNALS_DAILY_RUN_TIME=20:30        # 21:30 UK during BST
  SIGNALS_DIGEST_TIME=20:45
  SIGNALS_INTAKE_DIGEST_TIME=17:00
  ```
* **`/api/signals/run` is synchronous.** A few seconds at 30-50 tickers. Past a few
  hundred names, move to the `run_pipeline_async` pattern in
  `app/services/steno/pipeline.py`.
* **`/api/signals/alerts/evaluate` defaults to `dry_run=true`** so poking the
  endpoint cannot notify Fred as a side effect.
* **DB** at `runtime/signals/signals.db`. `runtime/` is gitignored.
* Running the nightly job twice in a day is safe — every write is idempotent on
  its natural key.

---

## Known gaps

1. **§3.1 reference-chart sign-off** — needs the 10 screenshots. The one item that
   genuinely blocks calling the engine verified.
2. **§7.2 PDF attachments** — "Attached/linked PDFs are pulled in where the mail
   includes them" is not implemented. An attachment-only email returns
   `status: "skipped"` with an explicit reason rather than being silently dropped,
   and §7.5's paste box is the interim path. `app/services/steno/renderer.py` +
   `transcriber.py` already do PDF→text via Claude Vision and can be reused.
3. **§6.1 autocomplete** — needs Twelve Data. Advertised in the API response and
   in the UI rather than silently degraded.
4. **§5 Phase 4 backtest** — optional in the scope doc, not started. No blocker:
   `engine._evaluate()` is a pure function of a `BarSeries`, so replay is
   evaluating it over rolling windows; `scores_daily` is already a time series and
   every weight is a named constant.

   One prerequisite worth knowing: `HISTORY_BARS` is 300 (§3.3's "~300 bars"),
   which is ~14 months. §5 wants "hit-rates per band per asset class", and Prime
   (85+) is rare by design — 14 months across 30-50 names would yield too few
   Prime instances per asset class to tune on. The backtest should fetch several
   years (`SIGNALS_HISTORY_BARS`, and yfinance's period ladder already reaches
   `max`). Verified that Yahoo returns **split-adjusted** closes, so long windows
   do not carry split discontinuities that would fake TD setups; dividends are
   unadjusted (`auto_adjust=False`, matching `app/services/market.py`), which
   leaves ~0.5% quarterly gaps on payers — immaterial to TD's close-vs-close
   comparisons, marginal for z-scores.

   One design decision the scope doc leaves open: the forward-return definition
   (hold period, and whether the exit is the §5 exit score crossing 70 or a fixed
   horizon). That choice determines what "hit-rate" means.
5. ~~§9 mini chart~~ — **done.** `/api/signals/bars/{symbol}` returns aligned
   close/21E/50/200 series and the widget draws all four. Averages are computed
   over full history then sliced, so the 200 SMA is correct at the left edge of
   the window; warmup gaps break the polyline rather than interpolating across
   them.
6. **§3.2 duplicates the existing Steno scraper.** The repo already scrapes Real
   Vision for Steno PDFs on a daily cron (`app/services/steno/`), which §3.2 argues
   against. Nothing was removed. If the scraper is kept for Steno, the email path
   is effectively "RV + anything Fred pastes".
