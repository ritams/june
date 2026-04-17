from __future__ import annotations

from datetime import date

from app.services.ism import ISMClient


def test_parse_archive_observation_handles_legacy_pmi_roundup() -> None:
    client = ISMClient()
    html = """
    <html>
      <h1 class="magazineArticle__heading">ROB Roundup: March PMI®</h1>
      <div class="magazineArticle__date">April 02, 2018</div>
      <div class="richText__content">
        <p>The PMI® of 59.3 percent indicated a 19th consecutive month of growth.</p>
      </div>
    </html>
    """

    point = client._parse_archive_observation(html, "https://example.com/march")

    assert point.period_end == date(2018, 3, 31)
    assert point.value == 59.3


def test_parse_archive_observation_handles_modern_manufacturing_roundup() -> None:
    client = ISMClient()
    html = """
    <html>
      <h1 class="magazineArticle__heading">Report On Business® Roundup: October 2024 Manufacturing PMI®</h1>
      <div class="magazineArticle__date">November 01, 2024</div>
      <div class="richText__content">
        <p>The October composite index figure of 46.5 percent pointed to another month of contraction.</p>
      </div>
    </html>
    """

    point = client._parse_archive_observation(html, "https://example.com/october")

    assert point.period_end == date(2024, 10, 31)
    assert point.value == 46.5


def test_parse_pr_newswire_listing_handles_modern_release_headline() -> None:
    client = ISMClient()
    text = (
        "Apr 01, 2026, 10:00 ET Manufacturing PMI® at 52.7%; March 2026 ISM® Manufacturing PMI® Report "
        "Economic activity in the manufacturing sector expanded in March."
    )

    point = client._parse_pr_newswire_listing(text, "/news-releases/manufacturing-pmi-at-52-7-march-2026-302730721.html")

    assert point is not None
    assert point.period_end == date(2026, 3, 31)
    assert point.value == 52.7


def test_parse_pr_newswire_listing_handles_legacy_release_headline_without_data_year() -> None:
    client = ISMClient()
    text = (
        "Sep 03, 2019, 10:00 ET PMI® at 49.1%; August Manufacturing ISM® Report On Business® "
        "Economic activity in the manufacturing sector contracted in August."
    )

    point = client._parse_pr_newswire_listing(text, "/news-releases/pmi-at-49-1-august-manufacturing-ism-report-on-business-300909470.html")

    assert point is not None
    assert point.period_end == date(2019, 8, 31)
    assert point.value == 49.1
