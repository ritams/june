"""Steno service config — paths + Claude API settings.

Mirrors the steno-bot config but rooted at our runtime dir so cached auth state
and downloads live under runtime/steno/ alongside the rest of the dashboard state.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import ROOT_DIR


STENO_ROOT = ROOT_DIR / "runtime" / "steno"
STENO_AUTH_PATH = STENO_ROOT / "auth" / "rv_auth.json"
STENO_DOWNLOADS_DIR = STENO_ROOT / "downloads"
STENO_RENDERS_DIR = STENO_ROOT / "renders"
STENO_CACHE_DIR = STENO_ROOT / "cache"        # per-PDF extraction JSON cache
STENO_PORTFOLIO_PATH = STENO_ROOT / "model_portfolio.json"  # latest committed portfolio
STENO_PROCESSED_PATH = STENO_ROOT / "processed.json"        # which PDFs we've ingested

# Make sure paths exist on import — startup is the right time to create dirs.
for p in (STENO_AUTH_PATH.parent, STENO_DOWNLOADS_DIR, STENO_RENDERS_DIR, STENO_CACHE_DIR):
    p.mkdir(parents=True, exist_ok=True)

# Auth freshness — Azure B2C tokens generally last 24-48h
AUTH_MAX_AGE_DAYS = 2

# Claude API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_API_URL = os.getenv("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# Real Vision creds
RV_EMAIL = os.getenv("RV_EMAIL", "").strip()
RV_PASSWORD = os.getenv("RV_PASSWORD", "").strip()

# Render limits — Anthropic accepts up to 20 images per request but each
# 200-DPI page is ~1.5-2k vision tokens; 5/batch keeps us well under the
# 10k-image-token / 300s-timeout band on a 12-15 page Steno report.
MAX_PAGES_PER_PDF = 50
MAX_IMAGES_PER_REQUEST = 5
