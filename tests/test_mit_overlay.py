import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services import mit_overlay


def test_default_when_no_cache(tmp_path: Path):
    assert mit_overlay.current(tmp_path) == mit_overlay.DEFAULT_TEXT


def test_appends_as_of_when_present(tmp_path: Path):
    mit_overlay.save(tmp_path, {"summary": "Risk-on regime intact.", "as_of": "2026-06-01"})
    text = mit_overlay.current(tmp_path)
    assert "Risk-on regime intact." in text
    assert "2026-06-01" in text


def test_needs_refresh_when_no_cache(tmp_path: Path):
    assert mit_overlay.needs_refresh(tmp_path) is True


def test_needs_refresh_after_ttl(tmp_path: Path):
    # Cache from 8 days ago
    eight_days = time.time() - (8 * 24 * 3600)
    from datetime import datetime, timezone
    mit_overlay.save(tmp_path, {
        "summary": "stale", "as_of": "2026-05-01",
        "fetched_at": datetime.fromtimestamp(eight_days, tz=timezone.utc).isoformat(timespec="seconds"),
    })
    assert mit_overlay.needs_refresh(tmp_path) is True


def test_does_not_need_refresh_when_fresh(tmp_path: Path):
    from datetime import datetime, timezone
    mit_overlay.save(tmp_path, {
        "summary": "fresh", "as_of": "2026-06-01",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    assert mit_overlay.needs_refresh(tmp_path) is False


def test_refresh_skips_when_perplexity_disabled(tmp_path: Path):
    perplexity = MagicMock()
    perplexity.enabled = False
    result = mit_overlay.refresh(tmp_path, perplexity)
    assert result is None
    perplexity.latest_mit_overlay.assert_not_called()


def test_refresh_persists_and_returns_payload(tmp_path: Path):
    perplexity = MagicMock()
    perplexity.enabled = True
    perplexity.latest_mit_overlay.return_value = {
        "summary": "Cycle is mid-expansion.",
        "as_of": "2026-06-09",
        "season": "Summer",
        "citations": ["https://example.com"],
    }
    result = mit_overlay.refresh(tmp_path, perplexity, force=True)
    assert result is not None
    assert result["summary"] == "Cycle is mid-expansion."
    # Saved to disk
    cached = json.loads((tmp_path / "mit_overlay.json").read_text())
    assert cached["summary"] == "Cycle is mid-expansion."
    assert "fetched_at" in cached


def test_refresh_swallows_perplexity_failure(tmp_path: Path):
    perplexity = MagicMock()
    perplexity.enabled = True
    perplexity.latest_mit_overlay.side_effect = RuntimeError("API down")
    result = mit_overlay.refresh(tmp_path, perplexity, force=True)
    assert result is None
    # No cache written
    assert not (tmp_path / "mit_overlay.json").exists()
