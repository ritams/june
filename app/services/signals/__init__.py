"""Technical Signals Layer.

Implements `docs/technical-signals-layer-scope.md` — a low-noise technical
scoring engine embedded in the existing dashboard. Scores watchlist names 0-100
for entry quality and exit risk on a daily-close basis, and supports on-demand
lookup of any ticker (§6).

Module map (§ references are to the scope doc):

    config.py      tunable weights, thresholds, paths          §3.3, §4, §5, §7
    db.py          SQLite schema and connection policy         §10
    providers.py   OHLCV fetch behind a swappable protocol     §3.3
    bars.py        bar containers, weekly aggregation, staleness §3.3
    indicators.py  RSI/ATR/EMA/SMA/z-score/RVOL/Regime          §4
    td_sequential.py  native DeMark TD Sequential               §3.1, §4
    scoring.py     entry + exit 0-100, freshness, gate, bands   §5
    store.py       watchlist and score persistence              §10
    engine.py      orchestration: fetch → compute → score       §10

Explicitly NOT here, per §2 "out of scope": scraping, intraday signals,
auto-trading, options data, portfolio tracking, fundamental data, TradingView
integration. The engine does not pick stocks (§1) — it times entries and exits
on names selected fundamentally elsewhere.
"""
