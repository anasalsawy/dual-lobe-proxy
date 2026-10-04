"""Tests for the persona runtime facade.

These tests exercise the FastAPI facade contract without requiring the
openhands-sdk dependency: the runtime's chat() is monkeypatched with a
fake that records calls. The SDK-backed runtime paths (workspace
isolation, key hashing) are tested separately with the SDK installed.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import dual_lobe_persona.api as api


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.result = {"content": "I checked it — here is what I found.", "tool_actions": 3}
        self.error: Exception | None = None
        self.delay = 0.0

    async def chat(self, persona_key: str, message: str, session: str = "default") -> dict:
        self.calls.append((persona_key, message, session))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result

    def shutdown(self) -> None:
        pass


@pytest.fixture()
def fake_runtime():
    rt = FakeRuntime()
    api.app.state.runtime = rt
    with TestClient(api.app) as client:
        yield client, rt


def test_health(fake_runtime):
    client, _ = fake_runtime
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_models_lists_persona_model(fake_runtime):
    client, _ = fake_runtime
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert "sawii/dl-persona" in ids


def test_chat_requires_bearer(fake_runtime):
    client, _ = fake_runtime
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "sawii/dl-persona", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


def test_chat_rejects_empty_bearer(fake_runtime):
    client, _ = fake_runtime
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "sawii/dl-persona", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer  "},
    )
    assert resp.status_code == 401


def test_chat_forwards_last_user_message_with_key(fake_runtime):
    client, rt = fake_runtime
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "sawii/dl-persona",
            "messages": [
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "latest question"},
            ],
        },
        headers={"Authorization": "Bearer persona-key-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "I checked it — here is what I found."
    assert rt.calls == [("persona-key-1", "latest question", "default")]


def test_chat_session_header_scopes_conversation(fake_runtime):
    client, rt = fake_runtime
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "sawii/dl-persona", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer persona-key-1", "X-DL-Session": "project-x"},
    )
    assert resp.status_code == 200
    assert rt.calls[0][2] == "project-x"


def test_chat_rejects_streaming_honestly(fake_runtime):
    client, _ = fake_runtime
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "sawii/dl-persona",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers={"Authorization": "Bearer persona-key-1"},
    )
    assert resp.status_code == 400
    assert "Streaming" in resp.json()["detail"]


def test_chat_rejects_request_without_user_message(fake_runtime):
    client, _ = fake_runtime
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "sawii/dl-persona", "messages": [{"role": "assistant", "content": "hi"}]},
        headers={"Authorization": "Bearer persona-key-1"},
    )
    assert resp.status_code == 400


def test_chat_rejects_oversized_message(fake_runtime, monkeypatch):
    monkeypatch.setattr(api, "MAX_MESSAGE_BYTES", 10)
    api.app.state.runtime = FakeRuntime()
    with TestClient(api.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "sawii/dl-persona", "messages": [{"role": "user", "content": "x" * 100}]},
            headers={"Authorization": "Bearer persona-key-1"},
        )
    assert resp.status_code == 413


def test_chat_upstream_failure_is_502_without_internals(fake_runtime):
    client, rt = fake_runtime
    rt.error = RuntimeError("upstream 401 invalid_api_key sk-secret123")
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "sawii/dl-persona", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer persona-key-1"},
    )
    assert resp.status_code == 502
    assert "invalid_api_key" not in resp.text
    assert "sk-secret123" not in resp.text


def test_chat_timeout_is_504(fake_runtime, monkeypatch):
    monkeypatch.setattr(api, "REQUEST_TIMEOUT", 0.05)
    rt = FakeRuntime()
    rt.delay = 1.0
    api.app.state.runtime = rt
    with TestClient(api.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "sawii/dl-persona", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer persona-key-1"},
        )
    assert resp.status_code == 504


def test_chat_never_leaks_key_in_response(fake_runtime):
    client, rt = fake_runtime
    rt.result = {"content": "", "tool_actions": 0}
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "sawii/dl-persona", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer persona-key-1"},
    )
    assert resp.status_code == 200
    assert "persona-key-1" not in resp.text