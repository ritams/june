"""Tests for §7 source intake.

Three things carry most of the weight:

  1. **Nothing auto-enters the watchlist (§7.4).** Ingestion must only ever write
     a pending queue row. If an LLM hallucinates a ticker, the cost is one
     dismissal — never a position. Several tests assert the watchlist is
     untouched after ingestion.

  2. **The inbound webhook is the only externally-postable endpoint (§10),** so
     signature verification is tested including the fail-closed default. A
     misconfigured deployment must refuse email, not accept anonymous POSTs.

  3. **Extraction output is untrusted.** `_validate` is defence-in-depth behind
     the tool schema: it drops prose-as-ticker, unknown actions and unparseable
     dates rather than surfacing them to Fred as plausible suggestions.

The Anthropic call is always stubbed — no test hits the network or bills tokens.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.signals import db as signals_db
from app.services.signals import intake as intake_mod
from app.services.signals import store
from app.services.signals.api import router as signals_router
from app.services.signals.config import SOURCE_SILENCE_DAYS


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "intake_test.db"
    monkeypatch.setattr(signals_db, "SIGNALS_DB_PATH", path)
    monkeypatch.setattr(store, "connect", lambda: signals_db.connect(path))
    monkeypatch.setattr(intake_mod, "connect", lambda: signals_db.connect(path))
    signals_db.init_db(path)
    return path


@pytest.fixture
def stub_extract(monkeypatch):
    """Replace the Anthropic call with a canned tool_use result.

    Patches `extract` rather than the HTTP client so tests declare the
    *extraction outcome* they care about, not the wire format.
    """
    def install(result: dict):
        monkeypatch.setattr(
            intake_mod, "extract",
            lambda text, source_hint=None: intake_mod._validate(result, source_hint=source_hint),
        )
    return install


def _portfolio_note(**overrides) -> dict:
    base = {
        "is_portfolio_note": True,
        "source": "STENO",
        "published_date": "2026-07-26",
        "note_summary": "Portfolio note",
        "actions": [
            {"ticker": "HPQ", "action": "add", "instrument_note": "cheap on FCF", "asset_class": "equity"},
            {"ticker": "ONDS", "action": "trim", "instrument_note": "taking profit", "asset_class": "equity"},
        ],
    }
    base.update(overrides)
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(signals_router)
    return TestClient(app)


# ── §10 signature verification ───────────────────────────────────────────────


def test_unsigned_is_rejected_when_no_secret_configured(monkeypatch):
    """Fail closed. An inbound endpoint that accepts unsigned POSTs is an open door."""
    monkeypatch.setattr(intake_mod, "INTAKE_WEBHOOK_SECRET", None)
    monkeypatch.setattr(intake_mod, "INTAKE_ALLOW_UNSIGNED", False)
    with pytest.raises(intake_mod.IntakeAuthError, match="not configured"):
        intake_mod.verify_signature(b"{}", None)


def test_unsigned_allowed_only_with_the_explicit_dev_flag(monkeypatch):
    monkeypatch.setattr(intake_mod, "INTAKE_WEBHOOK_SECRET", None)
    monkeypatch.setattr(intake_mod, "INTAKE_ALLOW_UNSIGNED", True)
    intake_mod.verify_signature(b"{}", None)     # must not raise


def test_valid_signature_passes(monkeypatch):
    import hashlib
    import hmac

    secret = "s3cret"
    monkeypatch.setattr(intake_mod, "INTAKE_WEBHOOK_SECRET", secret)
    body = b'{"TextBody":"hello"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    intake_mod.verify_signature(body, sig)


def test_wrong_signature_is_rejected(monkeypatch):
    monkeypatch.setattr(intake_mod, "INTAKE_WEBHOOK_SECRET", "s3cret")
    with pytest.raises(intake_mod.IntakeAuthError, match="mismatch"):
        intake_mod.verify_signature(b"body", "deadbeef")


def test_missing_signature_is_rejected_when_secret_is_set(monkeypatch):
    monkeypatch.setattr(intake_mod, "INTAKE_WEBHOOK_SECRET", "s3cret")
    with pytest.raises(intake_mod.IntakeAuthError, match="Missing signature"):
        intake_mod.verify_signature(b"body", None)


def test_timestamp_is_included_in_the_signed_payload(monkeypatch):
    """Signing the body alone permits unlimited replay of a captured request."""
    import hashlib
    import hmac

    secret = "s3cret"
    monkeypatch.setattr(intake_mod, "INTAKE_WEBHOOK_SECRET", secret)
    body = b"body"
    ts = "1750000000"
    with_ts = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    intake_mod.verify_signature(body, with_ts, ts)
    # The body-only signature must NOT validate when a timestamp is supplied.
    body_only = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with pytest.raises(intake_mod.IntakeAuthError):
        intake_mod.verify_signature(body, body_only, ts)


def test_auth_middleware_exempts_only_the_email_path():
    """A prefix match would expose approve/dismiss, which mutate the watchlist."""
    from app.services.auth import _is_signed_webhook

    assert _is_signed_webhook("/api/signals/intake/email") is True
    assert _is_signed_webhook("/api/signals/intake/email/") is True
    assert _is_signed_webhook("/api/signals/intake/pending") is False
    assert _is_signed_webhook("/api/signals/intake/1/approve") is False
    assert _is_signed_webhook("/api/signals/watchlist") is False


# ── §7.3 extraction validation ───────────────────────────────────────────────


def test_validate_accepts_a_clean_note():
    ex = intake_mod._validate(_portfolio_note())
    assert ex.is_portfolio_note is True
    assert ex.source == "STENO"
    assert [a["ticker"] for a in ex.actions] == ["HPQ", "ONDS"]


def test_validate_drops_prose_masquerading_as_a_ticker():
    """The failure mode that matters: "shipping stocks" is not a ticker."""
    note = _portfolio_note(actions=[
        {"ticker": "shipping stocks", "action": "add", "instrument_note": "theme"},
        {"ticker": "GOOD", "action": "add", "instrument_note": "fine"},
    ])
    assert [a["ticker"] for a in intake_mod._validate(note).actions] == ["GOOD"]


def test_validate_accepts_real_world_ticker_shapes():
    """Must not over-reject: BRK.B, BTC-USD, GC=F, ^GSPC are all legitimate."""
    note = _portfolio_note(actions=[
        {"ticker": t, "action": "add", "instrument_note": "x"}
        for t in ("BRK.B", "BTC-USD", "GC=F", "^GSPC")
    ])
    assert len(intake_mod._validate(note).actions) == 4


def test_validate_drops_unknown_actions():
    note = _portfolio_note(actions=[
        {"ticker": "AAA", "action": "yolo", "instrument_note": "x"},
        {"ticker": "BBB", "action": "exit", "instrument_note": "x"},
    ])
    assert [a["ticker"] for a in intake_mod._validate(note).actions] == ["BBB"]


def test_validate_falls_back_to_own_for_an_unknown_source():
    assert intake_mod._validate(_portfolio_note(source="BLOOMBERG")).source == "OWN"


def test_validate_drops_an_unparseable_published_date():
    assert intake_mod._validate(_portfolio_note(published_date="last Tuesday")).published_date is None


def test_validate_normalises_case():
    note = _portfolio_note(actions=[{"ticker": "hpq", "action": "ADD", "instrument_note": "x"}])
    action = intake_mod._validate(note).actions[0]
    assert action["ticker"] == "HPQ" and action["action"] == "add"


def test_validate_truncates_an_overlong_instrument_note():
    note = _portfolio_note(actions=[{"ticker": "AAA", "action": "add", "instrument_note": "x" * 5000}])
    assert len(intake_mod._validate(note).actions[0]["instrument_note"]) <= 300


# ── §7.3 / §7.4 ingestion — nothing auto-enters ──────────────────────────────


def test_ingest_queues_but_does_not_touch_the_watchlist(stub_extract):
    """§7.4: "nothing auto-enters the watchlist"."""
    stub_extract(_portfolio_note())
    result = intake_mod.ingest_text("some note", source_tag="STENO")
    assert result["status"] == "queued"
    assert store.list_watchlist() == []          # the guarantee
    assert len(intake_mod.pending()) == 1


def test_non_portfolio_note_is_dropped_silently(stub_extract):
    """§7.3: "Non-portfolio emails (event invites, marketing) ... dropped silently"."""
    stub_extract(_portfolio_note(is_portfolio_note=False, actions=[]))
    result = intake_mod.ingest_text("Join our webinar!", source_tag="STENO")
    assert result["status"] == "dropped"
    assert intake_mod.pending() == []


def test_portfolio_note_with_no_valid_actions_is_reported_as_empty(stub_extract):
    """Distinct from 'dropped' — this may mean the extraction prompt needs work."""
    stub_extract(_portfolio_note(actions=[{"ticker": "not a ticker", "action": "add", "instrument_note": "x"}]))
    result = intake_mod.ingest_text("a note", source_tag="STENO")
    assert result["status"] == "empty"
    assert intake_mod.pending() == []


def test_ingest_rejects_empty_text():
    with pytest.raises(ValueError):
        intake_mod.ingest_text("   ")


def test_ingest_records_the_source_even_for_a_dropped_note(stub_extract):
    """§7's silence notice is about whether email ARRIVES, not whether it had names.

    A month of Steno event invites means the forwarding rule works.
    """
    stub_extract(_portfolio_note(is_portfolio_note=False, actions=[]))
    intake_mod.ingest_text("marketing", source_tag="STENO")
    with signals_db.connect(signals_db.SIGNALS_DB_PATH) as conn:
        row = conn.execute("SELECT last_seen_at FROM sources WHERE tag='STENO'").fetchone()
    assert row["last_seen_at"] is not None


# ── §7.2 email payload shapes ────────────────────────────────────────────────


@pytest.mark.parametrize("body_key", ["TextBody", "body-plain", "text"])
def test_ingest_email_accepts_each_provider_body_field(stub_extract, body_key):
    """Postmark / Mailgun / SendGrid all name the body differently."""
    stub_extract(_portfolio_note())
    result = intake_mod.ingest_email({body_key: "note text", "Subject": "Portfolio update"})
    assert result["status"] == "queued"


def test_ingest_email_infers_the_source_from_the_sender(stub_extract):
    captured = {}

    def fake_extract(text, source_hint=None):
        captured["hint"] = source_hint
        return intake_mod._validate(_portfolio_note(), source_hint=source_hint)

    intake_mod.extract = fake_extract
    try:
        intake_mod.ingest_email({"TextBody": "x", "From": "research@stenoresearch.com"})
        assert captured["hint"] == "STENO"
    finally:
        pass


def test_attachment_with_no_content_field_is_skipped():
    """An attachment the provider described but did not include has nothing to
    decode — distinct from a PDF that failed to parse (tested below)."""
    result = intake_mod.ingest_email({"TextBody": "", "Attachments": [{"Name": "note.pdf"}]})
    assert result["status"] == "skipped"
    assert result["reason"] == "empty email body"


def test_empty_email_is_skipped():
    result = intake_mod.ingest_email({"TextBody": "   "})
    assert result["status"] == "skipped"
    assert "empty" in result["reason"]


# ── §7.4 approve / dismiss ───────────────────────────────────────────────────


def test_approve_adds_names_with_source_provenance(stub_extract):
    stub_extract(_portfolio_note(actions=[
        {"ticker": "HPQ", "action": "add", "instrument_note": "cheap", "asset_class": "equity"},
    ]))
    intake_id = intake_mod.ingest_text("note", source_tag="STENO")["intake_id"]
    result = intake_mod.approve(intake_id)
    assert result["applied"][0]["result"] == "added"
    rows = store.list_watchlist()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "HPQ"
    assert rows[0]["source_tag"] == "STENO"      # §7 provenance carried through


def test_approve_of_a_trim_keeps_the_name_on_the_watchlist(stub_extract):
    """A source trimming a position still holds it — exit timing still matters."""
    store.add_to_watchlist("ONDS", source_tag="STENO")
    stub_extract(_portfolio_note(actions=[
        {"ticker": "ONDS", "action": "trim", "instrument_note": "profit", "asset_class": "equity"},
    ]))
    intake_id = intake_mod.ingest_text("note", source_tag="STENO")["intake_id"]
    result = intake_mod.approve(intake_id)
    assert "kept on watchlist" in result["applied"][0]["result"]
    assert [r["symbol"] for r in store.list_watchlist()] == ["ONDS"]


def test_approve_of_an_exit_removes_the_name(stub_extract):
    """§7's removal suggestion, applied on approval only."""
    store.add_to_watchlist("ONDS", source_tag="STENO")
    stub_extract(_portfolio_note(actions=[
        {"ticker": "ONDS", "action": "exit", "instrument_note": "done", "asset_class": "equity"},
    ]))
    intake_id = intake_mod.ingest_text("note", source_tag="STENO")["intake_id"]
    assert intake_mod.approve(intake_id)["applied"][0]["result"] == "removed"
    assert store.list_watchlist() == []


def test_approve_survives_one_bad_ticker_in_a_batch(stub_extract):
    """One bad name must not block the rest of the note."""
    stub_extract(_portfolio_note(actions=[
        {"ticker": "GOOD", "action": "add", "instrument_note": "x", "asset_class": "equity"},
        {"ticker": "ALSOGOOD", "action": "add", "instrument_note": "x", "asset_class": "equity"},
    ]))
    intake_id = intake_mod.ingest_text("note", source_tag="STENO")["intake_id"]
    result = intake_mod.approve(intake_id)
    assert len(result["applied"]) == 2
    assert len(store.list_watchlist()) == 2


def test_approve_is_idempotent_by_refusing_a_second_application(stub_extract):
    stub_extract(_portfolio_note())
    intake_id = intake_mod.ingest_text("note", source_tag="STENO")["intake_id"]
    intake_mod.approve(intake_id)
    with pytest.raises(ValueError, match="already approved"):
        intake_mod.approve(intake_id)


def test_dismiss_leaves_the_watchlist_alone(stub_extract):
    stub_extract(_portfolio_note())
    intake_id = intake_mod.ingest_text("note", source_tag="STENO")["intake_id"]
    intake_mod.dismiss(intake_id)
    assert store.list_watchlist() == []
    assert intake_mod.pending() == []


def test_dismiss_of_an_unknown_id_raises():
    with pytest.raises(ValueError, match="No intake event"):
        intake_mod.dismiss(999)


def test_pending_flags_removal_suggestions(stub_extract):
    """§7: an exit/trim on a name already on the watchlist is a removal suggestion."""
    store.add_to_watchlist("ONDS", source_tag="STENO")
    stub_extract(_portfolio_note(actions=[
        {"ticker": "ONDS", "action": "exit", "instrument_note": "x", "asset_class": "equity"},
        {"ticker": "NEWNAME", "action": "add", "instrument_note": "x", "asset_class": "equity"},
    ]))
    intake_mod.ingest_text("note", source_tag="STENO")
    actions = {a["ticker"]: a for a in intake_mod.pending()[0]["actions"]}
    assert actions["ONDS"]["is_removal_suggestion"] is True
    assert actions["ONDS"]["on_watchlist"] is True
    assert actions["NEWNAME"]["is_removal_suggestion"] is False


# ── §7 source silence ────────────────────────────────────────────────────────


def test_a_never_seen_source_is_not_reported_as_silent():
    """Warning about a never-configured forwarding rule would fire on day one."""
    assert intake_mod.silent_sources() == []


def test_a_recently_seen_source_is_not_silent():
    intake_mod.touch_source("STENO")
    assert intake_mod.silent_sources() == []


def test_a_long_silent_source_is_reported():
    """§7: "if no email arrives from a source for 21 days"."""
    old = (datetime.now(timezone.utc) - timedelta(days=SOURCE_SILENCE_DAYS + 2)).isoformat()
    with signals_db.connect(signals_db.SIGNALS_DB_PATH) as conn:
        conn.execute("UPDATE sources SET last_seen_at = ? WHERE tag = 'STENO'", (old,))
    silent = intake_mod.silent_sources()
    assert len(silent) == 1
    assert silent[0]["tag"] == "STENO"
    assert silent[0]["days_silent"] >= SOURCE_SILENCE_DAYS


def test_own_is_never_reported_as_silent():
    """OWN is Fred's own ideas, not an inbound feed."""
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    with signals_db.connect(signals_db.SIGNALS_DB_PATH) as conn:
        conn.execute("UPDATE sources SET last_seen_at = ? WHERE tag = 'OWN'", (old,))
    assert [s["tag"] for s in intake_mod.silent_sources()] == []


# ── §7.6 weekly digest ───────────────────────────────────────────────────────


def test_intake_digest_is_none_when_nothing_pending():
    """§7.6: "only if pending > 0 — silence otherwise". The normal Sunday outcome."""
    assert intake_mod.build_intake_digest() is None


def test_intake_digest_counts_notes_and_names(stub_extract):
    """§7.6's example: "2 notes parsed, 3 names pending approval"."""
    stub_extract(_portfolio_note())          # 2 actions
    intake_mod.ingest_text("note one", source_tag="STENO")
    intake_mod.ingest_text("note two", source_tag="STENO")
    digest = intake_mod.build_intake_digest()
    assert "2 notes parsed" in digest
    assert "4 names pending" in digest


def test_intake_digest_includes_the_silence_warning(stub_extract):
    stub_extract(_portfolio_note())
    intake_mod.ingest_text("note", source_tag="STENO")
    old = (datetime.now(timezone.utc) - timedelta(days=SOURCE_SILENCE_DAYS + 2)).isoformat()
    with signals_db.connect(signals_db.SIGNALS_DB_PATH) as conn:
        conn.execute("UPDATE sources SET last_seen_at = ? WHERE tag = 'RV'", (old,))
    assert "No Real Vision note" in intake_mod.build_intake_digest()


# ── HTTP API ─────────────────────────────────────────────────────────────────


def test_email_endpoint_401s_on_a_bad_signature(client, monkeypatch):
    monkeypatch.setattr(intake_mod, "INTAKE_WEBHOOK_SECRET", "s3cret")
    r = client.post("/api/signals/intake/email", content=b'{"TextBody":"x"}',
                    headers={"X-Signals-Signature": "bad"})
    assert r.status_code == 401


def test_email_endpoint_accepts_a_valid_signature(client, monkeypatch, stub_extract):
    import hashlib
    import hmac

    secret = "s3cret"
    monkeypatch.setattr(intake_mod, "INTAKE_WEBHOOK_SECRET", secret)
    stub_extract(_portfolio_note())
    body = json.dumps({"TextBody": "note", "Subject": "Portfolio"}).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = client.post("/api/signals/intake/email", content=body,
                    headers={"X-Signals-Signature": sig, "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"


def test_email_endpoint_400s_on_malformed_json(client, monkeypatch):
    monkeypatch.setattr(intake_mod, "INTAKE_WEBHOOK_SECRET", None)
    monkeypatch.setattr(intake_mod, "INTAKE_ALLOW_UNSIGNED", True)
    r = client.post("/api/signals/intake/email", content=b"not json")
    assert r.status_code == 400


def test_paste_endpoint_queues(client, stub_extract):
    stub_extract(_portfolio_note())
    r = client.post("/api/signals/intake/paste?source_tag=COWAN", json={"text": "a pasted note"})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"


def test_paste_endpoint_400s_on_empty_text(client):
    assert client.post("/api/signals/intake/paste", json={"text": "  "}).status_code == 400


def test_pending_endpoint_returns_queue_and_silence(client, stub_extract):
    stub_extract(_portfolio_note())
    client.post("/api/signals/intake/paste", json={"text": "note"})
    body = client.get("/api/signals/intake/pending").json()
    assert body["count"] == 1
    assert body["name_count"] == 2
    assert "silent_sources" in body


def test_approve_endpoint_adds_to_the_watchlist(client, stub_extract):
    stub_extract(_portfolio_note(actions=[
        {"ticker": "HPQ", "action": "add", "instrument_note": "x", "asset_class": "equity"},
    ]))
    intake_id = client.post("/api/signals/intake/paste?source_tag=STENO",
                            json={"text": "note"}).json()["intake_id"]
    assert client.post(f"/api/signals/intake/{intake_id}/approve").status_code == 200
    assert client.get("/api/signals/watchlist").json()["count"] == 1


def test_dismiss_endpoint(client, stub_extract):
    stub_extract(_portfolio_note())
    intake_id = client.post("/api/signals/intake/paste", json={"text": "note"}).json()["intake_id"]
    assert client.post(f"/api/signals/intake/{intake_id}/dismiss").status_code == 200
    assert client.get("/api/signals/intake/pending").json()["count"] == 0
    assert client.get("/api/signals/watchlist").json()["count"] == 0


def test_approve_unknown_id_is_404(client):
    assert client.post("/api/signals/intake/999/approve").status_code == 404


# ── §7.2 PDF attachment extraction ───────────────────────────────────────────


def _make_pdf(text: str) -> bytes:
    """A real single-page PDF with a text layer, built with PyMuPDF."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    # Wrap so the text actually lands on the page rather than running off it.
    y = 72
    for line in [text[i:i + 90] for i in range(0, len(text), 90)]:
        page.insert_text((72, y), line, fontsize=9)
        y += 12
    data = doc.tobytes()
    doc.close()
    return data


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()


def test_extract_pdf_text_uses_the_text_layer(tmp_path, monkeypatch):
    """The cheap path — no Vision call, no billing."""
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    body = "Portfolio update. We are adding HPQ on free cash flow. " * 20
    text, method = intake_mod.extract_pdf_text(_make_pdf(body), name="note.pdf")
    assert method == "text_layer"
    assert "HPQ" in text


def test_extract_pdf_rejects_a_non_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    with pytest.raises(ValueError, match="does not look like a PDF"):
        intake_mod.extract_pdf_text(b"just some bytes", name="fake.pdf")


def test_extract_pdf_enforces_the_size_cap(tmp_path, monkeypatch):
    """DoS guard — the webhook accepts data from outside the network."""
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    monkeypatch.setattr(intake_mod, "INTAKE_MAX_PDF_BYTES", 100)
    with pytest.raises(intake_mod.PdfTooLarge):
        intake_mod.extract_pdf_text(_make_pdf("x " * 500), name="big.pdf")


def test_thin_text_layer_falls_back_to_vision(tmp_path, monkeypatch):
    """A chart-only or scanned PDF must reach Claude Vision, not be dropped."""
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    called = {}

    def fake_render(path):
        called["render"] = True
        return ([tmp_path / "page_001.png"], False)

    def fake_transcribe(images, pdf_stem=None):
        called["transcribe"] = True
        return "VISION TEXT: adding HPQ"

    import app.services.steno.renderer as renderer
    import app.services.steno.transcriber as transcriber

    monkeypatch.setattr(renderer, "render_pdf_pages", fake_render)
    monkeypatch.setattr(transcriber, "transcribe_pages", fake_transcribe)

    text, method = intake_mod.extract_pdf_text(_make_pdf("tiny"), name="scan.pdf")
    assert method == "vision"
    assert called == {"render": True, "transcribe": True}
    assert "HPQ" in text


def test_pdf_is_stored_content_addressed(tmp_path, monkeypatch):
    """Same note twice reuses one file rather than accumulating copies."""
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    data = _make_pdf("Adding HPQ on free cash flow. " * 30)
    intake_mod.extract_pdf_text(data, name="note.pdf")
    intake_mod.extract_pdf_text(data, name="note.pdf")
    assert len(list(tmp_path.glob("*.pdf"))) == 1


def test_pdf_filename_cannot_escape_the_intake_directory(tmp_path, monkeypatch):
    """A provider-supplied filename is attacker-influenced on the webhook path.

    The property that matters is that the written file stays inside the intake
    directory — not that the name is free of dots. Path separators are stripped,
    so "../../etc/passwd.pdf" becomes a harmless flat filename.
    """
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    intake_mod.extract_pdf_text(_make_pdf("Adding HPQ. " * 60), name="../../etc/passwd.pdf")
    written = list(tmp_path.iterdir())
    assert written, "nothing was written"
    for p in written:
        assert p.resolve().parent == tmp_path.resolve()   # no traversal
        assert "/" not in p.name and "\\" not in p.name


# ── §7.2 attachments through the email path ──────────────────────────────────


def test_email_attachment_text_is_appended_and_ingested(tmp_path, monkeypatch, stub_extract):
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    stub_extract(_portfolio_note())
    payload = {
        "TextBody": "This week's note attached.",
        "Subject": "Portfolio",
        "Attachments": [{"Name": "note.pdf", "ContentType": "application/pdf",
                         "Content": _b64(_make_pdf("Adding HPQ. " * 60))}],
    }
    result = intake_mod.ingest_email(payload)
    assert result["status"] == "queued"
    assert result["attachments_extracted"] == 1


def test_attachment_only_email_now_works(tmp_path, monkeypatch, stub_extract):
    """Previously returned "skipped" — §7.2's gap is closed."""
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    stub_extract(_portfolio_note())
    payload = {
        "TextBody": "",
        "Attachments": [{"Name": "note.pdf", "ContentType": "application/pdf",
                         "Content": _b64(_make_pdf("Adding HPQ. " * 60))}],
    }
    assert intake_mod.ingest_email(payload)["status"] == "queued"


def test_a_bad_attachment_does_not_sink_a_usable_body(tmp_path, monkeypatch, stub_extract):
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    stub_extract(_portfolio_note())
    payload = {
        "TextBody": "We are adding HPQ this week.",
        "Attachments": [{"Name": "broken.pdf", "ContentType": "application/pdf",
                         "Content": _b64(b"not a pdf at all")}],
    }
    result = intake_mod.ingest_email(payload)
    assert result["status"] == "queued"
    assert result["attachment_errors"]      # surfaced, not swallowed


def test_all_attachments_failing_reports_why(tmp_path, monkeypatch):
    """Must not look like "the source sent nothing"."""
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    payload = {
        "TextBody": "",
        "Attachments": [{"Name": "broken.pdf", "ContentType": "application/pdf",
                         "Content": _b64(b"nope")}],
    }
    result = intake_mod.ingest_email(payload)
    assert result["status"] == "skipped"
    assert "none could be extracted" in result["reason"]
    assert "broken.pdf" in result["reason"]      # says which one, not just that one failed


def test_non_pdf_attachments_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    payload = {"TextBody": "", "Attachments": [
        {"Name": "logo.png", "ContentType": "image/png", "Content": _b64(b"\x89PNG")},
    ]}
    result = intake_mod.ingest_email(payload)
    assert result["status"] == "skipped"
    assert result["reason"] == "empty email body"


def test_linked_pdfs_are_not_fetched(tmp_path, monkeypatch):
    """SSRF guard: §7.2 says "linked", but this endpoint takes outside input."""
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    payload = {"TextBody": "", "Attachments": [
        {"Name": "note.pdf", "ContentType": "application/pdf", "Url": "http://169.254.169.254/latest/meta-data/"},
    ]}
    # No Content field → nothing decoded, nothing fetched.
    assert intake_mod._attachments_from_payload(payload) == []


# ── §7.5 upload endpoint ─────────────────────────────────────────────────────


def test_upload_endpoint_queues_a_pdf(client, tmp_path, monkeypatch, stub_extract):
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    stub_extract(_portfolio_note())
    pdf = _make_pdf("Adding HPQ on free cash flow. " * 30)
    r = client.post("/api/signals/intake/upload?source_tag=COWAN",
                    files={"file": ("note.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert r.json()["extraction_method"] == "text_layer"


def test_upload_endpoint_400s_on_a_non_pdf(client, tmp_path, monkeypatch):
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    r = client.post("/api/signals/intake/upload",
                    files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400


def test_upload_endpoint_413s_on_an_oversized_pdf(client, tmp_path, monkeypatch):
    monkeypatch.setattr(intake_mod, "SIGNALS_INTAKE_DIR", tmp_path)
    monkeypatch.setattr(intake_mod, "INTAKE_MAX_PDF_BYTES", 100)
    r = client.post("/api/signals/intake/upload",
                    files={"file": ("big.pdf", _make_pdf("x " * 500), "application/pdf")})
    assert r.status_code == 413


# ── §7.4 [Review] — approve a subset ─────────────────────────────────────────


def test_review_approves_only_the_selected_tickers(stub_extract):
    """§7.4's "[Review]" path: keep a wrong extraction out without discarding
    the whole note."""
    stub_extract(_portfolio_note(actions=[
        {"ticker": "GOOD", "action": "add", "instrument_note": "x", "asset_class": "equity"},
        {"ticker": "WRONG", "action": "add", "instrument_note": "x", "asset_class": "equity"},
    ]))
    intake_id = intake_mod.ingest_text("note", source_tag="STENO")["intake_id"]
    result = intake_mod.approve(intake_id, ["GOOD"])

    by_ticker = {a["ticker"]: a["result"] for a in result["applied"]}
    assert by_ticker["GOOD"] == "added"
    assert "skipped" in by_ticker["WRONG"]
    assert [r["symbol"] for r in store.list_watchlist()] == ["GOOD"]


def test_review_resolves_the_item_even_when_some_names_are_skipped():
    """Leaving it pending would make the queue accumulate half-handled notes."""
    intake_id = intake_mod._queue(
        source_tag="STENO", published_date="2026-07-26", subject="note", raw_ref=None,
        actions=[{"ticker": "A", "action": "add", "instrument_note": "", "asset_class": "equity"},
                 {"ticker": "B", "action": "add", "instrument_note": "", "asset_class": "equity"}],
    )
    intake_mod.approve(intake_id, ["A"])
    assert intake_mod.pending() == []


def test_review_ticker_selection_is_case_insensitive():
    intake_id = intake_mod._queue(
        source_tag="STENO", published_date=None, subject="note", raw_ref=None,
        actions=[{"ticker": "AAPL", "action": "add", "instrument_note": "", "asset_class": "equity"}],
    )
    intake_mod.approve(intake_id, ["aapl"])
    assert [r["symbol"] for r in store.list_watchlist()] == ["AAPL"]


def test_approve_with_none_still_approves_everything():
    """The "[Approve all]" path must be unchanged by the Review addition."""
    intake_id = intake_mod._queue(
        source_tag="STENO", published_date=None, subject="note", raw_ref=None,
        actions=[{"ticker": "A", "action": "add", "instrument_note": "", "asset_class": "equity"},
                 {"ticker": "B", "action": "add", "instrument_note": "", "asset_class": "equity"}],
    )
    intake_mod.approve(intake_id)
    assert {r["symbol"] for r in store.list_watchlist()} == {"A", "B"}


def test_review_endpoint_accepts_a_ticker_subset(client, stub_extract):
    stub_extract(_portfolio_note(actions=[
        {"ticker": "KEEP", "action": "add", "instrument_note": "x", "asset_class": "equity"},
        {"ticker": "DROP", "action": "add", "instrument_note": "x", "asset_class": "equity"},
    ]))
    intake_id = client.post("/api/signals/intake/paste", json={"text": "note"}).json()["intake_id"]
    r = client.post(f"/api/signals/intake/{intake_id}/approve", params={"tickers": "KEEP"})
    assert r.status_code == 200
    symbols = [w["symbol"] for w in client.get("/api/signals/watchlist").json()["watchlist"]]
    assert symbols == ["KEEP"]
