"""Technical Signals Layer — configuration.

Implements the tunable surface required by `docs/technical-signals-layer-scope.md`
§5: "Weights and thresholds live in a config file."

Everything the scoring model needs to be re-tuned without touching engine code
lives here. The scope doc's Phase 4 (optional backtest replay, §5) exists
precisely so these numbers can be revised against historical hit-rates — so
they are all named constants, never inline literals in the scoring functions.

Layout mirrors `app/services/steno/config.py`: paths rooted at the shared
`runtime/` dir, env-overridable operational settings, and spec constants
transcribed verbatim with a §-citation next to each.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import ROOT_DIR


def _as_bool_env(name: str, default: bool) -> bool:
    """Env var → bool, matching the convention in app/config.py's _as_bool."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ── Paths ────────────────────────────────────────────────────────────────────

SIGNALS_ROOT = ROOT_DIR / "runtime" / "signals"

# Single SQLite file for every relational table in scope doc §10:
#   watchlist, sources, intake_events, ohlcv_daily, scores_daily,
#   signal_events, alerts_log
#
# The scope doc specifies Postgres/Supabase (§10). We use SQLite instead: the
# rest of this repo has no database at all (JSON files via
# app/services/steno/store.py), a hosted Postgres would be a brand-new
# deployment dependency on the EC2 box, and at the scope doc's stated watchlist
# size (§1: "~30-50 tickers") a single-writer SQLite file is comfortably
# sufficient. The schema is written to be portable, so a later migration to
# Postgres is a connection-string and dialect change rather than a redesign.
SIGNALS_DB_PATH = SIGNALS_ROOT / "signals.db"

# §7.2: PDFs arriving as email attachments or via the §7.5 upload box are stored
# here before extraction, so a failed parse can be inspected rather than lost.
SIGNALS_INTAKE_DIR = SIGNALS_ROOT / "intake"

SIGNALS_ROOT.mkdir(parents=True, exist_ok=True)
SIGNALS_INTAKE_DIR.mkdir(parents=True, exist_ok=True)

# §7.2 PDF guards. An inbound webhook accepts data from outside, so the size cap
# is a denial-of-service guard as much as a sanity check: rendering a
# 500-page PDF to 200-DPI PNGs would exhaust memory on the EC2 box.
INTAKE_MAX_PDF_BYTES = int(os.getenv("SIGNALS_INTAKE_MAX_PDF_BYTES", str(25 * 1024 * 1024)))

# Below this many extracted characters we treat the PDF as having no usable text
# layer (scanned or chart-only) and fall back to Claude Vision. A research note
# with real prose clears this comfortably; a cover page of images will not.
INTAKE_PDF_TEXT_MIN_CHARS = int(os.getenv("SIGNALS_INTAKE_PDF_TEXT_MIN_CHARS", "400"))


# ── Price data (scope doc §3.3) ──────────────────────────────────────────────

# §3.3 specifies Twelve Data (or Polygon + a crypto source). We ship yfinance
# first: it is already a dependency of this repo (app/services/market.py) and
# needs no API key, so the engine can be built and verified immediately.
#
# TODO(twelve-data): add TwelveDataProvider implementing the same
# OHLCVProvider protocol in app/services/signals/providers.py, then flip
# OHLCV_PROVIDER to "twelvedata". Two things genuinely improve on the switch:
#   1. §6.1 autocomplete — Twelve Data has a reference/symbol-search endpoint;
#      yfinance has no supported equivalent, so lookup currently validates a
#      typed symbol rather than offering true autocomplete.
#   2. Crypto and equities arrive from one authenticated source with an SLA,
#      which is what makes the §3.3 "stale" contract meaningful.
# The provider protocol is the whole point of the indirection — no engine,
# scoring, or API code should need to change.
OHLCV_PROVIDER = os.getenv("SIGNALS_OHLCV_PROVIDER", "yfinance").strip().lower()

# The TwelveDataProvider is implemented and registered; it needs only this key.
# To switch: set TWELVE_DATA_API_KEY and SIGNALS_OHLCV_PROVIDER=twelvedata.
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip() or None

# Search can use a *different* provider from bars, and by default it does.
#
# Measured against the live APIs, the two have opposite strengths:
#   * Twelve Data has a real symbol-search endpoint (§6.1 autocomplete) — the one
#     thing yfinance cannot do at all — but its basic plan allows 8 requests per
#     minute, which turns a 50-name nightly run into a ~6-minute throttled crawl,
#     and its intraday volume lags badly (2% of median mid-session vs yfinance's
#     44%), under-scoring §5's volume component on daytime lookups.
#   * yfinance has no search, but no rate limit and better intraday volume.
#
# So the default is the hybrid: yfinance for bars, Twelve Data for search when a
# key exists. One search call per keystroke-batch is trivially within 8/min,
# while the nightly run stays unthrottled. Set SIGNALS_SEARCH_PROVIDER explicitly
# to override, or point both at one provider.
SEARCH_PROVIDER = (
    os.getenv("SIGNALS_SEARCH_PROVIDER", "").strip().lower()
    or ("twelvedata" if TWELVE_DATA_API_KEY else OHLCV_PROVIDER)
)

# Twelve Data basic plan: 8 requests/minute, 800/day (confirmed live via
# /api_usage). Raise if the plan is upgraded.
TWELVE_DATA_RPM = int(os.getenv("TWELVE_DATA_RPM", "8"))

# §3.3: "~300 bars history per ticker". 300 daily bars is the minimum that
# supports a 200-day SMA plus enough lookback for its slope and for a 50-day
# z-score window to be well-defined from the first scored bar.
HISTORY_BARS = int(os.getenv("SIGNALS_HISTORY_BARS", "300"))

# §3.3: "fetched nightly ~21:30 UK". Local time on the host; the EC2 box runs UTC,
# which is UK time minus one hour in BST — set SIGNALS_DAILY_RUN_TIME explicitly
# in .env for the deployment rather than relying on host locale.
DAILY_RUN_TIME = os.getenv("SIGNALS_DAILY_RUN_TIME", "21:30").strip()

# §6.2: on-demand lookups fetch live and must return in under 2s. Cache fetched
# bars briefly so a burst of lookups on the same symbol hits the DB, not the
# network. Distinct from the persisted ohlcv_daily table, which is the nightly
# run's record.
LOOKUP_CACHE_TTL_SECONDS = int(os.getenv("SIGNALS_LOOKUP_CACHE_TTL", "900"))

# §3.3: "If the data API is down, the run flags 'stale' on the dashboard and
# skips alerts — never scores silently on old data." A symbol's bars are stale
# once the most recent bar is older than this many calendar days. Three days
# covers a normal Fri→Mon weekend gap plus one public holiday without false
# positives; a fourth day of silence is a real problem worth surfacing.
STALE_AFTER_DAYS = int(os.getenv("SIGNALS_STALE_AFTER_DAYS", "3"))


# ── Indicator parameters (scope doc §4) ──────────────────────────────────────
# "Nothing else in v1." — §4. Adding an indicator means editing the scope doc
# first, then this block.

EMA_FAST = 21          # §4: "21 EMA"    — pullback zone and stretch anchor
SMA_MID = 50           # §4: "50 SMA"    — trend context, z-score window
SMA_SLOW = 200         # §4: "200 SMA"   — regime gate (§5)
RSI_PERIOD = 14        # §4: "RSI(14), daily + weekly confluence"
ATR_PERIOD = 14        # §4: "ATR-normalised stretch"; 14 is Wilder's standard
ROC_PERIOD = 20        # §4 Regime Composite: "20-day ROC"
RVOL_LOOKBACK = 20     # §4: "Relative volume (RVOL)" vs its own 20-day average
ZSCORE_WINDOW = 50     # §4: "z-score vs 50D"
ATR_TREND_WINDOW = 20  # §4 Regime Composite: "ATR trend" — ATR now vs 20 bars ago
SMA_SLOPE_WINDOW = 20  # §4 Regime Composite: "50>200 slope" measured over 20 bars

# §4: weekly bars are aggregated locally from daily (§3.3), so weekly indicators
# need enough weekly bars for the same windows. 300 daily bars ≈ 60 weekly bars,
# which supports RSI(14) and the TD counts but NOT a weekly 200-period SMA —
# the weekly timeframe is used only for TD confluence and RSI per §4/§5.
MIN_WEEKLY_BARS = 30


# ── Entry score weights (scope doc §5, "Entry Score (0-100)") ────────────────
# Transcribed verbatim from the §5 component table. Max values sum to 100:
#   25 + 25 + 20 + 15 + 10 + 5 = 100
ENTRY_MAX = {
    "td_sequential":  25,   # §5: "TD Sequential | 25"
    "regime":         25,   # §5: "Regime Composite | 25"
    "stretch":        20,   # §5: "Mean reversion stretch | 20"
    "trend":          15,   # §5: "Trend structure | 15"
    "rsi":            10,   # §5: "RSI | 10"
    "volume":          5,   # §5: "Volume | 5"
}

# §5 TD Sequential row: "Daily buy setup 7-8: 10 · Daily 9 complete: 18 ·
# Daily 13 countdown: 25 · Weekly 9/13 confluence: +5 (capped 25)"
TD_SETUP_7_8_POINTS = 10
TD_SETUP_9_POINTS = 18
TD_COUNTDOWN_13_POINTS = 25
TD_WEEKLY_CONFLUENCE_BONUS = 5

# §5 Regime Composite row: "Bearish→Neutral flip: 12 · Neutral→Bullish flip: 25 ·
# Established Bullish, pullback context: 15"
REGIME_FLIP_BEAR_TO_NEUTRAL_POINTS = 12
REGIME_FLIP_NEUTRAL_TO_BULL_POINTS = 25
REGIME_ESTABLISHED_BULL_PULLBACK_POINTS = 15

# §5 Mean reversion row: "Z <= -2 vs 50D or >= 2.5 ATR below 21 EMA: 20 ·
# Z <= -1.5: 12 · Z <= -1: 6"
STRETCH_Z_EXTREME = -2.0
STRETCH_ATR_EXTREME = 2.5      # ATRs below the 21 EMA; equivalent trigger to Z <= -2
STRETCH_EXTREME_POINTS = 20
STRETCH_Z_STRONG = -1.5
STRETCH_STRONG_POINTS = 12
STRETCH_Z_MILD = -1.0
STRETCH_MILD_POINTS = 6

# §5 Trend structure row: "Above rising 200D: 8 · 50D > 200D: +4 ·
# Pullback into 21 EMA/50D zone: +3"  (8 + 4 + 3 = 15 = ENTRY_MAX["trend"])
TREND_ABOVE_RISING_200_POINTS = 8
TREND_GOLDEN_CROSS_POINTS = 4
TREND_PULLBACK_ZONE_POINTS = 3

# §5 Trend structure and Regime rows both reference a "pullback" context
# ("Pullback into 21 EMA/50D zone", "Established Bullish, pullback context").
# The scope doc names the zone but does not define its edges, so we define it
# here: price has retraced into the band bounded by the 21 EMA and the 50 SMA,
# with a tolerance either side so a near-touch still counts.
#
# **[INTERPRETATION]** — this is our reading, not a quoted spec value. It is a
# tuning candidate for the §5 Phase 4 backtest. 2% is wide enough to catch a
# pullback that stalls just above the 21 EMA without admitting price that is
# still extended well above it.
PULLBACK_ZONE_TOLERANCE = 0.02

# §5 RSI row: "Daily < 30 turning up: 10 · Daily < 35 with weekly > 40: 7 ·
# Bullish divergence: 8"
RSI_OVERSOLD = 30.0
RSI_OVERSOLD_TURNING_POINTS = 10
RSI_MILD_OVERSOLD = 35.0
RSI_WEEKLY_SUPPORT = 40.0
RSI_MILD_WITH_WEEKLY_POINTS = 7
RSI_BULLISH_DIVERGENCE_POINTS = 8

# §5 Volume row: "RVOL >= 2 on signal day: 5 · RVOL >= 1.5: 3"
RVOL_HIGH = 2.0
RVOL_HIGH_POINTS = 5
RVOL_ELEVATED = 1.5
RVOL_ELEVATED_POINTS = 3


# ── Exit score weights (scope doc §5, "Exit Score (0-100), mirrored") ────────
# §5: "TD sell 9/13 (25) · Regime deterioration (25) · stretch >= +2 z / +2.5 ATR
# above 21 EMA (20) · loss of 21 EMA / 50D on close (15) · daily RSI > 75 or
# weekly bearish divergence (10) · distribution volume (5)."
EXIT_MAX = {
    "td_sequential":  25,
    "regime":         25,
    "stretch":        20,
    "structure":      15,   # loss of 21 EMA / 50D on close
    "rsi":            10,
    "volume":          5,
}

EXIT_STRETCH_Z_EXTREME = 2.0        # mirror of STRETCH_Z_EXTREME
EXIT_STRETCH_ATR_EXTREME = 2.5      # ATRs *above* the 21 EMA
EXIT_RSI_OVERBOUGHT = 75.0          # §5: "daily RSI > 75"


# ── Regime gate (scope doc §5) ───────────────────────────────────────────────
# §5: "Regime gate: below a falling 200D, entry score capped at 60, badged
# 'counter-trend'." Also §4: "200-day regime gate (weekly confirmed) | Not
# scored — caps counter-trend entry scores at 60".
COUNTER_TREND_CAP = 60
COUNTER_TREND_BADGE = "counter-trend"


# ── Event freshness (scope doc §5) ───────────────────────────────────────────
# §5: "TD completions and regime flips score fully for 3 trading days, then
# decay linearly to zero over 10."
#
# Read as: bars 0-3 since the event score at full weight; from bar 3 the weight
# decays linearly, reaching zero 10 trading days after the full-credit window
# ends (i.e. at bar 13). See decay_factor() in scoring.py for the exact curve
# and its boundary tests.
FRESHNESS_FULL_DAYS = 3
FRESHNESS_DECAY_DAYS = 10


# ── Score bands (scope doc §5) ───────────────────────────────────────────────
# §5: "Bands: 85-100 Prime · 70-84 Strong · 50-69 Watch · <50 no signal."
# Tuple: (lower_inclusive, upper_inclusive, band_name).
BANDS: list[tuple[int, int, str]] = [
    (85, 100, "Prime"),
    (70,  84, "Strong"),
    (50,  69, "Watch"),
    (0,   49, "No signal"),
]

# Ordered weakest→strongest. Used by the alerting layer (§8.1: alerts fire only
# on band transitions) to decide whether a move is an upgrade or a downgrade.
BAND_ORDER = ["No signal", "Watch", "Strong", "Prime"]


def band_for_score(score: int) -> str:
    """Map a 0-100 score to its band name per §5.

    Scores are clamped to [0, 100] by the scoring functions before this is
    called, so the fallback is unreachable in practice.
    """
    for lo, hi, name in BANDS:
        if lo <= score <= hi:
            return name
    return "No signal"


# ── Alerting (scope doc §8) ──────────────────────────────────────────────────

# §8.3: "One daily digest ~21:45 UK". Runs 15 minutes after the scoring job
# (DAILY_RUN_TIME) so it always reports on fresh scores. Same timezone caveat as
# DAILY_RUN_TIME — set explicitly in .env for the deployment.
DIGEST_TIME = os.getenv("SIGNALS_DIGEST_TIME", "21:45").strip()

# §7.6: "Sunday 18:00 UK digest line via Hermes" for the intake queue.
# APScheduler day-of-week: 'sun'.
INTAKE_DIGEST_TIME = os.getenv("SIGNALS_INTAKE_DIGEST_TIME", "18:00").strip()
INTAKE_DIGEST_DAY = os.getenv("SIGNALS_INTAKE_DIGEST_DAY", "sun").strip().lower()

# §8.3: "Real-time pushes reserved for Prime (>=85) transitions."
REALTIME_BAND = "Prime"
REALTIME_SCORE_THRESHOLD = 85

# §8.1: "Exit crossing 70" is the exit-side alert trigger.
EXIT_ALERT_THRESHOLD = 70

# §8.2: "Max one alert per ticker per day, hard cap."
MAX_ALERTS_PER_TICKER_PER_DAY = 1

# §8.4 TD completion override: "TD9/TD13 completions, daily and weekly, both
# sides, always notify regardless of band. Weekly completions and any TD13 →
# real-time push; daily TD9s → digest 'TD counts' line."
# The scope doc names these config keys explicitly: `td_override.enabled`,
# `td_override.realtime_scope`, per-ticker opt-out (the last is a column on
# `watchlist`, see db.py).
TD_OVERRIDE_ENABLED = _as_bool_env("SIGNALS_TD_OVERRIDE_ENABLED", True)

# §3.1 sign-off state: have our native counts been reconciled against Fred's paid
# TradingView indicator on 10 reference charts?
#
# False until that test runs. See docs/td-sequential-verification.md. This is a
# single switch rather than a comment because it changes behaviour: while counts
# are unverified, TD completions must not bypass the band gate with a real-time
# push (§8.4), and the widget badges them as unverified.
TD_COUNTS_VERIFIED = _as_bool_env("SIGNALS_TD_COUNTS_VERIFIED", False)

# Which TD completions escalate to a real-time push rather than the digest.
# 'weekly_and_13' implements §8.4 as written. 'all' pushes every completion;
# 'none' routes everything to the digest.
#
# Default is derived from TD_COUNTS_VERIFIED: §8.4's real-time push deliberately
# bypasses the band gate, so an unverified count that fires one is the single
# most expensive way for this engine to be wrong — it buzzes Fred's phone, and
# §1's premise is that a noisy alerter gets muted and then delivers nothing.
# Until sign-off, completions still fire, still score, and still appear in the
# 21:45 digest's "TD counts" line; they just don't push. An explicit env value
# always wins, so this can be overridden either way.
TD_OVERRIDE_REALTIME_SCOPE = (
    os.getenv("SIGNALS_TD_OVERRIDE_REALTIME_SCOPE", "").strip().lower()
    or ("weekly_and_13" if TD_COUNTS_VERIFIED else "none")
)

# §8.6: "Payload to Hermes ... outbound POST, retried, delivery logged. Hermes
# owns transport."
#
# No Hermes inbound webhook exists in this deployment yet — the Hermes agent
# connects *to* this service over MCP (see app/mcp_server.py) rather than
# exposing an endpoint of its own. So:
#   * If SIGNALS_HERMES_WEBHOOK_URL is set, we POST the §8.6 payload to it.
#   * Otherwise we fall back to the Telegram client already configured in this
#     repo (app/services/telegram.py), which is what actually reaches Fred today.
# Either way the delivery is logged to `alerts_log`, so the §8.6 contract holds
# regardless of which sink is active.
HERMES_WEBHOOK_URL = os.getenv("SIGNALS_HERMES_WEBHOOK_URL", "").strip() or None

# §8.6: "retried". Total attempts per alert, with a linear backoff between them.
ALERT_MAX_ATTEMPTS = int(os.getenv("SIGNALS_ALERT_MAX_ATTEMPTS", "3"))
ALERT_RETRY_BACKOFF_SECONDS = float(os.getenv("SIGNALS_ALERT_RETRY_BACKOFF", "2.0"))

# §8.3: "top 5 entry scores".
DIGEST_TOP_N = int(os.getenv("SIGNALS_DIGEST_TOP_N", "5"))

# §7: "if no email arrives from a source for 21 days, the dashboard shows a
# quiet 'no Steno note received in 3 weeks' notice".
SOURCE_SILENCE_DAYS = int(os.getenv("SIGNALS_SOURCE_SILENCE_DAYS", "21"))


# ── Watchlist provenance (scope doc §7) ──────────────────────────────────────
# §7: "every ticker carries a source tag (STENO / RV / COWAN / OWN) and date
# added, filterable."
SOURCE_TAGS = ("STENO", "RV", "COWAN", "OWN")
DEFAULT_SOURCE_TAG = "OWN"

# §6.4 / §3.1: each ticker gets an "Open in TradingView" link, because Chameleon
# cannot be replicated (§3.1) and remains Fred's manual confirmation step.
TRADINGVIEW_URL_TEMPLATE = "https://www.tradingview.com/chart/?symbol={symbol}"
