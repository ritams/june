"""SLR / eSLR plumbing note — per build-28th-may.md §13-14.

Spec: this is a "small policy/plumbing note," NOT a scoring input. The Hermes
card and the Liquidity page each show one line like:

    Bank Plumbing / SLR: Supportive / Neutral / Restrictive

Classification logic:

  Pre-2026-01-01:   Restrictive  (old eSLR rule constrained large-bank capacity
                                  to intermediate Treasuries; binding constraint)
  2026-01-01 to 2026-04-01:
                    Neutral      (early-adoption window — some banks moving,
                                  some not, capacity effect mixed)
  2026-04-01 onwards:
                    Supportive   (eSLR relaxation effective — frees ~5% of
                                  Treasury-related balance-sheet capacity at
                                  the largest banks; meaningful tailwind for
                                  Treasury market liquidity and indirectly for
                                  risk-asset financing)

Reference: U.S. regulators finalised the eSLR change with effectiveness
April 1, 2026 and early adoption permitted from January 1, 2026 (build-28th-
may.md §13). The dates are exact; the classification is deterministic.

This module returns BOTH a label and a one-line note suitable for display.
"""

from __future__ import annotations

from datetime import date


EARLY_ADOPTION_START = date(2026, 1, 1)
EFFECTIVENESS_DATE = date(2026, 4, 1)


def classify(today: date | None = None) -> dict[str, str]:
    """Return the current SLR plumbing classification.

    Returns:
      {
        "label": "Supportive" | "Neutral" | "Restrictive",
        "note": "Bank Plumbing / SLR: <label> · <context>",
        "as_of": "YYYY-MM-DD"
      }
    """
    today = today or date.today()
    if today < EARLY_ADOPTION_START:
        label = "Restrictive"
        context = (
            "old eSLR rule still in force; large-bank balance-sheet capacity "
            "for Treasury intermediation is constrained"
        )
    elif today < EFFECTIVENESS_DATE:
        label = "Neutral"
        context = (
            "early-adoption window (eSLR effective April 1 2026); some banks "
            "moving, some not — capacity impact mixed"
        )
    else:
        label = "Supportive"
        context = (
            "eSLR relaxation effective April 1 2026 freed large-bank "
            "balance-sheet capacity for Treasury intermediation"
        )

    return {
        "label": label,
        "note": f"Bank Plumbing / SLR: {label} · {context}",
        "as_of": today.isoformat(),
    }


def current_note(today: date | None = None) -> str:
    """Convenience — the one-line note for embedding in the CIO card / Liquidity page."""
    return classify(today)["note"]
