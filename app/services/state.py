from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        if not self.path.exists():
            self.path.write_text(json.dumps({"alerts": {}, "daily_card_date": ""}, indent=2))

    def load(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(self.path.read_text())

    def save(self, state: dict[str, Any]) -> None:
        with self.lock:
            self.path.write_text(json.dumps(state, indent=2, sort_keys=True))
