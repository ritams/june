"""Real Vision PDF downloader — scrolls the report feed, intercepts PDF
network requests, then pulls the file via the authenticated context.

Same pattern as the sibling steno-bot. Filters to "Steno Signals" title.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from app.services.steno.config import STENO_DOWNLOADS_DIR


logger = logging.getLogger(__name__)

REPORTS_URL = "https://app.realvision.com/?contentTypes=REPORT"
BASE_URL = "https://app.realvision.com"
DELAY_BETWEEN_REPORTS = 2


class SessionExpiredError(Exception):
    pass


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text


def _collect_steno_report_urls(page) -> list[str]:
    page.goto(REPORTS_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)
    if "b2clogin.com" in page.url or "login" in page.url.lower():
        raise SessionExpiredError("Session expired: redirected to login")

    urls: list[str] = []
    seen: set[str] = set()
    prev_count = -1
    while True:
        cards = page.locator("a").all()
        for card in cards:
            try:
                href = card.get_attribute("href") or ""
                text = card.inner_text()
            except Exception:
                continue
            if "steno" not in text.lower() and "steno" not in href.lower():
                continue
            if href.startswith("http"):
                full = href
            elif href.startswith("/"):
                full = BASE_URL + href
            else:
                continue
            if "realvision.com" not in full:
                continue
            if full not in seen:
                seen.add(full)
                urls.append(full)
        if len(urls) == prev_count:
            break
        prev_count = len(urls)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

    logger.info("Steno feed scan found %d report(s)", len(urls))
    return urls


def _capture_pdf_url(page, report_url: str) -> str | None:
    pdf_url: list[str] = []

    def on_request(request):
        url = request.url
        if ".pdf" in url or "application/pdf" in (request.headers.get("accept", "")):
            if url not in pdf_url:
                pdf_url.append(url)

    def on_response(response):
        if "pdf" in response.headers.get("content-type", "") and response.url not in pdf_url:
            pdf_url.append(response.url)

    page.on("request", on_request)
    page.on("response", on_response)
    page.goto(report_url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)
    page.remove_listener("request", on_request)
    page.remove_listener("response", on_response)
    return pdf_url[0] if pdf_url else None


def _filename_from_url_and_title(url: str, title: str) -> str:
    parsed = urlparse(url)
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", parsed.path)
    if date_match:
        return f"steno-signals-{date_match.group(1)}.pdf"
    return f"{_slugify(title)}.pdf"


def download_steno_signals(auth_state_path: Path, *, redownload_all: bool = False) -> list[Path]:
    """Download new Steno Signals PDFs. Returns paths to newly-downloaded files."""
    from playwright.sync_api import sync_playwright  # noqa: WPS433

    STENO_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(auth_state_path))
        page = context.new_page()

        for i, report_url in enumerate(_collect_steno_report_urls(page)):
            slug = report_url.rstrip("/").split("/")[-1]
            preliminary = STENO_DOWNLOADS_DIR / f"{slug}.pdf"
            if not redownload_all and preliminary.exists():
                continue

            pdf_url = _capture_pdf_url(page, report_url)
            if not pdf_url:
                try:
                    pdf_link = page.locator('a[href*=".pdf"]').first.get_attribute("href", timeout=3000)
                    if pdf_link:
                        pdf_url = pdf_link if pdf_link.startswith("http") else BASE_URL + pdf_link
                except Exception:
                    pass
            if not pdf_url:
                continue

            try:
                resp = context.request.get(pdf_url)
                if resp.status != 200:
                    continue
                try:
                    title = page.locator("h1").first.inner_text(timeout=5000).strip()
                except Exception:
                    title = "untitled"
                out = STENO_DOWNLOADS_DIR / _filename_from_url_and_title(report_url, title)
                out.write_bytes(resp.body())
                logger.info("Downloaded %s (%dKB)", out.name, len(resp.body()) // 1024)
                downloaded.append(out)
            except Exception as exc:
                logger.error("Download failed for %s: %s", report_url, exc)

            time.sleep(DELAY_BETWEEN_REPORTS)

        browser.close()

    return downloaded
