"""Tests for bar containers, weekly aggregation and staleness (scope doc §3.3)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.signals.bars import Bar, BarSeries, to_weekly


def _bar(d: date, close: float, *, high=None, low=None, volume=1000.0) -> Bar:
    return Bar(
        bar_date=d,
        open=close,
        high=high if high is not None else close + 1,
        low=low if low is not None else close - 1,
        close=close,
        volume=volume,
    )


# ── Ordering contract ────────────────────────────────────────────────────────


def test_series_sorts_bars_oldest_to_newest_on_construction():
    """Every downstream module relies on bars[-1] being newest and bars[i-4]
    being four bars earlier. A mis-ordered series would yield plausible but
    wrong DeMark counts — the exact failure §3.1's acceptance test targets."""
    out_of_order = [
        _bar(date(2026, 1, 3), 103.0),
        _bar(date(2026, 1, 1), 101.0),
        _bar(date(2026, 1, 2), 102.0),
    ]
    series = BarSeries("TEST", "daily", out_of_order)
    assert [b.close for b in series.bars] == [101.0, 102.0, 103.0]
    assert series.last.close == 103.0


def test_accessors_return_parallel_lists():
    series = BarSeries("TEST", "daily", [_bar(date(2026, 1, i + 1), 100.0 + i) for i in range(3)])
    assert series.closes == [100.0, 101.0, 102.0]
    assert len(series.highs) == len(series.lows) == len(series.volumes) == 3


def test_last_on_empty_series_is_none():
    assert BarSeries("TEST", "daily", []).last is None


def test_tail_preserves_order_and_length():
    series = BarSeries("TEST", "daily", [_bar(date(2026, 1, i + 1), 100.0 + i) for i in range(10)])
    tail = series.tail(3)
    assert len(tail) == 3
    assert tail.closes == [107.0, 108.0, 109.0]


def test_tail_of_zero_is_empty():
    series = BarSeries("TEST", "daily", [_bar(date(2026, 1, 1), 100.0)])
    assert len(series.tail(0)) == 0


def test_bar_is_frozen():
    """Indicators must never mutate their input."""
    bar = _bar(date(2026, 1, 1), 100.0)
    with pytest.raises(Exception):
        bar.close = 200.0    # type: ignore[misc]


# ── Staleness (§3.3) ─────────────────────────────────────────────────────────


def test_recent_data_is_not_stale():
    today = datetime.now(timezone.utc).date()
    series = BarSeries("TEST", "daily", [_bar(today - timedelta(days=1), 100.0)])
    assert series.is_stale() is False


def test_old_data_is_stale():
    today = datetime.now(timezone.utc).date()
    series = BarSeries("TEST", "daily", [_bar(today - timedelta(days=10), 100.0)])
    assert series.is_stale() is True


def test_weekend_gap_is_not_stale_with_the_default_window():
    """The 3-day default absorbs Fri→Mon plus a holiday without false positives."""
    monday = date(2026, 7, 27)
    friday = monday - timedelta(days=3)
    series = BarSeries("TEST", "daily", [_bar(friday, 100.0)])
    assert series.is_stale(as_of=monday) is False


def test_empty_series_is_stale_by_definition():
    """No data is strictly worse than old data.

    Returning False here would let the engine score a symbol it knows nothing
    about — the "missing data = confident signal" bug the 2026-06-21 audit found.
    """
    assert BarSeries("TEST", "daily", []).is_stale() is True


def test_stale_window_is_configurable_per_call():
    today = date(2026, 7, 27)
    series = BarSeries("TEST", "daily", [_bar(today - timedelta(days=5), 100.0)])
    assert series.is_stale(as_of=today, max_age_days=3) is True
    assert series.is_stale(as_of=today, max_age_days=7) is False


# ── Weekly aggregation (§3.3) ────────────────────────────────────────────────


def test_weekly_aggregation_uses_standard_ohlc_rules():
    """open=first, high=max, low=min, close=last, volume=sum."""
    # Mon 2026-01-05 through Fri 2026-01-09 — one ISO week.
    bars = [
        _bar(date(2026, 1, 5), 100.0, high=101.0, low=99.0, volume=10.0),
        _bar(date(2026, 1, 6), 102.0, high=105.0, low=101.0, volume=20.0),
        _bar(date(2026, 1, 7), 101.0, high=103.0, low=98.0, volume=30.0),
        _bar(date(2026, 1, 8), 104.0, high=106.0, low=103.0, volume=40.0),
        _bar(date(2026, 1, 9), 103.0, high=104.0, low=102.0, volume=50.0),
    ]
    weekly = to_weekly(BarSeries("TEST", "daily", bars))
    assert len(weekly) == 1
    w = weekly.bars[0]
    assert w.bar_date == date(2026, 1, 5)       # labelled by week-start Monday
    assert w.open == 100.0                      # Monday's open
    assert w.high == 106.0                      # max of the week
    assert w.low == 98.0                        # min of the week
    assert w.close == 103.0                     # Friday's close
    assert w.volume == pytest.approx(150.0)     # sum


def test_weekly_splits_across_week_boundaries():
    # Fri 2026-01-09 and Mon 2026-01-12 are different ISO weeks.
    bars = [_bar(date(2026, 1, 9), 100.0), _bar(date(2026, 1, 12), 110.0)]
    weekly = to_weekly(BarSeries("TEST", "daily", bars))
    assert len(weekly) == 2
    assert [b.bar_date for b in weekly.bars] == [date(2026, 1, 5), date(2026, 1, 12)]


def test_weekly_bars_are_labelled_by_monday_regardless_of_start_day():
    """A week that begins on Wednesday still keys to that week's Monday, so a
    partial week updates in place as it fills in rather than duplicating."""
    wednesday = date(2026, 1, 7)
    weekly = to_weekly(BarSeries("TEST", "daily", [_bar(wednesday, 100.0)]))
    assert weekly.bars[0].bar_date == date(2026, 1, 5)


def test_partial_final_week_is_included():
    """§3.3 runs nightly; a Wednesday must show a live weekly count, not last
    Friday's. A partial week is what a weekly chart shows intraweek."""
    bars = [_bar(date(2026, 1, 5), 100.0), _bar(date(2026, 1, 6), 101.0)]
    weekly = to_weekly(BarSeries("TEST", "daily", bars))
    assert len(weekly) == 1
    assert weekly.bars[0].close == 101.0


def test_weekly_volume_is_none_if_any_day_is_missing_volume():
    """Unknown-in, unknown-out — a partial sum would corrupt weekly RVOL."""
    bars = [
        _bar(date(2026, 1, 5), 100.0, volume=10.0),
        _bar(date(2026, 1, 6), 101.0, volume=None),
        _bar(date(2026, 1, 7), 102.0, volume=30.0),
    ]
    weekly = to_weekly(BarSeries("TEST", "daily", bars))
    assert weekly.bars[0].volume is None


def test_weekly_of_empty_series_is_empty():
    weekly = to_weekly(BarSeries("TEST", "daily", []))
    assert len(weekly) == 0
    assert weekly.timeframe == "weekly"


def test_weekly_output_is_ordered_and_labelled_weekly():
    start = date(2026, 1, 5)
    bars = [_bar(start + timedelta(days=i), 100.0 + i) for i in range(40)]
    weekly = to_weekly(BarSeries("TEST", "daily", bars))
    dates = [b.bar_date for b in weekly.bars]
    assert dates == sorted(dates)
    assert weekly.timeframe == "weekly"
    assert all(b.bar_date.weekday() == 0 for b in weekly.bars)   # every label is a Monday


def test_to_dict_round_trips_dates_as_iso_strings():
    series = BarSeries("TEST", "daily", [_bar(date(2026, 1, 5), 100.0)],
                       fetched_at=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc))
    payload = series.to_dict()
    assert payload["bars"][0]["bar_date"] == "2026-01-05"
    assert payload["fetched_at"].startswith("2026-01-05")
