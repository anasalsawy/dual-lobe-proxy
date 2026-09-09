"""Exercise actual response delivery ordering without a database or provider."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from dual_lobe.api import chat
from dual_lobe.api.auth import Principal
from dual_lobe.api.schemas import ChatCompletionRequest
from dual_lobe.core.settings import Settings
from dual_lobe.provider.adapters import ChatCompletionsAdapter, NormalizedRequest, ProviderTarget


@pytest.fixture
def request_path(monkeypatch):
    settings = Settings(_env_file=None, rollout_stage="context")
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    @asynccontextmanager
    async def session(*args):
        yield SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(chat, "tenant_session", session)
    monkeypatch.setattr(chat.repo, "get_or_create_run", AsyncMock(return_value=SimpleNamespace(
        id="81c93c4c-e7d5-47c6-8e45-e0a859f677bc", goal="Do useful work",
    )))
    monkeypatch.setattr(chat, "_read_notes", AsyncMock(return_value=(None, "no_current_review")))
    persist = AsyncMock()
    monkeypatch.setattr(chat, "_persist_observation", persist)
    monkeypatch.setattr(chat.limits, "check_limits", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    provider = AsyncMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "result"}, "finish_reason": "stop"}]
    })
    adapter = SimpleNamespace(buffered=provider)
    monkeypatch.setattr(chat, "get_registry", lambda: SimpleNamespace(
        target=lambda _: SimpleNamespace(enabled=True, model="fake", kind="chat_completions"),
        adapter=lambda _: adapter,
    ))
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    return request, Principal(1, "tenant", frozenset()), persist, adapter


async def test_buffered_body_sent_before_observation_persistence(request_path):
    request, principal, persist, adapter = request_path
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "task"}]), request, principal,
    )
    assert not persist.called
    seen = []
    async def send(message):
        seen.append(message["type"])
        assert not persist.called
    await response({"type": "http"}, AsyncMock(), send)
    assert seen == ["http.response.start", "http.response.body"]
    assert persist.call_count == 1
    assert b"result" in response.body


async def test_bypass_does_not_inject_or_enqueue_b(request_path):
    request, principal, persist, adapter = request_path
    request = Request({"type": "http", "headers": [(b"x-dual-lobe-mode", b"bypass")],
                       "method": "POST", "path": "/"})
    messages = [{"role": "user", "content": "task"}]
    response = await chat.chat_completions(ChatCompletionRequest(messages=messages), request, principal)
    assert adapter.buffered.call_args.args[0].messages == messages
    await response.background()
    assert persist.call_args.args[-1] is False


async def test_image_input_is_explicitly_disabled(request_path):
    from fastapi import HTTPException
    request, principal, _, _ = request_path
    with pytest.raises(HTTPException) as exc:
        await chat.chat_completions(ChatCompletionRequest(messages=[
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
        ]), request, principal)
    assert exc.value.status_code == 400


async def test_actual_asgi_stream_yields_before_provider_finishes(request_path):
    request, principal, persist, adapter = request_path
    released = asyncio.Event()
    async def source(req):
        yield {"choices": [{"index": 0, "delta": {"content": "first"}, "finish_reason": None}]}
        await released.wait()
        yield {"choices": [{"index": 0, "delta": {"content": "last"}, "finish_reason": "stop"}]}
    adapter.stream = source
    response = await chat.chat_completions(ChatCompletionRequest(
        messages=[{"role": "user", "content": "task"}], stream=True,
    ), request, principal)
    chunks = []
    async def send(message):
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            assert not persist.called
            if b"first" in message.get("body", b""):
                assert not released.is_set()
                released.set()
    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    await asyncio.wait_for(response(scope, AsyncMock(), send), .5)
    assert b"first" in chunks[0] and b"last" in chunks[1]
    assert persist.call_count == 1


@pytest.mark.parametrize("wire", [
    b'data: not-json\n\n', b'data: {"error": {"message": "upstream failure"}}\n\n',
    b'data: {"choices": []}', b'data: []\n\n',
])
async def test_invalid_sse_never_silently_succeeds(monkeypatch, wire):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=wire)
    )) as client:
        monkeypatch.setattr("dual_lobe.provider.adapters.get_http_client", lambda: client)
        adapter = ChatCompletionsAdapter(ProviderTarget("a", "https://example.invalid/v1", "key", "model"))
        with pytest.raises(ValueError):
            _ = [c async for c in adapter.stream(NormalizedRequest(messages=[], stream=True))]


async def test_cancelled_stream_closes_upstream():
    closed = asyncio.Event()
    async def source(req):
        try:
            yield {"choices": [{"index": 0, "delta": {"content": "first"}}]}
            await asyncio.Event().wait()
        finally:
            closed.set()
    generator = chat._stream_body(SimpleNamespace(stream=source),
                                  NormalizedRequest(messages=[], timeout=1), "a",
                                  {"output": "", "status": "INCOMPLETE"})
    await anext(generator)
    await generator.aclose()
    assert closed.is_set()


def test_unknown_request_fields_are_not_silently_dropped():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ChatCompletionRequest(messages=[], unknown_option=True)


def test_original_goal_is_not_a_long_system_prompt():
    assert chat._original_goal([
        {"role": "system", "content": "rules" * 10000},
        {"role": "user", "content": "Create the report"},
        {"role": "user", "content": "retry"},
    ]) == "Create the report"


async def test_tenant_context_is_transaction_local(monkeypatch):
    from dual_lobe.core import engine
    session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    @asynccontextmanager
    async def factory():
        yield session
    monkeypatch.setattr(engine, "rls_session_factory", lambda: factory)
    async with engine.tenant_session(17):
        assert "set_config('app.tenant_id', :tenant, true)" in str(session.execute.call_args.args[0])
        assert session.execute.call_args.args[1] == {"tenant": "17"}
        assert not session.commit.called
