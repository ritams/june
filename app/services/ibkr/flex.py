"""IBKR Flex Web Service client.

Two-step pull:
  1. POST FlexStatementService.SendRequest with token + query_id → reference code
  2. POST FlexStatementService.GetStatement with token + reference code → XML

Parses positions, cash balances, and NAV out of the response. Read-only by
construction — Flex can't place trades.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree as ET

import httpx


logger = logging.getLogger(__name__)

FLEX_BASE = "https://gdcdyn.interactivebrokers.com/Universal/servlet"
SEND_URL = f"{FLEX_BASE}/FlexStatementService.SendRequest"
GET_URL = f"{FLEX_BASE}/FlexStatementService.GetStatement"
PROTOCOL_VERSION = "3"


class IBKRFlexError(RuntimeError):
    """Any IBKR Flex API failure (missing creds, IBKR error code, parse error)."""


@dataclass
class Position:
    symbol: str
    description: str
    asset_category: str
    position: float          # quantity (negative for shorts)
    mark_price: float
    market_value: float      # in base currency
    currency: str
    cost_basis_price: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CashBalance:
    currency: str
    ending_cash: float       # in that currency
    market_value: float      # in base currency


@dataclass
class PortfolioSnapshot:
    fetched_at: str
    account_id: str
    base_currency: str
    nav: float                                  # total NAV in base currency
    positions: list[Position]
    cash: list[CashBalance]
    raw_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def total_cash_base(self) -> float:
        return sum(c.market_value for c in self.cash)

    @property
    def total_positions_base(self) -> float:
        return sum(p.market_value for p in self.positions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "account_id": self.account_id,
            "base_currency": self.base_currency,
            "nav": self.nav,
            "total_cash_base": self.total_cash_base,
            "total_positions_base": self.total_positions_base,
            "positions": [
                {
                    "symbol": p.symbol,
                    "description": p.description,
                    "asset_category": p.asset_category,
                    "position": p.position,
                    "mark_price": p.mark_price,
                    "market_value": p.market_value,
                    "currency": p.currency,
                    "cost_basis_price": p.cost_basis_price,
                }
                for p in self.positions
            ],
            "cash": [
                {"currency": c.currency, "ending_cash": c.ending_cash, "market_value": c.market_value}
                for c in self.cash
            ],
            "raw_meta": self.raw_meta,
        }


def _require_creds() -> tuple[str, str]:
    token = os.getenv("IBKR_FLEX_TOKEN", "").strip()
    query_id = os.getenv("IBKR_FLEX_QUERY_ID", "").strip()
    if not token or not query_id:
        raise IBKRFlexError(
            "IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID must be set in .env. "
            "Generate them in IBKR Client Portal → Reporting → Flex Queries / Flex Web Service."
        )
    return token, query_id


def _parse_send_response(xml_text: str) -> str:
    """Extract the reference code (or raise on IBKR error)."""
    root = ET.fromstring(xml_text)
    status = (root.findtext("Status") or "").strip()
    if status.lower() != "success":
        code = root.findtext("ErrorCode") or "?"
        msg = root.findtext("ErrorMessage") or "unknown error"
        raise IBKRFlexError(f"IBKR Flex SendRequest failed (code {code}): {msg}")
    ref = (root.findtext("ReferenceCode") or "").strip()
    if not ref:
        raise IBKRFlexError("IBKR Flex SendRequest succeeded but returned no ReferenceCode")
    return ref


def _parse_statement(xml_text: str) -> PortfolioSnapshot:
    """Parse a Flex statement XML into a PortfolioSnapshot."""
    root = ET.fromstring(xml_text)

    # IBKR sometimes wraps an error in a top-level <FlexStatementResponse> too.
    err_code = root.findtext(".//ErrorCode")
    if err_code:
        msg = root.findtext(".//ErrorMessage") or "unknown error"
        raise IBKRFlexError(f"IBKR Flex GetStatement returned error code {err_code}: {msg}")

    # We accept both possible roots: <FlexQueryResponse> direct or <FlexStatementResponse>.
    stmt = root.find(".//FlexStatement")
    if stmt is None:
        raise IBKRFlexError("Missing <FlexStatement> in Flex response (check Activity Flex Query config)")

    account_id = stmt.attrib.get("accountId", "")
    period_start = stmt.attrib.get("fromDate", "")
    period_end = stmt.attrib.get("toDate", "")

    account_info = stmt.find("AccountInformation")
    base_ccy = (account_info.attrib.get("currency") if account_info is not None else "") or "USD"

    # NAV — Equity Summary in Base provides nav as "total" attribute on EquitySummaryInBase
    nav = 0.0
    nav_node = stmt.find(".//EquitySummaryByReportDateInBase")
    if nav_node is not None:
        try:
            nav = float(nav_node.attrib.get("total", "0") or 0)
        except ValueError:
            nav = 0.0
    if nav == 0.0:
        # Fallback: sum positions market value + cash
        nav = None  # filled below

    positions: list[Position] = []
    for pos in stmt.findall(".//OpenPositions/OpenPosition"):
        try:
            qty = float(pos.attrib.get("position", "0") or 0)
            if qty == 0:
                continue
            positions.append(
                Position(
                    symbol=pos.attrib.get("symbol", "") or pos.attrib.get("underlyingSymbol", ""),
                    description=pos.attrib.get("description", ""),
                    asset_category=pos.attrib.get("assetCategory", ""),
                    position=qty,
                    mark_price=float(pos.attrib.get("markPrice", "0") or 0),
                    market_value=float(pos.attrib.get("positionValueInBase", pos.attrib.get("positionValue", "0")) or 0),
                    currency=pos.attrib.get("currency", base_ccy),
                    cost_basis_price=float(pos.attrib.get("costBasisPrice", "0") or 0) or None,
                    raw=dict(pos.attrib),
                )
            )
        except ValueError as exc:
            logger.warning("Position parse error: %s — %s", exc, pos.attrib)

    cash: list[CashBalance] = []
    for c in stmt.findall(".//CashReport/CashReportCurrency"):
        try:
            cur = c.attrib.get("currency", "")
            if not cur or cur == "BASE_SUMMARY":
                # IBKR includes a BASE_SUMMARY row that aggregates everything; skip.
                continue
            ending = float(c.attrib.get("endingCash", "0") or 0)
            mv_base = float(c.attrib.get("endingCashInBase", c.attrib.get("endingCash", "0")) or 0)
            cash.append(CashBalance(currency=cur, ending_cash=ending, market_value=mv_base))
        except ValueError:
            continue

    if nav is None:
        nav = sum(p.market_value for p in positions) + sum(c.market_value for c in cash)

    return PortfolioSnapshot(
        fetched_at=datetime.now(timezone.utc).isoformat(),
        account_id=account_id,
        base_currency=base_ccy,
        nav=nav,
        positions=positions,
        cash=cash,
        raw_meta={"period_start": period_start, "period_end": period_end},
    )


def fetch_snapshot(*, max_poll: int = 6, poll_delay: float = 3.0) -> PortfolioSnapshot:
    """Pull the latest Flex statement and parse into a PortfolioSnapshot."""
    token, query_id = _require_creds()
    headers = {"User-Agent": "DJG-Advisory/0.1"}

    with httpx.Client(timeout=60.0, headers=headers) as client:
        send_resp = client.get(SEND_URL, params={"t": token, "q": query_id, "v": PROTOCOL_VERSION})
        send_resp.raise_for_status()
        ref_code = _parse_send_response(send_resp.text)
        logger.info("IBKR Flex statement requested (ref=%s); polling for completion", ref_code)

        # IBKR generates the report asynchronously. Poll GetStatement.
        last_text = ""
        for attempt in range(max_poll):
            time.sleep(poll_delay if attempt > 0 else poll_delay)  # initial wait too
            get_resp = client.get(GET_URL, params={"t": token, "q": ref_code, "v": PROTOCOL_VERSION})
            last_text = get_resp.text
            # IBKR returns a transient error code 1019 while the report is still cooking.
            root = ET.fromstring(last_text)
            err = root.findtext(".//ErrorCode")
            if err == "1019":
                logger.info("Flex statement not ready yet (attempt %d/%d)", attempt + 1, max_poll)
                continue
            if err:
                msg = root.findtext(".//ErrorMessage") or "unknown error"
                raise IBKRFlexError(f"IBKR Flex returned error {err}: {msg}")
            # No error → parse and return
            return _parse_statement(last_text)

        raise IBKRFlexError(f"IBKR Flex statement not ready after {max_poll} polls. Last response: {last_text[:200]}")
