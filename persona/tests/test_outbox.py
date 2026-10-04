"""Tests for the persona outbox policy, heartbeat, inbound webhook, profile."""
from __future__ import annotations

import json

import pytest

from dual_lobe_persona.outbox import Outbox, _in_quiet_hours


@pytest.fixture()
def outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    # Pin quiet hours to a one-minute window so tests are time-independent.
    monkeypatch.setattr("dual_lobe_persona.outbox.QUIET_HOURS", "23:59-23:59")
    return Outbox("persona-key-1")


def test_append_and_pending_and_drain(outbox):
    msg = outbox.append("whatsapp", "+15550001111", "thinking of you — how did the interview go?")
    assert msg is not None
    pending = outbox.pending()
    assert [p.id for p in pending] == [msg.id]
    drained = outbox.drain()
    assert [d.id for d in drained] == [msg.id]
    assert outbox.pending() == []


def test_daily_cap_blocks_spam(outbox, monkeypatch):
    monkeypatch.setattr("dual_lobe_persona.outbox.MAX_OUTREACH_PER_DAY", 2)
    assert outbox.append("email", "u@x.com", "one") is not None
    assert outbox.append("email", "u@x.com", "two") is not None
    assert outbox.append("email", "u@x.com", "three") is None
    assert outbox.append("email", "u@x.com", "four") is None


def test_quiet_hours_detection():
    # 23:30 inside 22:00-08:00
    import time as t

    stamp = t.mktime(t.strptime("2026-10-03 23:30", "%Y-%m-%d %H:%M"))
    assert _in_quiet_hours(stamp) is True
    stamp = t.mktime(t.strptime("2026-10-03 12:00", "%Y-%m-%d %H:%M"))
    assert _in_quiet_hours(stamp) is False


def test_quiet_hours_blocks_append(outbox, monkeypatch):
    import time as t

    monkeypatch.setattr("dual_lobe_persona.outbox.QUIET_HOURS", "00:00-23:59")
    stamp = t.mktime(t.strptime("2026-10-03 12:00", "%Y-%m-%d %H:%M"))
    assert outbox.append("sms", "+15550001111", "hi", now=stamp) is None


def test_outbox_survives_reload(outbox):
    outbox.append("whatsapp", "+15550001111", "hello")
    again = Outbox("persona-key-1")
    assert len(again.pending()) == 1


def test_outbox_isolated_per_persona(outbox, monkeypatch):
    outbox.append("whatsapp", "+15550001111", "for persona 1")
    other = Outbox("persona-key-2")
    assert other.pending() == []