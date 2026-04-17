from __future__ import annotations

import calendar
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
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


@dataclass(frozen=True)
class ISMHistoryPoint:
    period_end: date
    value: float
    source_url: str


class ISMClient:
    base_url = "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi"
    sitemap_url = "https://www.ismworld.org/sitemap.xml"
    pr_news_url = "https://www.prnewswire.com/news/institute-for-supply-management/"

    def historical_manufacturing_pmi(self) -> list[ISMHistoryPoint]:
        observations: dict[date, ISMHistoryPoint] = {}

        try:
            for point in self._pr_newswire_history():
                observations[point.period_end] = point
        except Exception:
            observations = {}

        if not observations:
            for url in self._archive_urls():
                try:
                    point = self._parse_archive_observation(self._fetch_html(url), url)
                except Exception:
                    continue
                observations[point.period_end] = point

            for point in self._recent_report_observations():
                observations[point.period_end] = point

        return [observations[key] for key in sorted(observations)]

    def _pr_newswire_history(self) -> list[ISMHistoryPoint]:
        observations: dict[date, ISMHistoryPoint] = {}

        for page in range(1, 9):
            url = f"{self.pr_news_url}?page={page}&pagesize=100"
            response = httpx.get(url, timeout=20.0, follow_redirects=True)
            response.raise_for_status()
            document = BeautifulSoup(response.text, "html.parser")
            page_points = 0

            for anchor in document.find_all("a", href=True):
                href = anchor["href"]
                if "/news-releases/" not in href:
                    continue
                text = " ".join(anchor.get_text(" ", strip=True).split())
                point = self._parse_pr_newswire_listing(text, href)
                if point is None:
                    continue
                observations[point.period_end] = point
                page_points += 1

            if page_points == 0:
                break

        return [observations[key] for key in sorted(observations)]

    def _parse_pr_newswire_listing(self, text: str, href: str) -> ISMHistoryPoint | None:
        slug = href.rsplit("/", 1)[-1]
        if "manufacturing" not in slug:
            return None
        if not (slug.startswith("manufacturing-pmi-at-") or slug.startswith("pmi-at-")):
            return None
        if "seasonal-factors" in slug:
            return None

        release_match = re.match(r"([A-Za-z]{3} \d{2}, \d{4}),\s+\d{2}:\d{2}\s+ET\s+", text)
        if release_match is None:
            return None

        release_date = datetime.strptime(release_match.group(1), "%b %d, %Y").date()
        title = text[release_match.end() :].strip()
        title = title.split(" Economic activity", 1)[0]
        title = title.split(" The report was issued", 1)[0]

        if "Manufacturing" not in title or "PMI" not in title:
            return None

        value_match = re.match(r"(?:Manufacturing\s+)?PMI(?:®)?\s+at\s+([0-9]+(?:\.[0-9])?)%", title)
        if value_match is None:
            return None

        period_match = None
        for segment in re.split(r"[;,]", title)[1:]:
            candidate = re.match(r"\s*([A-Za-z]+)(?:\s+(\d{4}))?\b.*Manufacturing", segment)
            if candidate:
                period_match = candidate
                break
        if period_match is None:
            return None

        month_name = period_match.group(1)
        month_number = self._month_number(month_name)
        if period_match.group(2):
            year = int(period_match.group(2))
        else:
            year = release_date.year - 1 if month_number > release_date.month else release_date.year

        return ISMHistoryPoint(
            period_end=self._period_end(year, month_number),
            value=float(value_match.group(1)),
            source_url=f"https://www.prnewswire.com{href}",
        )

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

    def _archive_urls(self) -> list[str]:
        xml = self._fetch_html(self.sitemap_url)
        urls = set(
            re.findall(
                r"https://www\.ismworld\.org/supply-management-news-and-reports/news-publications/inside-supply-management-magazine/blog/[^<]*/",
                xml,
            )
        )

        archive_urls: list[str] = []
        for url in urls:
            slug = url.rstrip("/").split("/")[-1]
            if "pmi" not in slug:
                continue
            if "services" in slug or "hospital" in slug:
                continue
            if "manufacturing" in slug or re.search(r"(?:^|-)pmi(?:-\d+)?$", slug):
                archive_urls.append(url)
        return sorted(archive_urls)

    def _recent_report_observations(self) -> list[ISMHistoryPoint]:
        now = datetime.now(EASTERN)
        observations: dict[date, ISMHistoryPoint] = {}

        for months_back in (1, 2, 3, 4):
            year, month = self._subtract_months(now.year, now.month, months_back)
            slug = MONTH_NAMES[month].lower()
            url = f"{self.base_url}/{slug}/"
            try:
                reading = self._parse_report(self._fetch_html(url), url)
            except Exception:
                continue

            current_month = self._month_number(reading.data_month)
            current_period = self._period_end(reading.data_year, current_month)
            observations[current_period] = ISMHistoryPoint(
                period_end=current_period,
                value=reading.current,
                source_url=reading.source_url,
            )

            if reading.previous is None or reading.previous_month is None:
                continue

            previous_month = self._month_number(reading.previous_month)
            previous_year = reading.data_year - 1 if previous_month > current_month else reading.data_year
            previous_period = self._period_end(previous_year, previous_month)
            observations[previous_period] = ISMHistoryPoint(
                period_end=previous_period,
                value=reading.previous,
                source_url=reading.source_url,
            )

        return [observations[key] for key in sorted(observations)]

    def _parse_archive_observation(self, html: str, source_url: str) -> ISMHistoryPoint:
        document = BeautifulSoup(html, "html.parser")
        heading = document.select_one(".magazineArticle__heading")
        date_node = document.select_one(".magazineArticle__date")
        body = document.select_one(".richText__content")

        if heading is None or date_node is None or body is None:
            raise RuntimeError("Unable to parse ISM archive article")

        title = heading.get_text(" ", strip=True)
        article_date = datetime.strptime(date_node.get_text(" ", strip=True), "%B %d, %Y").date()
        body_text = body.get_text(" ", strip=True)
        month_name, year = self._archive_period(title, article_date)
        value = self._archive_value(body_text)

        return ISMHistoryPoint(
            period_end=self._period_end(year, self._month_number(month_name)),
            value=value,
            source_url=source_url,
        )

    def _archive_period(self, title: str, article_date: date) -> tuple[str, int]:
        match = re.search(r":\s*([A-Za-z]+)(?:\s+(\d{4}))?\s+(?:Manufacturing\s+)?PMI", title, re.IGNORECASE)
        if not match:
            raise RuntimeError("Unable to parse archive period from ISM title")

        month_name = match.group(1)
        month_number = self._month_number(month_name)
        if match.group(2):
            return month_name, int(match.group(2))

        year = article_date.year
        if month_number > article_date.month:
            year -= 1
        return month_name, year

    def _archive_value(self, text: str) -> float:
        patterns = (
            r"composite(?:\s+index)?\s+(?:reading|figure)\s+of\s+([0-9]{2}\.[0-9])\s+percent",
            r"Manufacturing PMI(?:\s*®)?\s+(?:of|at|came in at|registered)\s+([0-9]{2}\.[0-9])\s+percent",
            r"composite PMI(?:\s*®)?\s+(?:of|at)\s+([0-9]{2}\.[0-9])\s+percent",
            r"PMI(?:\s*®)?\s+came in at\s+([0-9]{2}\.[0-9])\s+percent",
            r"PMI(?:\s*®)?\s+of\s+([0-9]{2}\.[0-9])\s+percent",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return float(match.group(1))
        raise RuntimeError("Unable to parse PMI value from ISM archive article")

    def _period_end(self, year: int, month: int) -> date:
        return date(year, month, calendar.monthrange(year, month)[1])

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
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
        }
        try:
            result = subprocess.run(
                ["curl", "-A", headers["User-Agent"], "-fsSL", url],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.stdout
        except Exception:
            response = httpx.get(url, timeout=20.0, follow_redirects=True, headers=headers)
            response.raise_for_status()
            return response.text
