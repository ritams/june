from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class PerplexityReading:
    current: float
    previous: float | None
    period_month: str | None
    period_year: int | None
    release_date: str | None
    summary: str | None
    citations: list[str] = field(default_factory=list)

    @property
    def period_label(self) -> str | None:
        if self.period_month and self.period_year:
            return f"{self.period_month} {self.period_year}"
        if self.period_month:
            return self.period_month
        if self.period_year:
            return str(self.period_year)
        return None


class PerplexityClient:
    base_url = "https://api.perplexity.ai/chat/completions"

    def __init__(self, api_key: str | None, model: str) -> None:
        self.api_key = api_key
        self.model = model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def latest_ism_manufacturing_pmi(self) -> PerplexityReading:
        payload = self._ask_json(
            """
Return only valid JSON for the latest ISM Manufacturing PMI release.
Use this schema exactly:
{
  "current": number,
  "previous": number | null,
  "period_month": string | null,
  "period_year": integer | null,
  "release_date": string | null,
  "summary": string | null
}
If a field is unknown, use null. Do not include markdown.
            """.strip()
        )
        return self._reading_from_payload(payload)

    def latest_mit_overlay(self) -> dict[str, Any]:
        """Fetch a fresh MIT (Macro Investing Tool) view from Julien Bittel via
        Perplexity. The MIT report is published monthly on Real Vision / GMI.

        Returns:
          {
            "summary": "1-2 sentence current MIT view, plain English",
            "as_of": "ISO date of the most recent report Perplexity found",
            "citations": [...]
          }
        """
        payload = self._ask_json(
            """
You are extracting Julien Bittel's most recent Macro Investing Tool (MIT) view.
Bittel publishes the MIT report monthly on Real Vision via Global Macro Investor.
He covers the business cycle, growth + inflation dynamics, the 4 seasons
(Spring/Summer/Autumn/Winter), and liquidity. Search recent (last 60 days)
sources for his most recent published MIT note, podcast, or video summary.

Return strictly this JSON:
{
  "summary": string,            // 1-2 sentence summary of Bittel's CURRENT MIT view
                                // in his own framing (cycle position + liquidity).
                                // Max 240 characters. No preamble, no "according to".
  "as_of": string | null,       // ISO date (YYYY-MM-DD) of the most recent source.
  "season": string | null       // One of: "Spring", "Summer", "Autumn", "Winter", null
}
If no recent source is found, set summary to "No recent MIT update located." and as_of to null.
            """.strip()
        )
        return {
            "summary": str(payload.get("summary") or "")[:300],
            "as_of": payload.get("as_of"),
            "season": payload.get("season"),
            "citations": [str(c) for c in payload.get("citations", []) if c],
        }

    def latest_korean_exports(self) -> PerplexityReading:
        payload = self._ask_json(
            """
Return only valid JSON for the latest South Korea exports year-over-year release.
Use this schema exactly:
{
  "current": number,
  "previous": number | null,
  "period_month": string | null,
  "period_year": integer | null,
  "release_date": string | null,
  "summary": string | null
}
Interpret "current" and "previous" as year-over-year percentage changes.
If a field is unknown, use null. Do not include markdown.
            """.strip()
        )
        return self._reading_from_payload(payload)

    def _reading_from_payload(self, payload: dict[str, Any]) -> PerplexityReading:
        return PerplexityReading(
            current=float(payload["current"]),
            previous=float(payload["previous"]) if payload.get("previous") is not None else None,
            period_month=payload.get("period_month"),
            period_year=int(payload["period_year"]) if payload.get("period_year") is not None else None,
            release_date=payload.get("release_date"),
            summary=payload.get("summary"),
            citations=[str(item) for item in payload.get("citations", []) if item],
        )

    def _ask_json(self, prompt: str) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Perplexity API key is not configured")

        response = httpx.post(
            self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You extract structured macroeconomic release data. "
                            "Reply with JSON only."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=45.0,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        payload = json.loads(self._extract_json(content))
        citations = data.get("citations") or []
        if citations and "citations" not in payload:
            payload["citations"] = citations
        return payload

    def _extract_json(self, content: str) -> str:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise RuntimeError(f"Unable to parse JSON payload from Perplexity response: {content[:160]}")
        return match.group(0)
