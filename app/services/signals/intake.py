"""Technical Signals Layer — source intake (Steno / Real Vision names).

Implements `docs/technical-signals-layer-scope.md` §7, the email-based pipeline:

    1. Dedicated intake mailbox, Fred sets forwarding rules
    2. Inbound email service converts each email to a JSON POST at
       /api/intake/email
    3. Extraction: LLM call with a strict JSON schema
       {source, published_date, actions:[{ticker, action, instrument_note}]}
       Non-portfolio emails classified out and dropped silently
    4. Review queue — nothing auto-enters the watchlist
    5. Manual intake fallback — "Paste a note" box
    6. Weekly rhythm — Sunday 18:00 UK digest, only when pending > 0

═══════════════════════════════════════════════════════════════════════════════
NOTHING HERE AUTO-ENTERS THE WATCHLIST
═══════════════════════════════════════════════════════════════════════════════

§7.4 is emphatic: "nothing auto-enters the watchlist ... One tap approves ...
This keeps LLM extraction errors and non-actionable mentions out of the system,
and keeps the fundamental-selection decision where it belongs — with Fred."

So `ingest_*` only ever writes to `intake_events` with status='pending'. The
single function that touches the watchlist is `approve()`, and it is only ever
called from an authenticated dashboard action. An LLM hallucinating a ticker
therefore costs one dismissal, never a position.

Relatedly, §1: "It does not pick stocks." Intake surfaces *someone else's*
recommendation for Fred's approval; it never originates one.

═══════════════════════════════════════════════════════════════════════════════
WHY EMAIL AND NOT SCRAPING — AND ONE CONTRADICTION TO KNOW ABOUT
═══════════════════════════════════════════════════════════════════════════════

§3.2 rejects scraping Steno/RV: it would mean automating logins past bot
protection, would breach both platforms' terms, and would risk the
subscriptions themselves.

**However**, this repo already scrapes Real Vision for Steno PDFs end-to-end and
has done since before this scope doc — `app/services/steno/` (auth.py via
Playwright, downloader.py, renderer.py, transcriber.py) on a daily cron. So the
Steno half of §7 duplicates a working pipeline.

This module does not remove or replace that. It adds the email path, which is
the only route for Real Vision publications and the §7.5 paste fallback for
everything else (Cowan videos, etc.). If the existing scraper is kept for Steno,
the email path is really "RV + anything Fred pastes" — see the build log.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.services.signals import store
from app.services.signals.config import (
    INTAKE_MAX_PDF_BYTES,
    INTAKE_PDF_TEXT_MIN_CHARS,
    SIGNALS_INTAKE_DIR,
    SOURCE_SILENCE_DAYS,
    SOURCE_TAGS,
)
from app.services.signals.db import connect


logger = logging.getLogger(__name__)


# ── Inbound webhook auth (§10) ───────────────────────────────────────────────
#
# §10: "The only inbound endpoint is the email provider's signed POST (verify
# provider signature)". This is the one publicly-reachable, unauthenticated-by-
# session endpoint in the whole service, so it gets its own secret.
#
# Providers differ in scheme (Mailgun: HMAC-SHA256 over timestamp+token;
# Postmark: basic auth or a URL token; SendGrid: ECDSA), so we implement the
# common HMAC-SHA256-over-raw-body form plus a shared-token fallback, and
# document that the exact wiring depends on which provider is chosen.
INTAKE_WEBHOOK_SECRET = os.getenv("SIGNALS_INTAKE_WEBHOOK_SECRET", "").strip() or None

# Fail-closed by default. An inbound endpoint that accepts unsigned POSTs is an
# open door for anyone who finds the URL to inject tickers into Fred's review
# queue. Set SIGNALS_INTAKE_ALLOW_UNSIGNED=true only for local development.
INTAKE_ALLOW_UNSIGNED = os.getenv("SIGNALS_INTAKE_ALLOW_UNSIGNED", "").strip().lower() in {"1", "true", "yes", "on"}


class IntakeAuthError(PermissionError):
    """Signature verification failed, or no secret is configured."""


def verify_signature(raw_body: bytes, signature: str | None, timestamp: str | None = None) -> None:
    """Verify an inbound email provider's signature — §10. Raises IntakeAuthError.

    Uses `hmac.compare_digest` rather than `==` so the comparison is
    constant-time; a plain equality check leaks the correct prefix through
    timing and would let an attacker forge a signature byte by byte.
    """
    if INTAKE_WEBHOOK_SECRET is None:
        if INTAKE_ALLOW_UNSIGNED:
            logger.warning(
                "Intake webhook accepted an UNSIGNED request — "
                "SIGNALS_INTAKE_ALLOW_UNSIGNED is on. Never do this in production."
            )
            return
        raise IntakeAuthError(
            "SIGNALS_INTAKE_WEBHOOK_SECRET is not configured. Refusing to accept inbound email "
            "(set the secret, or SIGNALS_INTAKE_ALLOW_UNSIGNED=true for local development only)."
        )

    if not signature:
        raise IntakeAuthError("Missing signature header")

    # Include the timestamp in the signed payload when the provider sends one —
    # signing the body alone permits unlimited replay of a captured request.
    signed_payload = (timestamp.encode() + b"." + raw_body) if timestamp else raw_body
    expected = hmac.new(INTAKE_WEBHOOK_SECRET.encode(), signed_payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise IntakeAuthError("Signature mismatch")


# ── §7.3 extraction ──────────────────────────────────────────────────────────

# The strict schema §7.3 specifies, as an Anthropic tool definition. Same
# tool_use approach as app/services/steno/portfolio_extractor.py — forcing a tool
# call means the model cannot return prose we then have to parse.
INTAKE_TOOL: dict[str, Any] = {
    "name": "extract_research_actions",
    "description": (
        "Extract portfolio actions from a research note. Only extract tickers the note "
        "explicitly recommends acting on. If the note is not portfolio-actionable "
        "(event invite, marketing, pure macro commentary with no named positions), set "
        "is_portfolio_note to false and return an empty actions array."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            # §7.3: "Non-portfolio emails (event invites, marketing) are
            # classified out and dropped silently." The model makes that call
            # here rather than us regex-guessing from the subject line.
            "is_portfolio_note": {
                "type": "boolean",
                "description": (
                    "True only if this note recommends specific, named positions to add, trim, "
                    "exit or reiterate. False for event invitations, marketing, subscription "
                    "notices, or macro commentary that names no positions."
                ),
            },
            "source": {
                "type": "string",
                "enum": list(SOURCE_TAGS),
                "description": "Which research source this note came from.",
            },
            "published_date": {
                "type": ["string", "null"],
                "description": "Publication date as YYYY-MM-DD if stated in the note; null otherwise.",
            },
            "note_summary": {
                "type": "string",
                "description": "One short line describing the note, max 15 words. Shown in the review queue.",
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {
                            "type": "string",
                            "description": (
                                "The exchange ticker, uppercase. Use the US-listed ticker where one exists. "
                                "If the note names a theme without a ticker, omit the action entirely rather "
                                "than guessing a proxy."
                            ),
                        },
                        "action": {
                            "type": "string",
                            "enum": ["add", "trim", "exit", "reiterate"],
                        },
                        "instrument_note": {
                            "type": "string",
                            "description": "The source's own rationale, max 25 words. Quote or paraphrase; do not invent.",
                        },
                        "asset_class": {
                            "type": "string",
                            "enum": ["equity", "etf", "commodity", "crypto", "fx", "index", "other"],
                        },
                    },
                    "required": ["ticker", "action", "instrument_note"],
                },
            },
        },
        "required": ["is_portfolio_note", "source", "actions"],
    },
}

_SYSTEM_PROMPT = (
    "You are extracting portfolio actions from an investment research note for a review queue. "
    "A human approves every extracted name before it enters any watchlist, so a missed name is "
    "cheap and an invented one is expensive.\n\n"
    "Rules:\n"
    "- Extract ONLY tickers the note explicitly recommends acting on. Names mentioned as "
    "context, comparison, or market colour are NOT actions.\n"
    "- Never guess a ticker. If the note says 'shipping stocks' or 'a drone defence ETF' with no "
    "specific symbol, omit it — do not substitute a proxy you think fits.\n"
    "- Never infer an action the note does not state. 'We are watching X' is not an add.\n"
    "- If the note is an event invite, marketing, or macro commentary with no named positions, "
    "set is_portfolio_note=false and return an empty actions array."
)


@dataclass
class Extraction:
    """Result of §7.3 extraction."""

    is_portfolio_note: bool
    source: str
    published_date: str | None
    note_summary: str
    actions: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_portfolio_note": self.is_portfolio_note,
            "source": self.source,
            "published_date": self.published_date,
            "note_summary": self.note_summary,
            "actions": self.actions,
        }


def extract(text: str, *, source_hint: str | None = None) -> Extraction:
    """Run §7.3 extraction over note text.

    Reuses the config and calling convention already proven in
    app/services/steno/portfolio_extractor.py rather than introducing a second
    Anthropic client.

    Raises RuntimeError when the API key is missing or the model fails to return
    a tool_use block. Callers surface that as a 5xx — an extraction failure must
    not silently produce an empty queue entry, which would look like "the note
    contained nothing".
    """
    from app.services.steno.config import (
        ANTHROPIC_API_KEY,
        ANTHROPIC_API_URL,
        ANTHROPIC_VERSION,
        CLAUDE_MODEL,
    )

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY missing in environment")

    hint = f"\n\nThis note arrived from: {source_hint}" if source_hint else ""
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2048,
        "system": _SYSTEM_PROMPT,
        "tools": [INTAKE_TOOL],
        "tool_choice": {"type": "tool", "name": "extract_research_actions"},
        # Truncated to keep a pathological forwarded thread from blowing the
        # context window; 120k chars matches the Steno extractor's limit.
        "messages": [{"role": "user", "content": f"Research note:{hint}\n\n{text[:120000]}"}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    with httpx.Client(timeout=120.0) as client:
        response = client.post(ANTHROPIC_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()

    result: dict[str, Any] | None = None
    for block in body.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "extract_research_actions":
            result = block["input"]
            break
    if result is None:
        raise RuntimeError("Claude did not return a tool_use block — intake extraction failed")

    return _validate(result, source_hint=source_hint)


# Defensive ticker shape check. Deliberately permissive — it must accept
# BRK.B, BTC-USD, GC=F and ^GSPC — but it rejects prose, which is the failure
# mode that matters (an LLM returning "shipping stocks" as a ticker).
#
# The leading `\^?` is load-bearing: index symbols start with a caret, and
# requiring an alphanumeric first character silently dropped every index.
# The absence of whitespace in the character class is what rejects prose.
_TICKER_RE = re.compile(r"^\^?[A-Z0-9][A-Z0-9.\-=]{0,14}$")

_VALID_ACTIONS = {"add", "trim", "exit", "reiterate"}


def _validate(raw: dict[str, Any], *, source_hint: str | None = None) -> Extraction:
    """Sanity-check the model's output before it reaches the queue.

    Defence in depth behind the schema. The schema constrains types; this
    constrains *values*, dropping anything malformed rather than surfacing it to
    Fred as a plausible-looking suggestion. Every drop is logged so a systematic
    extraction problem is visible rather than silently thinning the queue.
    """
    source = str(raw.get("source") or source_hint or "OWN").strip().upper()
    if source not in SOURCE_TAGS:
        logger.warning("Extraction returned unknown source %r; filing as OWN", source)
        source = "OWN"

    actions: list[dict[str, Any]] = []
    for item in raw.get("actions") or []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        action = str(item.get("action") or "").strip().lower()
        if not _TICKER_RE.match(ticker):
            logger.info("Dropping malformed ticker from extraction: %r", ticker)
            continue
        if action not in _VALID_ACTIONS:
            logger.info("Dropping unknown action %r for %s", action, ticker)
            continue
        actions.append({
            "ticker": ticker,
            "action": action,
            "instrument_note": str(item.get("instrument_note") or "")[:300],
            "asset_class": str(item.get("asset_class") or "equity").strip().lower(),
        })

    published = raw.get("published_date")
    if published:
        try:
            date.fromisoformat(str(published)[:10])
            published = str(published)[:10]
        except ValueError:
            logger.info("Dropping unparseable published_date %r", published)
            published = None

    return Extraction(
        is_portfolio_note=bool(raw.get("is_portfolio_note")),
        source=source,
        published_date=published,
        note_summary=str(raw.get("note_summary") or "")[:200],
        actions=actions,
    )


# ── §7.2 PDF extraction ──────────────────────────────────────────────────────


class PdfTooLarge(ValueError):
    """Attachment exceeds INTAKE_MAX_PDF_BYTES."""


def extract_pdf_text(data: bytes, *, name: str = "note.pdf") -> tuple[str, str]:
    """PDF bytes → text. Returns (text, method) where method is 'text_layer'|'vision'.

    §7.2: "Attached/linked PDFs are pulled in where the mail includes them."

    Two-stage, cheapest first:

      1. **Text layer** via PyMuPDF. Nearly all research notes are generated from
         a word processor and carry real text. This is instant and free.
      2. **Claude Vision** via the existing Steno renderer + transcriber, only
         when the text layer is too thin to be usable (scanned pages, or a note
         that is all charts). Slow and billed, so it is the fallback rather than
         the default — the Steno pipeline uses Vision unconditionally because
         Steno PDFs *are* chart-heavy, which is not true of email notes generally.

    The file is written to SIGNALS_INTAKE_DIR before parsing so a failure can be
    inspected rather than lost.
    """
    if len(data) > INTAKE_MAX_PDF_BYTES:
        raise PdfTooLarge(
            f"PDF is {len(data) // (1024 * 1024)}MB, over the "
            f"{INTAKE_MAX_PDF_BYTES // (1024 * 1024)}MB intake limit"
        )
    if not data[:5].startswith(b"%PDF"):
        raise ValueError("Attachment does not look like a PDF (missing %PDF header)")

    # Content-addressed filename: the same note arriving twice reuses the same
    # path instead of accumulating copies, and the digest is a natural dedup key.
    digest = hashlib.sha256(data).hexdigest()[:16]
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:60]
    path = SIGNALS_INTAKE_DIR / f"{digest}-{safe_name}"
    if not path.exists():
        path.write_bytes(data)

    try:
        import fitz  # PyMuPDF, lazy-imported to match app/services/steno/renderer.py
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is not installed — cannot extract PDF text") from exc

    doc = fitz.open(path)
    try:
        text = "\n\n".join(page.get_text() for page in doc)
    finally:
        doc.close()

    if len(text.strip()) >= INTAKE_PDF_TEXT_MIN_CHARS:
        logger.info("Intake PDF %s: extracted %d chars from the text layer", safe_name, len(text))
        return text, "text_layer"

    # No usable text layer — fall back to Vision through the Steno pipeline.
    logger.info("Intake PDF %s: thin text layer (%d chars), falling back to Vision",
                safe_name, len(text.strip()))
    from app.services.steno import renderer, transcriber

    images, truncated = renderer.render_pdf_pages(path)
    if truncated:
        logger.warning("Intake PDF %s exceeded the page cap and was truncated", safe_name)
    vision_text = transcriber.transcribe_pages(images, pdf_stem=path.stem)
    return vision_text, "vision"


def _attachments_from_payload(payload: dict[str, Any]) -> list[tuple[str, bytes]]:
    """Decode base64 PDF attachments from a provider payload — §7.2.

    Supports the Postmark/SendGrid shape (`Attachments`/`attachments` with a
    base64 `Content`). Non-PDF attachments are ignored.

    **Linked (URL) PDFs are deliberately not fetched.** §7.2 says "attached or
    linked", but this endpoint accepts data from outside the network, and
    following an arbitrary URL from an inbound payload is a server-side request
    forgery vector — a forged email could point us at internal addresses. The
    §7.5 upload box covers linked PDFs with a human in the loop, which is the
    same trade §7.4 already makes for extraction errors.
    """
    raw = payload.get("Attachments") or payload.get("attachments") or []
    if not isinstance(raw, list):
        return []

    out: list[tuple[str, bytes]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or item.get("filename") or item.get("name") or "note.pdf")
        content_type = str(item.get("ContentType") or item.get("type") or "")
        if "pdf" not in content_type.lower() and not name.lower().endswith(".pdf"):
            continue
        content = item.get("Content") or item.get("content")
        if not content:
            continue
        try:
            out.append((name, base64.b64decode(content)))
        except Exception:
            logger.warning("Intake: could not base64-decode attachment %r", name)
    return out


# ── §7.2 / §7.5 ingestion ────────────────────────────────────────────────────


def ingest_text(
    text: str,
    *,
    source_tag: str | None = None,
    subject: str | None = None,
    raw_ref: str | None = None,
) -> dict[str, Any]:
    """Extract from note text and queue anything actionable — §7.3, §7.4, §7.5.

    Returns a summary of what happened. Three distinct outcomes, kept distinct
    because they need different responses from the operator:

      * `queued`   — actionable names are pending review
      * `dropped`  — classified as a non-portfolio note (§7.3 "dropped silently")
      * `empty`    — a portfolio note whose named actions all failed validation,
                     which is worth surfacing because it may mean the extraction
                     prompt needs work, unlike a marketing email

    Records the note against its source's `last_seen_at` regardless of outcome —
    §7's 21-day silence notice is about whether *email is arriving*, not whether
    it contained names. A month of Steno event invites means the forwarding rule
    works.
    """
    if not text or not text.strip():
        raise ValueError("Cannot extract from empty note text")

    extraction = extract(text, source_hint=source_tag)
    touch_source(extraction.source)

    if not extraction.is_portfolio_note:
        # §7.3: "classified out and dropped silently".
        logger.info("Intake: non-portfolio note from %s dropped (%s)", extraction.source, subject or "no subject")
        return {"status": "dropped", "reason": "not a portfolio note", "source": extraction.source}

    if not extraction.actions:
        logger.info("Intake: portfolio note from %s produced no valid actions", extraction.source)
        return {"status": "empty", "reason": "no valid actions extracted", "source": extraction.source}

    event_id = _queue(
        source_tag=extraction.source,
        published_date=extraction.published_date,
        subject=subject or extraction.note_summary,
        raw_ref=raw_ref,
        actions=extraction.actions,
    )
    return {
        "status": "queued",
        "intake_id": event_id,
        "source": extraction.source,
        "actions": extraction.actions,
    }


def ingest_email(payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest an inbound-email provider's JSON POST — §7.2.

    Provider payload shapes differ (Postmark uses TextBody/Subject, Mailgun uses
    body-plain/subject, SendGrid uses text/subject), so the field extraction
    below accepts all three rather than pinning to one provider before it is
    chosen. Whichever is picked, only this function changes.

    §7.2: "Attached/linked PDFs are pulled in where the mail includes them."
    Attached PDFs are decoded and their text appended to the body before
    extraction, so a note whose substance is in the attachment is handled.
    *Linked* PDFs are deliberately not fetched — see _attachments_from_payload.
    """
    text = (
        payload.get("TextBody")           # Postmark
        or payload.get("body-plain")      # Mailgun
        or payload.get("text")            # SendGrid
        or payload.get("StrippedTextReply")
        or ""
    )
    subject = payload.get("Subject") or payload.get("subject") or None
    sender = payload.get("From") or payload.get("from") or payload.get("sender") or ""
    message_id = payload.get("MessageID") or payload.get("Message-Id") or payload.get("message-id")

    # §7.2: pull in attached PDFs. Appended to the body rather than replacing it —
    # the covering email often carries the framing ("this week's portfolio note")
    # that helps the extractor classify a non-portfolio attachment correctly.
    pdf_notes: list[str] = []
    pdf_errors: list[str] = []
    for name, data in _attachments_from_payload(payload):
        try:
            pdf_text, method = extract_pdf_text(data, name=name)
            pdf_notes.append(f"\n\n--- attachment: {name} ({method}) ---\n{pdf_text}")
        except (PdfTooLarge, ValueError) as exc:
            # A bad attachment must not sink an email that also has a usable body.
            logger.warning("Intake: skipping attachment %r — %s", name, exc)
            pdf_errors.append(f"{name}: {exc}")
        except Exception as exc:
            logger.exception("Intake: attachment %r failed to extract", name)
            pdf_errors.append(f"{name}: {type(exc).__name__}")

    combined = (text + "".join(pdf_notes)).strip()

    if not combined:
        reason = (
            f"attachments present but none could be extracted ({'; '.join(pdf_errors)})"
            if pdf_errors else "empty email body"
        )
        logger.info("Intake email from %s produced no text: %s", sender, reason)
        return {"status": "skipped", "reason": reason}

    result = ingest_text(
        combined,
        source_tag=_source_from_sender(sender),
        subject=subject,
        raw_ref=message_id,
    )
    if pdf_notes:
        result["attachments_extracted"] = len(pdf_notes)
    if pdf_errors:
        # Surfaced rather than swallowed: a systematically failing attachment
        # type would otherwise look like a source that stopped recommending names.
        result["attachment_errors"] = pdf_errors
    return result


# Sender-domain → source tag. A hint only: §7.3's extraction sets the
# authoritative source, and _validate falls back to OWN for anything unmapped.
_SENDER_HINTS = [
    ("stenoresearch", "STENO"),
    ("steno-research", "STENO"),
    ("realvision", "RV"),
    ("real-vision", "RV"),
    ("cowan", "COWAN"),
]


def _source_from_sender(sender: str) -> str | None:
    low = (sender or "").lower()
    for needle, tag in _SENDER_HINTS:
        if needle in low:
            return tag
    return None


def _queue(
    *,
    source_tag: str,
    published_date: str | None,
    subject: str | None,
    raw_ref: str | None,
    actions: list[dict[str, Any]],
) -> int:
    """Insert a pending review-queue row — §7.4. Never touches the watchlist."""
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO intake_events
                (source_tag, published_date, subject, raw_ref, actions_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                source_tag,
                published_date,
                subject,
                raw_ref,
                json.dumps(actions),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return int(cur.lastrowid)


# ── §7.4 review queue ────────────────────────────────────────────────────────


def pending() -> list[dict[str, Any]]:
    """Pending queue items for the §9 intake strip, newest first.

    Each item's actions are annotated with whether the ticker is already on the
    watchlist, so the strip can show an exit/trim on a held name as a **removal
    suggestion** (§7: "When a source note says exit/trim a name currently on the
    watchlist, the queue shows a removal suggestion — again one-tap, never
    automatic").
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_events WHERE status = 'pending' ORDER BY id DESC"
        ).fetchall()

    on_list = {w["symbol"] for w in store.list_watchlist()}
    out = []
    for row in rows:
        item = dict(row)
        actions = json.loads(item.pop("actions_json") or "[]")
        for action in actions:
            action["on_watchlist"] = action["ticker"] in on_list
            action["is_removal_suggestion"] = (
                action["action"] in ("exit", "trim") and action["ticker"] in on_list
            )
        item["actions"] = actions
        out.append(item)
    return out


def approve(intake_id: int, tickers: list[str] | None = None) -> dict[str, Any]:
    """Apply a queue item's actions — §7.4's "One tap approves" and "Review".

    `tickers=None` approves everything — the "[Approve all]" path.
    `tickers=[...]` approves only those — the "[Review]" path, where Fred has
    inspected the extracted names and wants a subset. Names not listed are
    recorded as skipped and the item is still resolved, because the review *has*
    happened; leaving it pending would make the queue accumulate half-handled
    notes.

    The **only** function in this module that writes to the watchlist, and it is
    reachable only from an authenticated dashboard POST.

    Action semantics:
      * `add` / `reiterate` → add to the watchlist, tagged with the source
      * `exit`              → remove from the watchlist (the §7 removal suggestion)
      * `trim`             → **no watchlist change**

    `trim` deliberately does nothing structural: a source trimming a position
    still holds it, so the name stays on the watchlist and stays scored. Treating
    a trim as a removal would stop tracking a name its author still owns — and
    exit timing on a partially-held name is exactly what §5's exit score is for.
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM intake_events WHERE id = ?", (intake_id,)).fetchone()
        if row is None:
            raise ValueError(f"No intake event {intake_id}")
        if row["status"] != "pending":
            raise ValueError(f"Intake event {intake_id} is already {row['status']}")
        actions = json.loads(row["actions_json"] or "[]")
        source_tag = row["source_tag"]

    selected = {t.strip().upper() for t in tickers} if tickers is not None else None

    applied: list[dict[str, Any]] = []
    for action in actions:
        ticker = action["ticker"]
        verb = action["action"]
        if selected is not None and ticker not in selected:
            applied.append({"ticker": ticker, "result": "skipped (not selected in review)"})
            continue
        try:
            if verb in ("add", "reiterate"):
                store.add_to_watchlist(
                    ticker,
                    asset_class=action.get("asset_class") or "equity",
                    source_tag=source_tag,
                    notes=action.get("instrument_note"),
                )
                applied.append({"ticker": ticker, "result": "added"})
            elif verb == "exit":
                removed = store.remove_from_watchlist(ticker)
                applied.append({"ticker": ticker, "result": "removed" if removed else "not on watchlist"})
            else:  # trim
                applied.append({"ticker": ticker, "result": "noted (trim — kept on watchlist)"})
        except ValueError as exc:
            # One bad ticker must not block the rest of the note's names.
            logger.warning("Approve: skipping %s — %s", ticker, exc)
            applied.append({"ticker": ticker, "result": f"skipped: {exc}"})

    _resolve(intake_id, "approved")
    return {"intake_id": intake_id, "applied": applied}


def dismiss(intake_id: int) -> dict[str, Any]:
    """Discard a queue item without touching the watchlist — §7.4."""
    with connect() as conn:
        row = conn.execute("SELECT status FROM intake_events WHERE id = ?", (intake_id,)).fetchone()
        if row is None:
            raise ValueError(f"No intake event {intake_id}")
        if row["status"] != "pending":
            raise ValueError(f"Intake event {intake_id} is already {row['status']}")
    _resolve(intake_id, "dismissed")
    return {"intake_id": intake_id, "status": "dismissed"}


def _resolve(intake_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE intake_events SET status = ?, resolved_at = ? WHERE id = ?",
            (status, datetime.now(timezone.utc).isoformat(), intake_id),
        )


# ── §7 source silence ────────────────────────────────────────────────────────


def touch_source(source_tag: str) -> None:
    """Record that a note arrived from this source — drives the silence notice."""
    with connect() as conn:
        conn.execute(
            "UPDATE sources SET last_seen_at = ? WHERE tag = ?",
            (datetime.now(timezone.utc).isoformat(), source_tag.strip().upper()),
        )


def silent_sources(*, as_of: date | None = None) -> list[dict[str, Any]]:
    """Sources that have sent nothing for SOURCE_SILENCE_DAYS — §7.

    §7: "if no email arrives from a source for 21 days, the dashboard shows a
    quiet 'no Steno note received in 3 weeks' notice — catches a broken
    forwarding rule without nagging."

    Only reports sources that have been seen *at least once*. A source with
    `last_seen_at IS NULL` has never been configured, and warning that a
    never-set-up forwarding rule is "silent" would fire on day one for every
    source and train Fred to ignore the notice.

    OWN is excluded — it is Fred's own ideas, not an inbound feed.
    """
    today = as_of or datetime.now(timezone.utc).date()
    with connect() as conn:
        rows = conn.execute(
            "SELECT tag, label, last_seen_at FROM sources WHERE last_seen_at IS NOT NULL AND tag != 'OWN'"
        ).fetchall()

    out = []
    for row in rows:
        try:
            last = datetime.fromisoformat(row["last_seen_at"]).date()
        except (TypeError, ValueError):
            continue
        days = (today - last).days
        if days >= SOURCE_SILENCE_DAYS:
            out.append({
                "tag": row["tag"],
                "label": row["label"],
                "days_silent": days,
                "last_seen": last.isoformat(),
            })
    return out


# ── §7.6 weekly intake digest ────────────────────────────────────────────────


def build_intake_digest() -> str | None:
    """The §7.6 Sunday digest line, or None when there is nothing pending.

    §7.6: "Sunday 18:00 UK digest line via Hermes: 'Intake this week: 2 notes
    parsed, 3 names pending approval' (only if pending > 0 — silence otherwise)."

    The "only if pending > 0" is the whole point — returning None is the normal
    Sunday outcome.
    """
    items = pending()
    if not items:
        return None

    name_count = sum(len(item["actions"]) for item in items)
    lines = [
        f"📥 Intake this week: {len(items)} note{'s' if len(items) != 1 else ''} parsed, "
        f"{name_count} name{'s' if name_count != 1 else ''} pending approval"
    ]
    for item in items[:5]:
        actions = ", ".join(
            f"{'+' if a['action'] in ('add', 'reiterate') else '−' if a['action'] == 'exit' else '~'}{a['ticker']}"
            for a in item["actions"]
        )
        lines.append(f"  {item['source_tag']} · {item.get('published_date') or '—'} — {actions}")

    silent = silent_sources()
    if silent:
        lines.append("")
        for s in silent:
            lines.append(f"  ⚠ No {s['label']} note in {s['days_silent'] // 7} weeks")
    return "\n".join(lines)


def send_intake_digest(*, telegram_client: Any | None = None) -> dict[str, Any]:
    """Send the §7.6 digest if anything is pending. Silence otherwise."""
    from app.services.signals import alerts as alerts_mod

    message = build_intake_digest()
    if message is None:
        logger.info("Intake digest: nothing pending, staying silent")
        return {"sent": False, "reason": "nothing pending"}

    digest = alerts_mod.Alert(
        alert_type="intake", ticker=None, direction="buy", score=0, band="intake", realtime=True
    )
    alert_id = store.log_alert(None, "intake", {"message": message})
    ok, error, attempts = alerts_mod._deliver_text(message, digest, telegram_client=telegram_client)
    store.mark_alert_delivered(alert_id, delivered=ok, attempts=attempts, error=error)
    return {"sent": ok, "error": error, "message": message}
