import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from dual_lobe.api import auth, chat, director
from dual_lobe.api.schemas import ChatCompletionRequest
from dual_lobe.core.settings import Settings
from tests.unit.test_director import ScriptA, ScriptB, CALL, TOOLS


@pytest.fixture
def director_api(monkeypatch):
    app = FastAPI()
    app.include_router(chat.router)
    principal = auth.Principal(1, "tenant", frozenset({auth.SCOPE_INFERENCE_INVOKE}))
    for route in chat.router.routes:
        for dep in getattr(getattr(route, "dependant", None), "dependencies", []):
            app.dependency_overrides[dep.call] = lambda: principal
    settings = Settings(_env_file=None, b_enabled=False)
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    @asynccontextmanager
    async def session(*_):
        yield SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(chat, "tenant_session", session)
    monkeypatch.setattr(chat.repo, "get_or_create_run", AsyncMock(return_value=SimpleNamespace(
        id="86c93c4c-e7d5-47c6-8e45-e0a859f677bc", goal="Fix it")))
    monkeypatch.setattr(chat, "_persist_observation", AsyncMock())
    monkeypatch.setattr(chat.limits, "check_limits", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    payloads, locks = {}, set()
    class Store:
        def __init__(self, tenant, run):
            self.key = (tenant, run)
        async def acquire(self, seconds):
            if self.key in locks:
                from dual_lobe.director.store import SessionConflict
                raise SessionConflict("already running")
            locks.add(self.key)
            return "token", copy.deepcopy(payloads.get(self.key, {}))
        async def save(self, token, payload, release=False):
            payloads[self.key] = copy.deepcopy(payload)
            if release:
                locks.discard(self.key)
    monkeypatch.setattr(director, "DirectorStore", Store)
    a, b = ScriptA([]), ScriptB([])
    registry = SimpleNamespace(target=lambda _: SimpleNamespace(model="test-model", enabled=True, kind="chat_completions"),
                                adapter=lambda alias: a if alias == "lobe-a" else b)
    monkeypatch.setattr(chat, "get_registry", lambda: registry)
    return app, a, b, payloads, settings


async def test_http_tool_results_resume_same_session_and_label_b(director_api):
    app, a, b, states, _ = director_api
    a.outputs = [{"tool_calls": [CALL]}, "The check failed.", "I corrected the earlier claim: tests remain failing."]
    b.decisions = [{"action": "continue", "message": "Do not call the task complete; report the failing check."},
                   {"action": "stop", "message": "The remaining failure is now explicit."}]
    body = {"model": "lobe-a-director", "messages": [{"role": "user", "content": "Fix and verify"}], "tools": TOOLS}
    headers = {"X-DL-Run-ID": "conversation"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
        first = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert first.status_code == 200 and first.json()["choices"][0]["finish_reason"] == "tool_calls"
        assert not b.requests
        body["messages"] += [first.json()["choices"][0]["message"], {"role": "tool", "tool_call_id": "call-1", "content": "FAIL: one test"}]
        # Standard SDK serialization can include optional null fields.
        body["messages"][-2].update(refusal=None, audio=None, function_call=None)
        second = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert second.status_code == 200
        text = second.json()["choices"][0]["message"]["content"]
        assert "B (director)" in text and "A (turn 3)" in text
        assert len(a.requests) == 3
        # Retrying the consumed tool result cannot re-execute A or duplicate B.
        repeated = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert repeated.status_code == 409 and len(a.requests) == 3


@pytest.mark.parametrize("headers,extra,status", [
    ({}, {}, 400),
    ({"X-DL-Run-ID": "x"}, {"response_format": {"type": "json_object"}}, 400),
    ({"X-DL-Run-ID": "x", "X-Dual-Lobe-Mode": "bypass"}, {}, 400),
    ({"X-DL-Run-ID": "x", "X-DL-History": "guess"}, {}, 400),
])
async def test_unsupported_director_requests_are_explicit(director_api, headers, extra, status):
    app, a, b, _, _ = director_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
        response = await client.post("/v1/chat/completions", json={
            "model": "lobe-a-director", "messages": [{"role": "user", "content": "task"}], **extra}, headers=headers)
    assert response.status_code == status and not a.requests and not b.requests


async def test_header_invocation_and_delta_only_followup(director_api):
    app, a, b, _, _ = director_api
    a.outputs = ["First answer", "Follow-up answer"]
    b.decisions = [{"action": "stop", "message": "Enough for now"}]*2
    headers = {"X-DL-Run-ID": "x", "X-Dual-Lobe-Mode": "director", "X-DL-History": "delta"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
        for message in ("Explain the plan", "Expand step two"):
            headers["X-DL-Request-ID"] = message
            result = await client.post("/v1/chat/completions", json={
                "model": "lobe-a", "messages": [{"role": "user", "content": message}], "stream": True}, headers=headers)
            assert result.status_code == 200 and "B (director)" in result.text
    assert a.requests[-1].messages[-1]["content"] == "Expand step two"
    assert any(m.get("content") == "First answer" for m in a.requests[-1].messages)


async def test_delta_request_id_prevents_retry_as_a_new_user_turn(director_api):
    app, a, b, _, _ = director_api
    a.outputs = ["answer"]
    b.decisions = [{"action": "stop", "message": "Enough"}]
    headers = {"X-DL-Run-ID": "x", "X-DL-History": "delta", "X-DL-Request-ID": "unique-turn"}
    body = {"model": "lobe-a-director", "messages": [{"role": "user", "content": "go"}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
        assert (await client.post("/v1/chat/completions", json=body, headers=headers)).status_code == 200
        assert (await client.post("/v1/chat/completions", json=body, headers=headers)).status_code == 409
    assert len(a.requests) == 1


async def test_error_sse_has_no_successful_terminal_event(director_api):
    app, a, b, _, _ = director_api
    a.outputs = ["answer"]
    b.decisions = ["invalid"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
        result = await client.post("/v1/chat/completions", json={
            "model": "lobe-a-director", "messages": [{"role": "user", "content": "task"}], "stream": True},
            headers={"X-DL-Run-ID": "x"})
    assert result.status_code == 200 and "director_incomplete" in result.text
    assert '"finish_reason": "stop"' not in result.text
