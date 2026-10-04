"""Emotional chord geometry: pure functions C's readings are validated through."""
from __future__ import annotations

from dual_lobe.b.chord import average, cosine, describe, parse_chord
from dual_lobe.state.memory import compose_chord_advisory


def test_parse_drops_unknown_axes_and_clamps():
    assert parse_chord({"joy": 2, "grief": 0.5, "fear": -1, "tenderness": 0.4}) == {
        "joy": 1.0, "tenderness": 0.4}


def test_parse_accepts_int_weights_and_strings():
    assert parse_chord({"joy": 1, "pride": "0.5"}) == {"joy": 1.0, "pride": 0.5}


def test_parse_rejects_garbage():
    assert parse_chord(None) == {}
    assert parse_chord("joy") == {}
    assert parse_chord({"joy": "loud"}) == {}
    assert parse_chord({}) == {}


def test_parse_caps_voices_to_the_loudest():
    chord = parse_chord({"joy": 0.1, "sadness": 0.9, "fear": 0.2,
                         "anger": 0.8, "tenderness": 1.0, "pride": 0.7})
    assert set(chord) == {"tenderness", "sadness", "anger", "pride"}


def test_bittersweet_resonates_with_bittersweet_not_joy():
    """The point of the chord: a mixed state recalls mixed states."""
    bittersweet = {"joy": 0.6, "sadness": 0.7}
    pure_joy = {"joy": 0.9}
    assert cosine(bittersweet, bittersweet) == 1.0
    assert cosine(bittersweet, pure_joy) < cosine(bittersweet, {"sadness": 0.5, "joy": 0.7})
    assert cosine(bittersweet, pure_joy) < 0.75


def test_empty_chords_are_dissimilar_to_everything():
    assert cosine({}, {"joy": 1}) == 0.0
    assert cosine({"joy": 1}, {}) == 0.0


def test_average_is_salience_weighted_and_renormalized():
    # Loud recent note (salience 3) dominates the quiet old one (salience 1).
    state = average([({"sadness": 1.0}, 3.0), ({"joy": 1.0}, 1.0)])
    assert state["sadness"] == 1.0
    assert state["joy"] < state["sadness"]


def test_average_of_empties_is_empty():
    assert average([({}, 3.0), ({}, 1.0)]) == {}


def test_describe_labels_two_loud_voices():
    assert describe({"joy": 0.6, "sadness": 0.7}) == "sorrowful-joyful"
    assert describe({"joy": 1.0, "pride": 0.1}) == "joyful"
    assert describe({}) == "neutral"


def test_advisory_renders_loud_voices_and_forbids_naming():
    text = compose_chord_advisory({"sadness": 1.0, "joy": 0.6, "pride": 0.1})
    assert "sorrowful-joyful" in text
    assert "sadness 1" in text and "joy 0.6" in text
    assert "pride" not in text          # quiet voices are not advertised
    assert "style guidance only" in text
    assert compose_chord_advisory({}) is None
    assert compose_chord_advisory({"joy": 0.1}) is None  # nothing loud enough