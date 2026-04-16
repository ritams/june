from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    fred_api_key: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    google_sheet_id: str | None
    google_sheet_tab: str
    google_service_account_file: str | None
    app_timezone: str
    display_timezone: str
    daily_card_time: str
    enable_scheduler: bool
    refresh_interval_minutes: int
    cache_ttl_seconds: int
    host: str
    port: int
    global_m2_proxy_series_id: str | None
    runtime_dir: Path

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def sheets_enabled(self) -> bool:
        return bool(self.google_sheet_id and self.google_service_account_file)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    fred_api_key = os.getenv("FRED_API_KEY", "").strip()
    if not fred_api_key:
        raise RuntimeError("FRED_API_KEY is required")

    runtime_dir = ROOT_DIR / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        fred_api_key=fred_api_key,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        google_sheet_id=os.getenv("GOOGLE_SHEET_ID") or None,
        google_sheet_tab=os.getenv("GOOGLE_SHEET_TAB", "Macro Dashboard"),
        google_service_account_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or None,
        app_timezone=os.getenv("APP_TIMEZONE", "UTC"),
        display_timezone=os.getenv("DISPLAY_TIMEZONE", "UTC"),
        daily_card_time=os.getenv("DAILY_CARD_TIME", "07:45"),
        enable_scheduler=_as_bool(os.getenv("ENABLE_SCHEDULER"), True),
        refresh_interval_minutes=int(os.getenv("REFRESH_INTERVAL_MINUTES", "15")),
        cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "900")),
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        global_m2_proxy_series_id=os.getenv("GLOBAL_M2_PROXY_SERIES_ID") or None,
        runtime_dir=runtime_dir,
    )
