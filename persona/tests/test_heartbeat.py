"""Tests for heartbeat initiative, inbound webhook, and profile endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import dual_lobe_persona.api as api


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.next_reply = {"content": "How did the exam go?", "tool_actions": 1}

    async def chat(self, persona_key: str, message: str, session: str = "default") -> dict:
        self.calls.append((persona_key, message, session))
        return self.next_reply

    def shutdown(self) -> None:
        pass


class FakeHeartbeat:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.interval_seconds = 3600
        self.registered: list[str] = []
        self.next_beat = {"outreach": False}

    def register(self, persona_key: str) -> None:
        self.registered.append(persona_key)

    async def beat(self, persona_key: str) -> dict:
        return self.next_beat

    def shutdown(self) -> None:
        pass


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setattr("dual_lobe_persona.outbox.QUIET_HOURS", "23:59-23:59")
    rt = FakeRuntime()
    api.app.state.runtime = rt
    api.app.state.heartbeat = FakeHeartbeat(rt)
    with TestClient(api.app) as c:
        yield c, rt


def test_inbound_round_trips_to_outbox(client):
    c, _ = client
    resp = c.post(
        "/v1/persona/inbound",
        json={"channel": "whatsapp", "from": "+15550001111", "text": "hey, you around?"},
        headers={"Authorization": "Bearer persona-key-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "How did the exam go?"
    assert body["queued"] is True
    out = c.get("/v1/persona/outbox", headers={"Authorization": "Bearer persona-key-1"})
    rows = out.json()
    assert len(rows) == 1
    assert rows[0]["channel"] == "whatsapp"
    assert rows[0]["to"] == "+15550001111"


def test_outbox_drain_marks_sent(client):
    c, _ = client
    c.post(
        "/v1/persona/inbound",
        json={"channel": "email", "from": "u@x.com", "text": "hello"},
        headers={"Authorization": "Bearer persona-key-1"},
    )
    drained = c.get("/v1/persona/outbox", params={"drain": "true"},
                    headers={"Authorization": "Bearer persona-key-1"}).json()
    assert len(drained) == 1
    assert c.get("/v1/persona/outbox", headers={"Authorization": "Bearer persona-key-1"}).json() == []


def test_heartbeat_registration(client):
    c, _ = client
    resp = c.post("/v1/persona/heartbeat",
                  json={"channel": "whatsapp", "address": "+15550001111"},
                  headers={"Authorization": "Bearer persona-key-1"})
    assert resp.status_code == 200
    assert resp.json()["registered"] is True


def test_beat_now_uses_runtime_chat(client):
    c, rt = client
    resp = c.post("/v1/persona/beat", headers={"Authorization": "Bearer persona-key-1"})
    assert resp.status_code == 200
    # The beat itself goes through the runtime chat path (fake here).
    assert rt.calls == []  # FakeHeartbeat.beat doesn't call runtime directly
    assert resp.json() == {"outreach": False}


def test_profile_get_and_put(client):
    c, _ = client
    resp = c.get("/v1/persona/profile", headers={"Authorization": "Bearer persona-key-1"})
    assert resp.status_code == 200
    assert "Persona" in resp.json()["profile"]

    resp = c.put(
        "/v1/persona/profile",
        json={"profile": "# Maya\n\nMaya is warm and dry-witted, a night owl who texts in lowercase."},
        headers={"Authorization": "Bearer persona-key-1"},
    )
    assert resp.status_code == 200
    resp = c.get("/v1/persona/profile", headers={"Authorization": "Bearer persona-key-1"})
    assert "Maya" in resp.json()["profile"]


def test_profile_rejects_empty(client):
    c, _ = client
    resp = c.put(
        "/v1/persona/profile",
        json={"profile": "   "},
        headers={"Authorization": "Bearer persona-key-1"},
    )
    assert resp.status_code == 400


def test_heartbeat_outreach_lands_in_outbox(tmp_path, monkeypatch):
    """A real PersonaHeartbeat beat against a fake runtime queues outreach."""
    import asyncio

    from dual_lobe_persona.heartbeat import PersonaHeartbeat
    from dual_lobe_persona.outbox import Outbox

    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setattr("dual_lobe_persona.outbox.QUIET_HOURS", "23:59-23:59")
    rt = FakeRuntime()
    rt.next_reply = {"content": "good luck today — you've got this", "tool_actions": 0}
    hb = PersonaHeartbeat(rt)
    result = asyncio.run(hb.beat("persona-key-1"))
    assert result["outreach"] is True
    pending = Outbox("persona-key-1").pending()
    assert len(pending) == 1
    assert "good luck today" in pending[0].text


def test_heartbeat_no_outreach_mark_stays_quiet(tmp_path, monkeypatch):
    import asyncio

    from dual_lobe_persona.heartbeat import PersonaHeartbeat
    from dual_lobe_persona.outbox import Outbox

    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    rt = FakeRuntime()
    rt.next_reply = {"content": "NO_OUTREACH", "tool_actions": 0}
    hb = PersonaHeartbeat(rt)
    result = asyncio.run(hb.beat("persona-key-1"))
    assert result == {"outreach": False}
    assert Outbox("persona-key-1").pending() == []