"""Equivalence map for cross-ETF substitutes.

Steno may write "GLD" while Dan holds "IAU" — both physical gold ETFs. The mirror
engine needs to recognize them as the same exposure so it doesn't tell Dan to
"Buy GLD" while also flagging IAU as "Remove".

Each group lists tickers that should be treated as a single exposure. Membership
is symmetric (any ticker in a group can stand in for any other). The first
ticker in each group is the canonical name we display when collapsing.

Dan can override or extend this list by editing runtime/steno/aliases.json
(see load_user_overrides). User overrides MERGE with the defaults — they
extend groups but don't replace them, so we don't accidentally erase the
defaults if Dan only wants to add one mapping.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import ROOT_DIR


logger = logging.getLogger(__name__)

USER_OVERRIDES_PATH = ROOT_DIR / "runtime" / "steno" / "aliases.json"


# Groups of fungible tickers. Each group is one "exposure" — a Steno target on
# any ticker in the group is matched against Dan's holding on any other ticker
# in the group.
DEFAULT_GROUPS: list[list[str]] = [
    # Physical gold ETFs
    ["GLD", "IAU", "PHYS", "AAAU", "SGOL", "GLDM"],
    # Physical silver ETFs
    ["SLV", "PSLV", "SIVR"],
    # Broad commodities
    ["DJP", "DBC", "PDBC", "GSG"],
    # Copper exposure
    ["COPX", "CPER"],
    # Oil — futures vs services vs majors
    ["USO", "BNO", "OIL"],
    # US large-cap S&P 500
    ["SPY", "VOO", "IVV", "SPLG"],
    # Nasdaq 100
    ["QQQ", "QQQM"],
    # US small-cap
    ["IWM", "VTWO", "IJR"],
    # US total market
    ["VTI", "ITOT", "SCHB"],
    # US long-duration Treasuries (20y+)
    ["TLT", "VGLT", "EDV"],
    # US intermediate Treasuries (7-10y)
    ["IEF", "VGIT", "GOVT"],
    # TIPS
    ["TIP", "VTIP", "SCHP"],
    # High-yield credit
    ["HYG", "JNK", "SHYG"],
    # Investment-grade credit
    ["LQD", "VCIT", "IGSB"],
    # Dollar bull
    ["UUP", "USDU"],
    # EUR (vs USD) — long EUR/short USD pair trade
    ["FXE", "EUR"],
    # JPY
    ["FXY", "JPY"],
    # AUD
    ["FXA", "AUD"],
    # GBP
    ["FXB", "GBP"],
    # CHF
    ["FXF", "CHF"],
    # CAD
    ["FXC", "CAD"],
    # Bitcoin spot/proxy
    ["BTC", "BTC-USD", "IBIT", "FBTC", "GBTC", "BITB"],
    # Ethereum
    ["ETH", "ETH-USD", "ETHE", "ETHA"],
    # Semiconductors
    ["SMH", "SOXX", "XSD"],
    # Tech sector
    ["XLK", "VGT", "FTEC"],
    # Energy sector
    ["XLE", "VDE", "IYE"],
    # Financials
    ["XLF", "VFH", "IYF"],
    # Healthcare
    ["XLV", "VHT", "IYH"],
    # EM equities
    ["EEM", "VWO", "IEMG", "SPEM"],
    # China
    ["FXI", "MCHI", "KWEB"],
    # Japan
    ["EWJ", "DXJ"],
    # India
    ["INDA", "INDY", "EPI"],
    # Korea
    ["EWY", "FLKR"],
    # Mexico
    ["EWW"],
    # Brazil
    ["EWZ", "BRZU"],
    # Developed ex-US
    ["EFA", "VEA", "IEFA"],
    # ACWI / global
    ["ACWI", "VT", "URTH"],
]


def _normalize(ticker: str | None) -> str | None:
    if not ticker:
        return None
    return ticker.strip().upper()


def load_user_overrides() -> list[list[str]]:
    """User-defined extra groups, merged with the defaults."""
    if not USER_OVERRIDES_PATH.exists():
        return []
    try:
        raw = json.loads(USER_OVERRIDES_PATH.read_text())
        if isinstance(raw, dict) and "groups" in raw:
            raw = raw["groups"]
        if not isinstance(raw, list):
            return []
        return [
            [t.strip().upper() for t in g if isinstance(t, str) and t.strip()]
            for g in raw
            if isinstance(g, list)
        ]
    except Exception as exc:
        logger.warning("Equivalence overrides unreadable: %s", exc)
        return []


def build_equivalence_table() -> dict[str, set[str]]:
    """ticker → set of fungible tickers (including itself)."""
    table: dict[str, set[str]] = {}
    groups = DEFAULT_GROUPS + load_user_overrides()
    for group in groups:
        cleaned = {_normalize(t) for t in group if _normalize(t)}
        if len(cleaned) < 2:
            continue
        # Merge any pre-existing group containing one of these tickers, so user
        # overrides can extend a default group cleanly.
        for t in list(cleaned):
            if t in table:
                cleaned |= table[t]
        for t in cleaned:
            table[t] = cleaned
    return table


def equivalents(ticker: str | None) -> set[str]:
    """Return all tickers equivalent to the given one (always includes itself)."""
    canon = _normalize(ticker)
    if not canon:
        return set()
    table = build_equivalence_table()
    return table.get(canon, {canon})


def canonical(ticker: str | None) -> str | None:
    """Pick the first ticker from the equivalence group as the canonical name.

    Used for collapsing two rows (Steno wants GLD, Dan holds IAU) into a single
    signal display. We choose the first ticker by sorted order so the choice is
    deterministic regardless of which side of the comparison we came from.
    """
    canon = _normalize(ticker)
    if not canon:
        return None
    eq = equivalents(canon)
    if eq == {canon}:
        return canon
    return sorted(eq)[0]
