from app.services.ism import ISMClient


def test_parse_official_ism_report() -> None:
    html = """
    <html>
      <body>
        <h1>Manufacturing PMI® at 52.7%</h1>
        <h1>March 2026 ISM® Manufacturing PMI® Report</h1>
        <p>
          The Manufacturing PMI® registered 52.7 percent in March, a 0.3-percentage point increase
          compared to the reading of 52.4 percent in February.
        </p>
      </body>
    </html>
    """
    reading = ISMClient()._parse_report(html, "https://example.test/march/")
    assert reading.current == 52.7
    assert reading.previous == 52.4
    assert reading.data_month == "March"
    assert reading.data_year == 2026
    assert reading.previous_month == "February"
