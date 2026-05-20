"""Claude Vision transcription — batches 20 page images per request.

Returns the concatenated text of all pages. Cached per-PDF in
runtime/steno/cache/<pdf-stem>-transcript.txt so we don't re-pay for
identical inputs across reloads.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from app.services.steno.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_API_URL,
    ANTHROPIC_VERSION,
    CLAUDE_MODEL,
    MAX_IMAGES_PER_REQUEST,
    STENO_CACHE_DIR,
)


logger = logging.getLogger(__name__)

TRANSCRIBE_PROMPT = (
    "Transcribe all text content from these Steno Signals PDF pages accurately. "
    "Preserve structure: headings, bullet points, tables, paragraphs. For any portfolio "
    "or position tables, transcribe the table exactly with each ticker, weight, "
    "and commentary. Do not summarize — transcribe what you see."
)


def _encode_image(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _transcribe_batch(image_paths: list[Path]) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY missing in environment")
    content: list[dict] = [_encode_image(p) for p in image_paths]
    content.append({"type": "text", "text": TRANSCRIBE_PROMPT})
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    with httpx.Client(timeout=300.0) as client:
        resp = client.post(ANTHROPIC_API_URL, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Claude API {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
    usage = body.get("usage", {})
    logger.info("Transcribed %d images (in=%s, out=%s)", len(image_paths), usage.get("input_tokens"), usage.get("output_tokens"))
    return body["content"][0]["text"]


def transcribe_pages(image_paths: list[Path], pdf_stem: str | None = None) -> str:
    """Batch images by 20, concatenate transcripts. Cached per pdf_stem if given."""
    cache_path = STENO_CACHE_DIR / f"{pdf_stem}-transcript.txt" if pdf_stem else None
    if cache_path and cache_path.exists():
        logger.info("Using cached transcript for %s", pdf_stem)
        return cache_path.read_text()

    batches = [image_paths[i : i + MAX_IMAGES_PER_REQUEST] for i in range(0, len(image_paths), MAX_IMAGES_PER_REQUEST)]
    parts: list[str] = []
    for idx, batch in enumerate(batches):
        logger.info("Transcribing batch %d/%d (%d pages)", idx + 1, len(batches), len(batch))
        parts.append(_transcribe_batch(batch))
    full = "\n\n---\n\n".join(parts)
    if cache_path:
        cache_path.write_text(full)
    return full
