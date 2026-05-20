"""Regression tests for the Steno store + bucket-mirror engine.

Covers the gotchas we fixed:
  - Store deduplicates by (report_date, source_pdf) on re-commit, so re-ingesting
    a PDF replaces the old entry rather than appending.
  - The model-portfolio validator distinguishes full models (≥5 positions) from
    tactical updates / commentary.
  - The bucket-aggregating mirror sums Dan's holdings into Steno's buckets via
    the equivalence map and produces sensible Buy/Add/Hold/Trim signals.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import mirror as mirror_mod
from app.services.steno import store as steno_store


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the store at a tmp file so we don't clobber the dev runtime."""
    p = tmp_path / "model_portfolio.json"
    monkeypatch.setattr(steno_store, "STENO_PORTFOLIO_PATH", p)
    return p


def _make_portfolio(date: str, positions: list[dict]) -> dict:
    return {
        "report_date": date,
        "risk_tone": "selective",
        "summary": "test",
        "positions": positions,
        "cash_weight_pct": 30.0,
        "macro_notes": [],
    }


def test_commit_dedupes_same_pdf(isolated_store):
    """Re-committing the same source_pdf overwrites the prior entry rather than
    appending — this was the bug where multiple ingests left stale duplicates."""
    p1 = _make_portfolio("2026-03-04", [{"name": f"P{i}", "asset_class": "equity",
        "direction": "long", "target_weight_pct": 10, "commentary": "x"} for i in range(8)])
    steno_store.commit_portfolio(p1, source_pdf="mar-04.pdf")
    # Re-ingest with a different (corrected) extraction
    p2 = _make_portfolio("2026-03-04", [{"name": f"Q{i}", "asset_class": "equity",
        "direction": "long", "target_weight_pct": 10, "commentary": "y"} for i in range(8)])
    steno_store.commit_portfolio(p2, source_pdf="mar-04.pdf")
    data = steno_store.load_store()
    mar_04_entries = [h for h in data["history"] if h.get("source_pdf") == "mar-04.pdf"]
    assert len(mar_04_entries) == 1, "re-commit should replace, not append"
    assert mar_04_entries[0]["positions"][0]["name"] == "Q0"


def test_full_model_threshold(isolated_store):
    """A 4-position tactical report is recorded but NOT promoted to latest when
    a prior 8-position full model exists for an earlier date."""
    full = _make_portfolio("2026-03-04", [{"name": f"P{i}", "asset_class": "equity",
        "direction": "long", "target_weight_pct": 10, "commentary": "x"} for i in range(8)])
    steno_store.commit_portfolio(full, source_pdf="mar-04.pdf")
    tactical = _make_portfolio("2026-03-09", [{"name": "Oil Futures", "asset_class": "commodity",
        "direction": "long", "target_weight_pct": 25, "commentary": "vol hedge"},
        {"name": "USD Index", "asset_class": "currency",
        "direction": "long", "target_weight_pct": 25, "commentary": "shelter"}])
    steno_store.commit_portfolio(tactical, source_pdf="mar-09.pdf")
    latest = steno_store.get_latest()
    assert latest["report_date"] == "2026-03-04", "tactical should not displace the full model"
    updates = steno_store.recent_updates()
    assert any(u["report_date"] == "2026-03-09" for u in updates), "tactical should appear in updates"


def test_implausible_weights_rejected(isolated_store):
    """A '100% one position' extraction (Claude hallucination from a commentary
    piece) is recorded but not promoted."""
    full = _make_portfolio("2026-03-04", [{"name": f"P{i}", "asset_class": "equity",
        "direction": "long", "target_weight_pct": 10, "commentary": "x"} for i in range(8)])
    steno_store.commit_portfolio(full, source_pdf="mar-04.pdf")
    bogus = _make_portfolio("2026-05-18", [{"name": "Energy Stocks",
        "asset_class": "equity", "direction": "long", "target_weight_pct": 100,
        "commentary": "everything"}])
    steno_store.commit_portfolio(bogus, source_pdf="may-18.pdf")
    assert steno_store.get_latest()["report_date"] == "2026-03-04"


def test_bucket_mirror_aggregates_via_equivalence(isolated_store):
    """Dan holds IAU (gold ETF); Steno wants GLD. Equivalence map should fold
    IAU into the gold bucket and report a Hold."""
    portfolio = _make_portfolio("2026-03-04", [
        {"name": "Gold", "asset_class": "commodity", "direction": "long",
         "target_weight_pct": 5.0, "ticker": "GLD", "commentary": "ballast"},
        # 7 more so it counts as a full model
        *[{"name": f"P{i}", "asset_class": "equity", "direction": "long",
           "target_weight_pct": 10.0, "ticker": f"T{i}", "commentary": "x"} for i in range(7)],
    ])
    steno_store.commit_portfolio(portfolio, source_pdf="mar-04.pdf")
    ibkr = {
        "nav": 100_000,
        "base_currency": "USD",
        "positions": [
            {"symbol": "IAU", "description": "iShares Gold Trust", "market_value": 5_000,
             "position": 100, "asset_category": "equity"},
        ],
    }
    payload = mirror_mod.build_mirror(ibkr_snapshot=ibkr)
    gold = next(b for b in payload["buckets"] if b["name"] == "Gold")
    assert gold["dan_weight_pct"] == 5.0
    assert gold["action"] == "Hold", "5% IAU vs 5% GLD target should be Hold (equivalence map)"
    assert any(m["symbol"] == "IAU" for m in gold["members"])
    assert any(m["source"] == "equivalence" for m in gold["members"])


def test_theme_universe_unions_across_reports(isolated_store):
    """The rolling universe should union themes across the last N reports,
    with most-recent valid weight winning per theme."""
    # Core full model (Mar 4) — 5 positions: gold, BTC, ...
    core = _make_portfolio("2026-03-04", [
        {"name": "Gold", "asset_class": "commodity", "direction": "long",
         "target_weight_pct": 8.0, "ticker": "GLD", "commentary": "core"},
        {"name": "Bitcoin", "asset_class": "crypto", "direction": "long",
         "target_weight_pct": 10.0, "ticker": "BTC", "commentary": "core"},
        {"name": "Drone Defence", "asset_class": "equity", "direction": "long",
         "target_weight_pct": 10.0, "ticker": "JEDI", "commentary": "core"},
        {"name": "Silver Futures", "asset_class": "commodity", "direction": "short",
         "target_weight_pct": 5.0, "ticker": "SI", "commentary": "core"},
        {"name": "Kospi", "asset_class": "equity", "direction": "short",
         "target_weight_pct": 5.0, "ticker": "EWY", "commentary": "core"},
    ])
    steno_store.commit_portfolio(core, source_pdf="mar-04.pdf")
    # Tactical Mar 9 — adds Oil Futures, USD Index as new themes
    tactical = _make_portfolio("2026-03-09", [
        {"name": "Oil Futures", "asset_class": "commodity", "direction": "long",
         "target_weight_pct": 25.0, "ticker": "USO", "commentary": "vol hedge"},
        {"name": "USD Index", "asset_class": "currency", "direction": "long",
         "target_weight_pct": 25.0, "ticker": "UUP", "commentary": "shelter"},
    ])
    steno_store.commit_portfolio(tactical, source_pdf="mar-09.pdf")
    # Updated mention of Bitcoin at different weight — most-recent wins
    refresh = _make_portfolio("2026-04-01", [
        {"name": "Bitcoin", "asset_class": "crypto", "direction": "long",
         "target_weight_pct": 7.5, "ticker": "BTC", "commentary": "trim per vol"},
    ])
    steno_store.commit_portfolio(refresh, source_pdf="apr-01.pdf")
    # Bogus report with 0-weight + 100-weight positions — both should be filtered out
    bogus = _make_portfolio("2026-04-15", [
        {"name": "USD/JPY Long", "asset_class": "currency", "direction": "long",
         "target_weight_pct": 0.0, "ticker": "YCS", "commentary": "pair"},
        {"name": "Hallucinated Energy", "asset_class": "equity", "direction": "long",
         "target_weight_pct": 100.0, "ticker": "XLE", "commentary": "bad"},
    ])
    steno_store.commit_portfolio(bogus, source_pdf="apr-15.pdf")

    u = steno_store.build_theme_universe(lookback_reports=6)
    names = {t["name"] for t in u["themes"]}
    assert "Gold" in names and "Drone Defence" in names           # core preserved
    assert "Oil Futures" in names and "USD Index" in names        # tactical added
    assert "USD/JPY Long" not in names                            # 0% filtered out (rule a)
    assert "Hallucinated Energy" not in names                     # 100% filtered out (rule c)

    btc = next(t for t in u["themes"] if t["name"] == "Bitcoin")
    assert btc["target_weight_pct"] == 7.5                        # most-recent valid wins (rule c)
    assert btc["is_core"] is True                                 # was in the core model
    assert btc["source_report_date"] == "2026-04-01"              # weight came from latest mention

    oil = next(t for t in u["themes"] if t["name"] == "Oil Futures")
    assert oil["is_tactical"] is True
    assert oil["is_core"] is False
    assert oil["source_report_date"] == "2026-03-09"


def test_mirror_uses_theme_universe(isolated_store):
    """build_mirror() default path should aggregate via the universe — Dan's USO
    holding should count toward an Oil Futures bucket added by a tactical Mar 9
    report, NOT show up as off-thesis."""
    core = _make_portfolio("2026-03-04", [
        {"name": f"Theme{i}", "asset_class": "equity", "direction": "long",
         "target_weight_pct": 10.0, "ticker": f"T{i}", "commentary": "x"} for i in range(5)
    ])
    steno_store.commit_portfolio(core, source_pdf="mar-04.pdf")
    tactical = _make_portfolio("2026-03-09", [
        {"name": "Oil Futures", "asset_class": "commodity", "direction": "long",
         "target_weight_pct": 25.0, "ticker": "USO", "commentary": "vol hedge"},
    ])
    steno_store.commit_portfolio(tactical, source_pdf="mar-09.pdf")
    ibkr = {"nav": 100_000, "base_currency": "USD", "positions": [
        {"symbol": "USO", "market_value": 25_000, "position": 1000, "asset_category": "etf"},
    ]}
    payload = mirror_mod.build_mirror(ibkr_snapshot=ibkr)
    oil = next((b for b in payload["buckets"] if b["name"] == "Oil Futures"), None)
    assert oil is not None, "Oil Futures from Mar 9 update should appear as a bucket in the universe"
    assert oil["is_tactical"] is True
    assert oil["dan_weight_pct"] == 25.0
    assert oil["action"] == "Hold"
    assert payload["universe_meta"] is not None


def test_dxy_uup_equivalence():
    """The Mar 9-style 'USD Index → DXY' case: Dan holding UUP should count
    toward a DXY bucket via the equivalence map."""
    from app.services.equivalence import equivalents
    assert "DXY" in equivalents("UUP")
    assert "UUP" in equivalents("DXY")
