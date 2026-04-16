from __future__ import annotations

import httpx


class TelegramClient:
    def __init__(self, token: str | None, chat_id: str | None) -> None:
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_message(self, message: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        response = httpx.post(
            url,
            json={"chat_id": self.chat_id, "text": message},
            timeout=20.0,
        )
        response.raise_for_status()
        return True
