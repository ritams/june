from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class Observation:
    date: str
    value: float


class FredClient:
    base_url = "https://api.stlouisfed.org/fred"
    max_retries = 3
    backoff_seconds = 0.8

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        # FRED occasionally 500s on long pulls — retry transient 5xx with backoff.
        payload = {"api_key": self.api_key, "file_type": "json", **params}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with httpx.Client(timeout=20.0) as client:
                    response = client.get(f"{self.base_url}/{path}", params=payload)
                if response.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"FRED {response.status_code}", request=response.request, response=response
                    )
                    time.sleep(self.backoff_seconds * (attempt + 1))
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.RequestError as exc:
                last_exc = exc
                time.sleep(self.backoff_seconds * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("FRED request failed without exception")

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
