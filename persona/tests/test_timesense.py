"""Tests for time sense: gap awareness between exchanges."""
from __future__ import annotations

from dual_lobe_persona.timesense import TimeSense, _describe_gap


def test_first_contact_has_no_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    ts = TimeSense("persona-key-1")
    assert ts.elapsed_since_last_seen() is None
    assert ts.context_line() == ""


def test_gap_described_humanly(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    ts = TimeSense("persona-key-1")
    ts.stamp(now=1_000_000.0)
    line = ts.context_line(now=1_000_000.0 + 3 * 86400)
    assert "3 days" in line
    assert "(259200 seconds)" in line
    assert line.startswith("[time]")


def test_stamp_updates_between_exchanges(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    ts = TimeSense("persona-key-1")
    ts.stamp(now=1_000_000.0)
    gap = ts.elapsed_since_last_seen(now=1_000_000.0 + 300)
    assert gap == 300


def test_gap_descriptions():
    assert _describe_gap(30) == "moments"
    assert _describe_gap(120) == "2 minutes"
    assert _describe_gap(7_200) == "2 hours"
    assert _describe_gap(5 * 86400) == "5 days"
    assert _describe_gap(2 * 86400) == "2 days"
    assert _describe_gap(20 * 86400) == "2 weeks"
    assert _describe_gap(70 * 86400) == "2 months"
    assert _describe_gap(800 * 86400) == "2 years"


def test_timesense_isolated_per_persona(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    TimeSense("persona-key-1").stamp()
    assert TimeSense("persona-key-2").elapsed_since_last_seen() is None


def test_turn_duration_measured(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    ts = TimeSense("persona-key-1")
    mono = ts.mark_turn_started(now=1_000.0)
    duration = ts.mark_turn_ended(mono, now=1_014.0)
    # duration is real elapsed monotonic time between the two marks
    assert 0 <= duration < 15
    line = ts.duration_line(duration)
    assert "second" in line  # "under a second" or "N seconds"


def test_context_line_mentions_previous_duration(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    ts = TimeSense("persona-key-1")
    mono = ts.mark_turn_started()
    ts.mark_turn_ended(mono)
    line = ts.context_line()
    assert "took" in line
    assert "since you two last talked" in line


def test_clock_state_machine_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    ts = TimeSense("persona-key-1")
    mono = ts.mark_turn_started(now=1_000.0)
    ts.mark_turn_ended(mono, now=1_012.0)
    state = ts.clock_state(now=1_060.0)
    assert state["seconds_since_last_seen"] == 48.0
    assert state["last_turn"]["duration_seconds"] is not None
    assert state["last_turn"]["ended"] == 1_012.0