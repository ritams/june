"""Real Vision auth — Playwright login that captures Azure B2C MSAL tokens.

Mirrors the steno-bot pattern. We need storage_state (cookies + localStorage)
not just cookies, because Real Vision uses MSAL which keeps tokens in
localStorage. Same auth state lives at runtime/steno/auth/rv_auth.json and
gets refreshed at most every AUTH_MAX_AGE_DAYS.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from app.services.steno.config import (
    AUTH_MAX_AGE_DAYS,
    RV_EMAIL,
    RV_PASSWORD,
    STENO_AUTH_PATH,
)


logger = logging.getLogger(__name__)
HOME_URL = "https://app.realvision.com/"


def _has_msal_tokens(state_path: Path) -> bool:
    """A saved state with no origin tokens means we lost MSAL — treat as stale."""
    try:
        data = json.loads(state_path.read_text())
        return bool(data.get("origins"))
    except Exception as exc:
        logger.warning("Failed to read auth state: %s", exc)
        return False


def auth_state_is_fresh() -> bool:
    if not STENO_AUTH_PATH.exists():
        return False
    if not _has_msal_tokens(STENO_AUTH_PATH):
        return False
    age_seconds = time.time() - STENO_AUTH_PATH.stat().st_mtime
    return age_seconds < AUTH_MAX_AGE_DAYS * 86400


def login_and_save(*, headless: bool = False) -> None:
    """Open Real Vision, fill the Azure B2C form, save the full storage_state."""
    from playwright.sync_api import sync_playwright  # noqa: WPS433 — lazy for tests

    STENO_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    automated = bool(RV_EMAIL and RV_PASSWORD)
    logger.info("Steno login starting (automated=%s, headless=%s)", automated, headless)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.goto(HOME_URL)

        if automated:
            page.wait_for_selector('input[type="email"], input[name="loginfmt"]', timeout=30_000)
            page.fill('input[type="email"], input[name="loginfmt"]', RV_EMAIL)
            page.keyboard.press("Enter")
            page.wait_for_selector('input[type="password"]', timeout=15_000)
            page.fill('input[type="password"]', RV_PASSWORD)
            page.keyboard.press("Enter")
        else:
            logger.info("No RV creds in env — opening browser for manual login")

        page.wait_for_url(
            lambda url: "app.realvision.com" in url and "b2clogin.com" not in url,
            timeout=180_000,
        )
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
        context.storage_state(path=str(STENO_AUTH_PATH))
        browser.close()

    logger.info("Steno auth saved → %s", STENO_AUTH_PATH)


def ensure_authenticated(force: bool = False, headless: bool = False) -> Path:
    """Return a valid auth state path, refreshing only if stale or forced."""
    if not force and auth_state_is_fresh():
        return STENO_AUTH_PATH
    login_and_save(headless=headless)
    return STENO_AUTH_PATH
