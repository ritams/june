"""Tests for the §8 alerting layer.

The scope doc's §1 lists "State-change alerts only. No repeat pings." as
non-negotiable, and §8 is titled "Alerting — low-noise rules". So the majority of
these tests assert that something is **not** sent. A test suite for this module
that only checked alerts fire would pass happily on a system that pings on every
run — which is the actual failure mode, because a noisy alerter gets muted and
then delivers nothing at all.

The four suppression gates, each with its own section below:
  1. staleness (§3.3)
  2. band transition required (§8.1)
  3. one alert per ticker per day (§8.2)
  4. real-time vs digest routing (§8.3, §8.4)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.signals import alerts as alerts_mod
from app.services.signals import db as signals_db
from app.services.signals import store
from app.services.signals.alerts import (
    Alert,
    build_digest,
    crossed_exit_threshold,
    dispatch,
    evaluate,
    is_entry_upgrade,
    run_alert_cycle,
    send_digest,
)
from app.services.signals.config import EXIT_ALERT_THRESHOLD
from app.services.signals.scoring import Component, Score


TODAY = date(2026, 7, 29)
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "alerts_test.db"
    monkeypatch.setattr(signals_db, "SIGNALS_DB_PATH", path)
    monkeypatch.setattr(store, "connect", lambda: signals_db.connect(path))
    signals_db.init_db(path)
    return path


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retry backoff must not slow the suite."""
    monkeypatch.setattr(alerts_mod.time, "sleep", lambda _s: None)


class FakeTelegram:
    """Stands in for app/services/telegram.py's client."""

    def __init__(self, *, enabled=True, fail_times=0):
        self.enabled = enabled
        self.fail_times = fail_times
        self.messages: list[str] = []

    def send_message(self, message: str) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("telegram down")
        self.messages.append(message)
        return True


def _score(symbol, value, band, side="entry", drivers=None) -> Score:
    return Score(
        symbol=symbol,
        side=side,
        score=value,
        band=band,
        raw_score=value,
        components=[Component("td_sequential", "TD Sequential", 18.0, 25, "Daily buy setup 9")],
        drivers=drivers if drivers is not None else ["Daily buy setup 9 complete"],
    )


def _seed(
    symbol,
    *,
    entry_today,
    band_today,
    entry_prev=None,
    band_prev=None,
    exit_today=0,
    exit_band_today="No signal",
    exit_prev=None,
    held=False,
    stale=False,
    counter_trend=False,
):
    """Create a watchlist entry with up to two days of score history."""
    store.add_to_watchlist(symbol)
    if held:
        store.set_held(symbol, True)
    if entry_prev is not None:
        store.save_score(
            symbol, YESTERDAY,
            _score(symbol, entry_prev, band_prev or "Watch"),
            _score(symbol, exit_prev if exit_prev is not None else 0, "No signal", side="exit"),
            regime="Neutral", stale=False,
        )
    entry = _score(symbol, entry_today, band_today)
    entry.counter_trend = counter_trend
    store.save_score(
        symbol, TODAY, entry,
        _score(symbol, exit_today, exit_band_today, side="exit", drivers=["Exit driver"]),
        regime="Neutral", stale=stale,
    )


# ── §8.1 band-transition predicates ──────────────────────────────────────────


def test_upgrade_detection_matches_the_scope_doc_examples():
    """§8.1: "Watch → Strong, Strong → Prime"."""
    assert is_entry_upgrade("Watch", "Strong") is True
    assert is_entry_upgrade("Strong", "Prime") is True
    assert is_entry_upgrade("No signal", "Watch") is True


def test_no_alert_on_wobble_within_a_band():
    """§8.1: "Never on wobble within a band"."""
    assert is_entry_upgrade("Strong", "Strong") is False


def test_downgrades_do_not_alert_on_the_entry_side():
    assert is_entry_upgrade("Prime", "Strong") is False
    assert is_entry_upgrade("Watch", "No signal") is False


def test_no_previous_band_is_not_a_transition():
    """Stops every name alerting the day it joins the watchlist."""
    assert is_entry_upgrade(None, "Prime") is False


def test_exit_crossing_is_a_crossing_not_a_level():
    """§8.1: "Exit crossing 70" — already-above must not re-alert (§1 no repeat pings)."""
    assert crossed_exit_threshold(65, 72) is True
    assert crossed_exit_threshold(72, 75) is False      # already above yesterday
    assert crossed_exit_threshold(75, 65) is False      # falling back through
    assert crossed_exit_threshold(None, 90) is False    # no history


def test_exit_crossing_exactly_at_the_threshold_counts():
    assert crossed_exit_threshold(69, EXIT_ALERT_THRESHOLD) is True


# ── Gate 1: staleness (§3.3) ─────────────────────────────────────────────────


def test_stale_scores_never_alert():
    """§3.3: "the run flags 'stale' ... and skips alerts"."""
    _seed("STALE", entry_prev=55, band_prev="Watch", entry_today=90, band_today="Prime", stale=True)
    assert evaluate(TODAY) == []


def test_unscored_names_never_alert():
    store.add_to_watchlist("NEW")
    assert evaluate(TODAY) == []


# ── Gate 2: band transition (§8.1) ───────────────────────────────────────────


def test_upgrade_produces_one_alert():
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=75, band_today="Strong")
    result = evaluate(TODAY)
    assert len(result) == 1
    assert result[0].ticker == "AAA"
    assert result[0].direction == "buy"
    assert "Watch → Strong" in result[0].reason


def test_same_band_produces_nothing():
    _seed("AAA", entry_prev=71, band_prev="Strong", entry_today=83, band_today="Strong")
    assert evaluate(TODAY) == []


def test_first_ever_score_produces_nothing():
    """No prior band → no transition. Guards against a DB reset firing everything."""
    _seed("AAA", entry_today=95, band_today="Prime")
    assert evaluate(TODAY) == []


def test_alerts_are_scoped_to_the_requested_bar():
    """A re-run on a later day must not resurrect an old transition."""
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=90, band_today="Prime")
    assert evaluate(TODAY + timedelta(days=1)) == []


# ── §5 / §8.1 exit alerts ────────────────────────────────────────────────────


def test_exit_crossing_alerts_on_a_held_name():
    _seed("HELD", entry_prev=40, band_prev="No signal", entry_today=40, band_today="No signal",
          exit_prev=60, exit_today=80, exit_band_today="Strong", held=True)
    result = evaluate(TODAY)
    assert len(result) == 1
    assert result[0].direction == "sell"
    assert result[0].realtime is True       # money at risk pushes immediately


def test_exit_crossing_is_silent_on_an_unheld_name():
    """§5: "Exit alerts only on names toggled held"."""
    _seed("NOTHELD", entry_prev=40, band_prev="No signal", entry_today=40, band_today="No signal",
          exit_prev=60, exit_today=80, exit_band_today="Strong", held=False)
    assert evaluate(TODAY) == []


# ── §8.4 TD completion override ──────────────────────────────────────────────


@pytest.fixture
def td_verified(monkeypatch):
    """Simulate §3.1 sign-off having passed.

    The shipped default routes TD completions to the digest because the
    reference-chart reconciliation has not run — see
    docs/td-sequential-verification.md. Tests of §8.4's *documented* behaviour
    must therefore opt in explicitly rather than depend on the default, which
    is deliberately more conservative than the spec until counts are verified.
    """
    monkeypatch.setattr(alerts_mod, "TD_OVERRIDE_REALTIME_SCOPE", "weekly_and_13")


def test_td13_completion_alerts_regardless_of_band():
    """§8.4: "always notify regardless of band" — fires whatever the routing."""
    _seed("AAA", entry_prev=20, band_prev="No signal", entry_today=22, band_today="No signal")
    store.record_signal_event("AAA", TODAY, "countdown_13", "buy", "daily")
    result = evaluate(TODAY)
    assert len(result) == 1
    assert "TD13" in result[0].drivers[0]


def test_td13_pushes_realtime_once_counts_are_verified(td_verified):
    """§8.4: "any TD13 → real-time push" — the post-sign-off behaviour."""
    _seed("AAA", entry_prev=20, band_prev="No signal", entry_today=22, band_today="No signal")
    store.record_signal_event("AAA", TODAY, "countdown_13", "buy", "daily")
    assert evaluate(TODAY)[0].realtime is True


def test_td13_does_not_push_while_counts_are_unverified():
    """The shipped default. §8.4's push bypasses the band gate, so an unverified
    count firing one is the most expensive way this engine can be wrong."""
    _seed("AAA", entry_prev=20, band_prev="No signal", entry_today=22, band_today="No signal")
    store.record_signal_event("AAA", TODAY, "countdown_13", "buy", "daily")
    result = evaluate(TODAY)
    assert len(result) == 1              # still fires and still scores
    assert result[0].realtime is False   # but waits for the 21:45 digest


def test_weekly_td9_pushes_realtime_but_daily_td9_waits_for_the_digest(td_verified):
    """§8.4: "Weekly completions and any TD13 → real-time push; daily TD9s → digest"."""
    _seed("WK", entry_prev=20, band_prev="No signal", entry_today=20, band_today="No signal")
    store.record_signal_event("WK", TODAY, "setup_9", "buy", "weekly")
    _seed("DY", entry_prev=20, band_prev="No signal", entry_today=20, band_today="No signal")
    store.record_signal_event("DY", TODAY, "setup_9", "buy", "daily")

    by_ticker = {a.ticker: a for a in evaluate(TODAY)}
    assert by_ticker["WK"].realtime is True
    assert by_ticker["DY"].realtime is False


def test_default_realtime_scope_is_none_until_sign_off():
    """Pins the shipped default so a config edit can't silently re-enable pushes."""
    from app.services.signals.config import TD_COUNTS_VERIFIED, TD_OVERRIDE_REALTIME_SCOPE

    assert TD_COUNTS_VERIFIED is False
    assert TD_OVERRIDE_REALTIME_SCOPE == "none"


def test_sell_side_td_completions_also_notify():
    """§8.4: "both sides"."""
    _seed("AAA", entry_prev=20, band_prev="No signal", entry_today=20, band_today="No signal")
    store.record_signal_event("AAA", TODAY, "countdown_13", "sell", "daily")
    result = evaluate(TODAY)
    assert len(result) == 1 and result[0].direction == "sell"


def test_per_ticker_optout_silences_the_td_override():
    """§8.4: "per-ticker opt-out"."""
    _seed("AAA", entry_prev=20, band_prev="No signal", entry_today=20, band_today="No signal")
    store.record_signal_event("AAA", TODAY, "countdown_13", "buy", "daily")
    assert store.set_td_optout("AAA", True) is True
    assert evaluate(TODAY) == []


def test_td_override_can_be_disabled_globally(monkeypatch):
    """§8.4 config: `td_override.enabled`."""
    monkeypatch.setattr(alerts_mod, "TD_OVERRIDE_ENABLED", False)
    _seed("AAA", entry_prev=20, band_prev="No signal", entry_today=20, band_today="No signal")
    store.record_signal_event("AAA", TODAY, "countdown_13", "buy", "daily")
    assert evaluate(TODAY) == []


def test_realtime_scope_none_routes_everything_to_the_digest(monkeypatch):
    """§8.4 config: `td_override.realtime_scope`."""
    monkeypatch.setattr(alerts_mod, "TD_OVERRIDE_REALTIME_SCOPE", "none")
    _seed("AAA", entry_prev=20, band_prev="No signal", entry_today=20, band_today="No signal")
    store.record_signal_event("AAA", TODAY, "countdown_13", "buy", "daily")
    assert evaluate(TODAY)[0].realtime is False


def test_regime_flip_events_do_not_trigger_the_td_override():
    """Only TD9/TD13 get the §8.4 band bypass; a regime flip must go through §8.1."""
    _seed("AAA", entry_prev=20, band_prev="No signal", entry_today=20, band_today="No signal")
    store.record_signal_event("AAA", TODAY, "regime_flip", "buy", "daily")
    assert evaluate(TODAY) == []


# ── Gate 3: one alert per ticker per day (§8.2) ───────────────────────────────


def test_multiple_drivers_merge_into_one_alert():
    """§8.2: "multiple drivers on one name merge into one message"."""
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=90, band_today="Prime")
    store.record_signal_event("AAA", TODAY, "countdown_13", "buy", "daily")
    store.record_signal_event("AAA", TODAY, "setup_9", "buy", "weekly")
    result = evaluate(TODAY)
    assert len(result) == 1                      # one alert, not three
    assert len(result[0].drivers) >= 2           # but every driver survives
    assert "TD13" in " ".join(result[0].drivers)
    assert "TD9" in " ".join(result[0].drivers)


def test_merged_alert_keeps_realtime_if_any_constituent_was():
    """A Prime transition must not be downgraded by bundling with a daily TD9."""
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=90, band_today="Prime")
    store.record_signal_event("AAA", TODAY, "setup_9", "buy", "daily")   # digest-level alone
    assert evaluate(TODAY)[0].realtime is True


def test_second_alert_for_the_same_ticker_same_day_is_suppressed():
    """§8.2's hard cap, enforced across calls via the DB not memory."""
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=90, band_today="Prime")
    first = evaluate(TODAY)
    assert len(first) == 1
    dispatch(first, telegram_client=FakeTelegram())
    assert evaluate(TODAY) == []                 # cap held


def test_the_cap_survives_a_failed_delivery():
    """A flapping transport must not re-alert on every cycle.

    log_alert() records before sending precisely so the day's slot is consumed
    by the decision, not by the delivery outcome.
    """
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=90, band_today="Prime")
    dispatch(evaluate(TODAY), telegram_client=FakeTelegram(fail_times=99))
    assert evaluate(TODAY) == []


def test_the_digest_does_not_consume_a_tickers_daily_slot():
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=90, band_today="Prime")
    send_digest(TODAY, telegram_client=FakeTelegram())
    assert len(evaluate(TODAY)) == 1


def test_alerts_are_ranked_strongest_first():
    """A truncated digest must keep the most important names."""
    _seed("LOW", entry_prev=40, band_prev="No signal", entry_today=55, band_today="Watch")
    _seed("HIGH", entry_prev=55, band_prev="Watch", entry_today=95, band_today="Prime")
    assert [a.ticker for a in evaluate(TODAY)] == ["HIGH", "LOW"]


# ── §8.6 dispatch, payload, retry, logging ───────────────────────────────────


def test_payload_matches_the_scope_doc_shape():
    """§8.6: "{type, ticker, direction, score, band, drivers, timestamp}"."""
    payload = Alert(alert_type="signal", ticker="AAA", direction="buy", score=90,
                    band="Prime", drivers=["x"]).to_payload()
    assert set(payload) == {"type", "ticker", "direction", "score", "band", "drivers", "timestamp"}
    assert payload["type"] == "signal"
    # Routing metadata must not leak onto the wire.
    assert "realtime" not in payload and "reason" not in payload


def test_realtime_alerts_send_and_digest_alerts_are_held():
    _seed("RT", entry_prev=55, band_prev="Watch", entry_today=95, band_today="Prime")
    _seed("DG", entry_prev=40, band_prev="No signal", entry_today=55, band_today="Watch")
    tg = FakeTelegram()
    result = dispatch(evaluate(TODAY), telegram_client=tg)
    assert result["sent"] == ["RT"]
    assert result["held_for_digest"] == ["DG"]
    assert len(tg.messages) == 1


def test_every_alert_is_logged_even_when_held():
    _seed("DG", entry_prev=40, band_prev="No signal", entry_today=55, band_today="Watch")
    dispatch(evaluate(TODAY), telegram_client=FakeTelegram())
    logged = store.recent_alerts()
    assert len(logged) == 1
    assert logged[0]["symbol"] == "DG"
    assert logged[0]["delivered"] is False      # held, not delivered


def test_delivery_retries_then_succeeds():
    """§8.6: "retried"."""
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=95, band_today="Prime")
    tg = FakeTelegram(fail_times=2)
    result = dispatch(evaluate(TODAY), telegram_client=tg)
    assert result["sent"] == ["AAA"]
    logged = store.recent_alerts()[0]
    assert logged["delivered"] is True
    assert logged["attempts"] == 3


def test_delivery_failure_is_recorded_with_the_error():
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=95, band_today="Prime")
    result = dispatch(evaluate(TODAY), telegram_client=FakeTelegram(fail_times=99))
    assert result["failed"] == ["AAA"]
    logged = store.recent_alerts()[0]
    assert logged["delivered"] is False
    assert "telegram down" in (logged["last_error"] or "")


def test_no_sink_configured_is_a_failure_not_a_silent_success():
    """Must not read as delivered — that would hide a misconfigured deployment."""
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=95, band_today="Prime")
    result = dispatch(evaluate(TODAY), telegram_client=None)
    assert result["failed"] == ["AAA"]
    assert "No alert sink configured" in (store.recent_alerts()[0]["last_error"] or "")


def test_disabled_telegram_client_is_treated_as_no_sink():
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=95, band_today="Prime")
    result = dispatch(evaluate(TODAY), telegram_client=FakeTelegram(enabled=False))
    assert result["failed"] == ["AAA"]


def test_hermes_webhook_is_preferred_when_configured(monkeypatch):
    """§8.6: "outbound POST" to Hermes when a URL is set."""
    posted = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    def fake_post(url, json=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        return FakeResponse()

    import httpx

    monkeypatch.setattr(alerts_mod, "HERMES_WEBHOOK_URL", "https://hermes.example/hook")
    monkeypatch.setattr(httpx, "post", fake_post)

    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=95, band_today="Prime")
    tg = FakeTelegram()
    result = dispatch(evaluate(TODAY), telegram_client=tg)
    assert result["sent"] == ["AAA"]
    assert posted["url"] == "https://hermes.example/hook"
    assert posted["json"]["ticker"] == "AAA"
    assert tg.messages == []       # webhook wins; Telegram not used


def test_alert_render_merges_drivers_into_one_message():
    msg = Alert(alert_type="signal", ticker="AAA", direction="buy", score=90, band="Prime",
                drivers=["driver one", "driver two"], reason="Watch → Prime").render()
    assert "AAA" in msg and "Prime" in msg
    assert "driver one" in msg and "driver two" in msg


# ── §8.3 daily digest ────────────────────────────────────────────────────────


def test_digest_is_none_when_nothing_is_scored():
    """§1's no-repeat-pings extends to not sending a nightly "nothing happened"."""
    store.add_to_watchlist("AAA")
    assert build_digest(TODAY) is None


def test_digest_lists_top_entry_scores():
    """§8.3: "top 5 entry scores"."""
    for i, sym in enumerate(["A", "B", "C", "D", "E", "F", "G"]):
        _seed(sym, entry_today=90 - i * 5, band_today="Strong")
    digest = build_digest(TODAY)
    assert "Top 5 entry" in digest
    assert "A" in digest and "E" in digest
    assert "G" not in digest.split("Top 5 entry")[1].split("\n\n")[0]


def test_digest_calls_out_prime_signals():
    """§8.3: "Prime signals"."""
    _seed("PRIME", entry_today=92, band_today="Prime")
    digest = build_digest(TODAY)
    assert "Prime:" in digest
    assert "PRIME" in digest


def test_digest_lists_exit_warnings_only_for_held_names():
    """§8.3: "exit warnings on held names"."""
    _seed("HELD", entry_today=40, band_today="No signal", exit_today=85,
          exit_band_today="Prime", held=True)
    _seed("FREE", entry_today=40, band_today="No signal", exit_today=85,
          exit_band_today="Prime", held=False)
    digest = build_digest(TODAY)
    exit_block = digest.split("Exit warnings")[1]
    assert "HELD" in exit_block
    assert "FREE" not in exit_block


def test_digest_includes_a_td_counts_line():
    """§8.3: "TD counts line" — where §8.4's daily TD9s land."""
    _seed("AAA", entry_today=50, band_today="Watch")
    store.record_signal_event("AAA", TODAY, "setup_9", "buy", "daily")
    digest = build_digest(TODAY)
    assert "TD counts:" in digest
    assert "AAA buy9·D" in digest


def test_digest_flags_stale_names():
    """§3.3: staleness must be visible, not silent."""
    _seed("STALE", entry_today=50, band_today="Watch", stale=True)
    assert "stale data" in build_digest(TODAY)


def test_digest_badges_counter_trend_names():
    """§5's regime gate badge must reach the digest, not just the dashboard."""
    _seed("CT", entry_today=60, band_today="Watch", counter_trend=True)
    assert "counter-trend" in build_digest(TODAY)


def test_send_digest_delivers_and_logs():
    _seed("AAA", entry_today=90, band_today="Prime")
    tg = FakeTelegram()
    result = send_digest(TODAY, telegram_client=tg)
    assert result["sent"] is True
    assert len(tg.messages) == 1
    logged = store.recent_alerts()[0]
    assert logged["alert_type"] == "digest"
    assert logged["symbol"] is None
    assert logged["delivered"] is True


def test_send_digest_with_nothing_to_report_sends_nothing():
    store.add_to_watchlist("AAA")
    result = send_digest(TODAY, telegram_client=FakeTelegram())
    assert result["sent"] is False
    assert store.recent_alerts() == []


# ── Orchestration ────────────────────────────────────────────────────────────


def test_run_alert_cycle_on_a_quiet_day_sends_nothing():
    _seed("AAA", entry_prev=71, band_prev="Strong", entry_today=80, band_today="Strong")
    result = run_alert_cycle(TODAY, telegram_client=FakeTelegram())
    assert result["evaluated"] == 0
    assert result["sent"] == []


def test_run_alert_cycle_end_to_end():
    _seed("AAA", entry_prev=55, band_prev="Watch", entry_today=95, band_today="Prime")
    tg = FakeTelegram()
    result = run_alert_cycle(TODAY, telegram_client=tg)
    assert result["evaluated"] == 1
    assert result["sent"] == ["AAA"]
    assert "AAA" in tg.messages[0]
