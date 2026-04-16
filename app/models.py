from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Metric:
    key: str
    label: str
    display_value: str
    status: str
    summary: str
    secondary: str | None = None
    raw_value: float | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Dashboard:
    slug: str
    title: str
    status: str
    tone: str
    metrics: list[Metric] = field(default_factory=list)

    def counts(self) -> tuple[int, int]:
        positive = sum(metric.status == "positive" for metric in self.metrics)
        negative = sum(metric.status == "negative" for metric in self.metrics)
        return positive, negative

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "status": self.status,
            "tone": self.tone,
            "metrics": [metric.to_dict() for metric in self.metrics],
        }
