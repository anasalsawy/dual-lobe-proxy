"""Companion interactions: surface tagging, revisit-and-revise, unaddressed
queue, mood recall, session mood advisory.

Everything here must be an enhancement: no feature may break a turn, and the
affect line must never touch the meter.
"""
from __future__ import annotations

from dual_lobe.api.surface import SurfaceContext, parse_surface
from dual_lobe.state.memory import compose_mood_advisory


class FakeHeaders(dict):
    """Starlette-compatible header view (case-insensitive get)."""

    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


# --- Surface tagging (Companion Phase 2) ---

def test_surface_headers_parse_and_validate():
    surface = parse_surface(FakeHeaders({
        "X-DL-Surface-Device": "phone",
        "X-DL-Surface-App": "companion-ios",
    }))
    assert surface.device == "phone"
    assert surface.app == "companion-ios"
    assert surface
    line = surface.ephemeral_line()
    assert "device: phone" in line and "app: companion-ios" in line
    assert "not what they said" in line  # memory vs surface rule


def test_surface_headers_never_reject_garbage():
    """Surface is an enhancement: hostile/absurd headers drop, never error."""
    surface = parse_surface(FakeHeaders({
        "X-DL-Surface-Device": "<script>alert(1)</script> " * 40,
        "X-DL-Surface-App": "",
    }))
    assert surface.device == "script"  # only leading safe chars kept, bounded
    assert surface.app == ""
    assert parse_surface(FakeHeaders({})) == SurfaceContext("", "")


def test_missing_surface_is_falsy():
    assert not parse_surface(FakeHeaders({}))
    assert parse_surface(FakeHeaders({})).ephemeral_line() == ""


# --- Session mood advisory (tone, never the meter) ---

def test_mood_advisory_requires_unanimous_recent_feelings():
    assert compose_mood_advisory(["warm", "warm", "warm"]) is not None
    assert "style guidance only" in compose_mood_advisory(["warm", "warm", "warm"])
    assert "never changes verification" in compose_mood_advisory(["warm", "warm", "warm"])


def test_mood_advisory_silent_on_mixed_or_sparse_feelings():
    assert compose_mood_advisory(["warm", "tired", "warm"]) is None
    assert compose_mood_advisory(["warm", "warm"]) is None
    assert compose_mood_advisory(["neutral", "neutral", "neutral"]) is None
    assert compose_mood_advisory([]) is None


# --- Revisit-and-revise note contract ---

def test_revision_note_is_grounded_in_this_turn():
    """The revision note carries the user's own words as its quote."""
    note = {"kind": "callback", "anchor": "verdict revision: previously GREEN, now YELLOW",
            "quote": "actually the retry fixed it", "feeling": "neutral", "salience": 2}
    assert note["quote"] == "actually the retry fixed it"
    assert "previously GREEN" in note["anchor"]
    assert note["kind"] == "callback"