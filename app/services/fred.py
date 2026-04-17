from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class Observation:
    date: str
    value: float


class FredClient:
    base_url = "https://api.stlouisfed.org/fred"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {"api_key": self.api_key, "file_type": "json", **params}
        with httpx.Client(timeout=20.0) as client:
            response = client.get(f"{self.base_url}/{path}", params=payload)
            response.raise_for_status()
        return response.json()

    def observations(
        self,
        series_id: str,
        limit: int | None = 13,
        sort_order: str = "desc",
        observation_start: str | None = None,
    ) -> list[Observation]:
        params: dict[str, Any] = {
            "series_id": series_id,
            "sort_order": sort_order,
        }
        if limit is not None:
            params["limit"] = limit
        if observation_start is not None:
            params["observation_start"] = observation_start
        data = self._get("series/observations", params)
        items: list[Observation] = []
        for item in data.get("observations", []):
            if item["value"] == ".":
                continue
            items.append(Observation(date=item["date"], value=float(item["value"])))
        return items
