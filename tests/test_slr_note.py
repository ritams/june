from datetime import date

from app.services.slr_note import classify, current_note, EARLY_ADOPTION_START, EFFECTIVENESS_DATE


def test_pre_2026_is_restrictive():
    out = classify(date(2025, 12, 31))
    assert out["label"] == "Restrictive"
    assert "constrained" in out["note"]


def test_early_adoption_window_is_neutral():
    assert classify(EARLY_ADOPTION_START)["label"] == "Neutral"
    assert classify(date(2026, 2, 14))["label"] == "Neutral"
    # Day BEFORE effectiveness still neutral
    assert classify(date(2026, 3, 31))["label"] == "Neutral"


def test_effectiveness_date_inclusive_supportive():
    assert classify(EFFECTIVENESS_DATE)["label"] == "Supportive"
    assert classify(date(2026, 6, 10))["label"] == "Supportive"
    assert classify(date(2027, 1, 1))["label"] == "Supportive"


def test_current_note_default_today():
    """Default arg uses today — just verify it returns a non-empty string."""
    note = current_note()
    assert isinstance(note, str)
    assert "SLR" in note
    assert note.startswith("Bank Plumbing")


def test_classify_payload_shape():
    out = classify(date(2026, 6, 10))
    assert set(out.keys()) == {"label", "note", "as_of"}
    assert out["as_of"] == "2026-06-10"
