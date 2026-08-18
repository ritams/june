# TD Sequential — verification status

**Status: UNVERIFIED.** Shipping with real-time TD pushes disabled.

## What §3.1 requires

> native counts must match the paid TradingView indicator on 10 reference charts
> (Fred supplies screenshots) before Phase 1 signs off.

Exact match against the *paid* indicator. That test has not run — it needs the
screenshots.

## What we did

| Change | Where |
| --- | --- |
| `SIGNALS_TD_COUNTS_VERIFIED=false` (default) | `signals/config.py` |
| Real-time TD pushes off — completions route to the 21:45 digest instead | derived default for `TD_OVERRIDE_REALTIME_SCOPE` |
| TD badges render `TD9?` / `TD13?`; TD component shows "confirm on the chart" | `static/signals.js` |
| `td_counts_verified` exposed on `/api/signals/status` | `signals/api.py` |

TD completions still fire, still score, still appear in the digest's "TD counts"
line. They just don't buzz Fred's phone.

## Why

§8.4's real-time push **deliberately bypasses the band gate** — that is the one
place an unverified count reaches Fred with no other check in front of it. §1's
premise is that a noisy alerter gets muted and then delivers nothing, so a false
TD13 push is the most expensive way this engine can be wrong.

Everything else degrades gracefully: TD is 25 of 100 in §5, Prime transitions are
driven by all six components, and §3.1 already puts Fred on the TradingView chart
to confirm before executing. The push was the only unguarded path.

## Is exact match achievable?

**Rules: yes.** All DeMark ambiguities are isolated behind four `TDConfig` flags
(≤16 variants). The reference charts identify which variant Fred's script uses.

**Data: probably, except crypto.** Identical rules on different bars give
different counts. Yahoo's `BTC-USD` is a composite; TradingView's is a specific
exchange with different closes and different daily boundaries. Weekly bar
alignment and dividend adjustment can also differ. Verified that history depth is
*not* a factor (300 vs 500–730 bars give identical counts) and that Yahoo closes
are split-adjusted.

**Diagnostic when the screenshots land:** systematic mismatch → a rule difference,
flip a flag. Sporadic mismatch on near-tied bars → a feed difference, not fixable.

## To sign off

1. Get 10 charts from **one named** paid script (its name, author, and settings
   panel — "the paid indicator" is not a single artifact; different paid TD
   scripts disagree with each other on exactly these ambiguous rules).
2. Reconcile; flip whichever `TDConfig` flags the charts indicate.
3. Set `SIGNALS_TD_COUNTS_VERIFIED=true`. Pushes and clean badges return
   automatically — no code change.

Recommend narrowing the criterion to **completions (9s/13s) and their dates on
daily US equities/ETFs**, with crypto divergences documented rather than blocking.
As written, the test may be unpassable on crypto for reasons no code can fix.
