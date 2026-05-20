"""Doc-type registry for the Steno mirror.

Real Vision publishes several Steno-authored / Steno-Research products; only
some contain portfolio-relevant signal. This registry lets the downloader,
extractor, and theme universe treat each type appropriately.

Per Dan's spec (May 2026):
  • Steno Signals          — macro thesis only, NO portfolio table. Contributes
                              risk_tone + summary, but extracting `positions`
                              from these reports is what caused the earlier
                              hallucinations (100% Energy Stocks etc.).
  • Weekly Alpha Digest    — has a "Portfolio Update" narrative section with
                              trims/adds + YTD performance. Useful for tracking
                              direction changes on existing themes.
  • What We Told Hedge Funds — most actionable: explicit tickers + thesis
                              context. Primary source for new positions.
  • The Drill              — geopolitical / commodity color only. SKIP.
  • RV Pro Portfolio       — different product, SKIP per user instruction.
  • Macro Meets Micro      — sector deep-dives, no model portfolio. SKIP.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DocType:
    key: str                    # internal identifier
    slug_prefix: str            # URL-slug prefix to match RV reports
    label: str                  # human-readable name
    has_positions: bool         # does this doc include portfolio positions worth extracting?
    contributes_themes: bool    # should themes from this doc enter the universe?
    weight_priority: int        # higher = beats other docs for same theme (when timestamps tie)


DOC_TYPES: list[DocType] = [
    DocType(
        key="what_we_told_hedge_funds",
        slug_prefix="what-we-told-hedge-funds-this-week",
        label="What We Told Hedge Funds",
        has_positions=True,
        contributes_themes=True,
        weight_priority=3,           # most actionable — wins on tie
    ),
    DocType(
        key="weekly_alpha_digest",
        slug_prefix="the-weekly-alpha-digest",
        label="Weekly Alpha Digest",
        has_positions=True,
        contributes_themes=True,
        weight_priority=2,
    ),
    DocType(
        key="steno_signals",
        slug_prefix="steno-signals",
        label="Steno Signals",
        has_positions=False,         # macro thesis only — no portfolio table
        contributes_themes=False,    # do NOT extract themes; only risk_tone + summary
        weight_priority=1,
    ),
]

# Slugs we explicitly skip (downloader will not pull these)
SKIP_PREFIXES: list[str] = [
    "the-drill",
    "rv-pro-portfolio-update",
    "macro-meets-micro",
]

# Slugs the downloader should pull (everything in DOC_TYPES that matters)
INGEST_PREFIXES: list[str] = [d.slug_prefix for d in DOC_TYPES]


def classify_slug(slug: str) -> DocType | None:
    """Return the DocType for a given URL slug, or None if it should be skipped."""
    s = (slug or "").lower()
    for skip in SKIP_PREFIXES:
        if s.startswith(skip):
            return None
    for dt in DOC_TYPES:
        if s.startswith(dt.slug_prefix):
            return dt
    return None


def classify_filename(filename: str) -> DocType | None:
    """Same as classify_slug but works on a .pdf filename."""
    stem = (filename or "").lower().removesuffix(".pdf")
    return classify_slug(stem)
