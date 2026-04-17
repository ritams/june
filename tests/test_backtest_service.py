from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.config import Settings
from app.services.backtest import BacktestService, calculate_forward_returns, signal_transition_dates


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        fred_api_key="test",
        perplexity_api_key=None,
        perplexity_model="sonar-pro",
        telegram_bot_token=None,
        telegram_chat_id=None,
        google_sheet_id=None,
        google_sheet_tab="Macro Dashboard",
        google_service_account_file=None,
        app_timezone="UTC",
        display_timezone="UTC",
        daily_card_time="07:45",
        enable_scheduler=False,
        refresh_interval_minutes=15,
        cache_ttl_seconds=900,
        host="127.0.0.1",
        port=8000,
        global_m2_proxy_series_id=None,
        runtime_dir=tmp_path,
    )


def test_signal_transition_dates_only_emit_first_true() -> None:
    index = pd.date_range("2024-01-31", periods=5, freq="ME")
    active = pd.Series([False, True, True, False, True], index=index)
    transitions = signal_transition_dates(active)
    assert transitions == [index[1], index[4]]


def test_calculate_forward_returns_skips_incomplete_windows() -> None:
    index = pd.date_range("2024-01-01", periods=800, freq="D")
    prices = pd.Series(range(100, 900), index=index, dtype="float64")
    results = calculate_forward_returns(
        [pd.Timestamp("2024-01-15"), pd.Timestamp("2025-10-01")],
        prices,
        [180, 730],
        today=pd.Timestamp("2026-01-01"),
    )
    assert results["180"]["n"] == 1
    assert "730" not in results
    assert results["180"]["win_rate"] == 1.0


def test_dashboard_playbook_formats_btc_warning_and_allocation(tmp_path: Path) -> None:
    service = BacktestService(_settings(tmp_path))
    service.cache_path.write_text(
        json.dumps(
            {
                "last_calculated": "2026-04-13",
                "signals": {
                    "risk_on": {
                        "label": "RISK ON",
                        "subtitle": "Liquidity = EXPANDING and Cycle = EXPANSION",
                        "event_count": 8,
                        "assets": {
                            "SPY": {
                                "label": "S&P",
                                "since": "2000",
                                "confidence": {"tone": "positive", "label": "HIGH", "description": "20+ complete cycles"},
                                "results": {"730": {"avg": 22.4, "win_rate": 0.82, "n": 6, "best": 30.0, "worst": 10.0}},
                            },
                            "QQQ": {
                                "label": "Nasdaq",
                                "since": "2000",
                                "confidence": {"tone": "positive", "label": "HIGH", "description": "20+ complete cycles"},
                                "results": {"730": {"avg": 41.2, "win_rate": 0.86, "n": 6, "best": 60.0, "worst": 11.0}},
                            },
                            "IAU": {
                                "label": "Gold",
                                "since": "2005",
                                "confidence": {"tone": "neutral", "label": "MEDIUM", "description": "Shorter ETF history"},
                                "results": {"730": {"avg": 11.3, "win_rate": 0.7, "n": 6, "best": 20.0, "worst": -5.0}},
                            },
                            "BTC": {
                                "label": "BTC",
                                "since": "2017",
                                "confidence": {"tone": "negative", "label": "LOW", "description": "Direction only, never sizing"},
                                "results": {"730": {"avg": 88.0, "win_rate": 0.75, "n": 4, "best": 200.0, "worst": -40.0}},
                            },
                        },
                    },
                    "m2_acceleration": {
                        "label": "M2 Acceleration",
                        "subtitle": "US M2 MoM > 0.5% for 2 consecutive months",
                        "event_count": 7,
                        "assets": {},
                    },
                    "dollar_weakness": {
                        "label": "Dollar Weakness",
                        "subtitle": "DXY below 100 and 3-month trend negative",
                        "event_count": 9,
                        "assets": {},
                    },
                },
            }
        )
    )

    snapshot = {"signal": {"label": "RISK ON"}}
    playbook = service.dashboard_playbook("liquidity", snapshot)

    assert playbook["allocation"]["deploy_pct"] == 80
    assert "Nasdaq" in playbook["conviction"]
    risk_on_card = playbook["cards"][0]
    assert "Values marked * are based on fewer than 5 complete cases." in risk_on_card["warning"]
    btc_row = next(asset for asset in risk_on_card["assets"] if asset["symbol"] == "BTC")
    assert btc_row["warning"] is not None
    assert btc_row["horizons"]["730"]["display_avg"] == "+88.0%*"
