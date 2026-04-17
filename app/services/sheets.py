from __future__ import annotations

from typing import Any

import gspread


HEADERS = [
    "logged_at",
    "signal",
    "signal_summary",
    "signal_action",
    "liquidity_status",
    "business_cycle_status",
    "dxy",
    "m2_mom",
    "rrp",
    "tga",
    "fed_balance_sheet",
    "global_m2_proxy",
    "ism_pmi",
    "yield_curve",
    "credit_spreads_bps",
    "jobless_claims",
    "korean_exports_yoy",
]


class SheetsClient:
    def __init__(self, spreadsheet_id: str | None, credentials_file: str | None, worksheet_title: str) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.credentials_file = credentials_file
        self.worksheet_title = worksheet_title

    @property
    def enabled(self) -> bool:
        return bool(self.spreadsheet_id and self.credentials_file)

    def append_snapshot(self, row: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        client = gspread.service_account(filename=self.credentials_file)
        spreadsheet = client.open_by_key(self.spreadsheet_id)
        try:
            worksheet = spreadsheet.worksheet(self.worksheet_title)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=self.worksheet_title, rows=1000, cols=30)
            worksheet.append_row(HEADERS)
        values = [row.get(header, "") for header in HEADERS]
        worksheet.append_row(values, value_input_option="USER_ENTERED")
        return True
