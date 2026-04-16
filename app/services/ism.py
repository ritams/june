from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup


EASTERN = ZoneInfo("America/New_York")
MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


@dataclass
class ISMReading:
    current: float
    previous: float | None
    data_month: str
    data_year: int
    previous_month: str | None
    release_at: str
    source_url: str


class ISMClient:
    base_url = "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi"

    def latest_manufacturing_pmi(self) -> ISMReading:
        now = datetime.now(EASTERN)
        last_error: Exception | None = None

        for months_back in (1, 2, 3, 4):
            year, month = self._subtract_months(now.year, now.month, months_back)
            slug = MONTH_NAMES[month].lower()
            url = f"{self.base_url}/{slug}/"
            try:
                html = self._fetch_html(url)
                return self._parse_report(html, url)
            except Exception as exc:  # pragma: no cover - network path
                last_error = exc

        raise RuntimeError(f"Unable to fetch official ISM Manufacturing PMI report: {last_error}")

    def _parse_report(self, html: str, source_url: str) -> ISMReading:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

        report_match = re.search(
            r"([A-Za-z]+)\s+(\d{4})\s+ISM\s*(?:®)?\s+Manufacturing PMI\s*(?:®)?\s+Report",
            text,
        )
        if not report_match:
            raise RuntimeError("Unable to parse report month from ISM page")

        current_match = re.search(r"Manufacturing PMI\s*(?:®)?\s+at\s+([0-9.]+)%", text)
        if not current_match:
            raise RuntimeError("Unable to parse current ISM PMI value")

        delta_match = re.search(
            r"registered\s+([0-9.]+)\s+percent in\s+([A-Za-z]+),.*?reading of\s+([0-9.]+)\s+percent in\s+([A-Za-z]+)",
            text,
            re.IGNORECASE | re.DOTALL,
        )

        data_month = report_match.group(1)
        data_year = int(report_match.group(2))
        current = float(current_match.group(1))
        previous = float(delta_match.group(3)) if delta_match else None
        previous_month = delta_match.group(4) if delta_match else None

        release_year, release_month = self._add_months(data_year, self._month_number(data_month), 1)
        release_at = self._release_datetime(release_year, release_month).isoformat()

        return ISMReading(
            current=current,
            previous=previous,
            data_month=data_month,
            data_year=data_year,
            previous_month=previous_month,
            release_at=release_at,
            source_url=source_url,
        )

    def _release_datetime(self, year: int, month: int) -> datetime:
        target_business_day = 2 if month == 1 else 1
        count = 0
        day = 1
        while True:
            candidate = datetime(year, month, day, 10, 0, tzinfo=EASTERN)
            if candidate.weekday() < 5:
                count += 1
                if count == target_business_day:
                    return candidate
            day += 1

    def _month_number(self, name: str) -> int:
        for number, month_name in MONTH_NAMES.items():
            if month_name.lower() == name.lower():
                return number
        raise RuntimeError(f"Unknown month name: {name}")

    def _subtract_months(self, year: int, month: int, delta: int) -> tuple[int, int]:
        return self._add_months(year, month, -delta)

    def _add_months(self, year: int, month: int, delta: int) -> tuple[int, int]:
        total = (year * 12 + (month - 1)) + delta
        new_year, month_index = divmod(total, 12)
        return new_year, month_index + 1

    def _fetch_html(self, url: str) -> str:
        try:
            response = httpx.get(url, timeout=20.0, follow_redirects=True)
            response.raise_for_status()
            return response.text
        except Exception:
            result = subprocess.run(
                ["curl", "-fsSL", url],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.stdout
