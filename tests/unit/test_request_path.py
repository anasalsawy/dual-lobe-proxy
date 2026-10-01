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
from dual_lobe.b.channels import ObserverContext
from dual_lobe.api.auth import Principal
from dual_lobe.api.schemas import ChatCompletionRequest
from dual_lobe.core.settings import Settings
from dual_lobe.provider.adapters import ChatCompletionsAdapter, NormalizedRequest, ProviderTarget
from dual_lobe.gated import handler as gated_handler


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
    monkeypatch.setattr(chat, "_read_context", AsyncMock(return_value=ObserverContext()))
    persist = AsyncMock()
    monkeypatch.setattr(chat, "_persist_observation", persist)
    monkeypatch.setattr(chat.limits, "check_limits", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    provider = AsyncMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "result"}, "finish_reason": "stop"}]
    })
    adapter = SimpleNamespace(buffered=provider)
    registry = SimpleNamespace(
        target=lambda _: SimpleNamespace(enabled=True, model="fake", kind="chat_completions"),
        adapter=lambda _: adapter)
    monkeypatch.setattr(chat, "get_registry", lambda: registry)
    monkeypatch.setattr(gated_handler, "get_registry", lambda: registry)
    monkeypatch.setattr(gated_handler, "_call_b_json", AsyncMock(return_value={
        "deception_level": "GREEN", "meter_rationale": "No unsupported claims identified.",
        "concerns": [],
    }))
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    return request, Principal(1, "tenant", frozenset()), persist, adapter


async def test_buffered_gated_request_returns_verified_answer(request_path):
    request, principal, persist, _ = request_path
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "task"}]), request, principal)
    assert response.status_code == 200
    assert response.headers["x-dual-lobe-gated"] == "on"
    assert response.headers["x-dual-lobe-meter"] == "GREEN"
    assert b"result" in response.body
    assert not persist.called


async def test_legacy_bypass_header_does_not_disable_the_gate(request_path):
    request, principal, _, adapter = request_path
    request = Request({"type": "http", "headers": [(b"x-dual-lobe-mode", b"bypass")],
                       "method": "POST", "path": "/"})
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "task"}]), request, principal)
    assert response.headers["x-dual-lobe-gated"] == "on"
    sent = adapter.buffered.call_args.args[0].messages
    assert any(m.get("role") == "system" and "anti-deception observer" in m.get("content", "")
               for m in sent)


async def test_observer_feature_flags_do_not_disable_inline_verification(request_path, monkeypatch):
    request, principal, _, _ = request_path
    monkeypatch.setattr(chat, "get_settings", lambda: Settings(
        _env_file=None, context_memory_enabled=False, claim_checks_enabled=False,
        deception_meter_enabled=False))
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "task"}]), request, principal)
    assert response.headers["x-dual-lobe-gated"] == "on"
    assert response.headers["x-dual-lobe-meter"] == "GREEN"

async def test_image_input_is_explicitly_disabled(request_path):
    from fastapi import HTTPException
    request, principal, _, _ = request_path
    with pytest.raises(HTTPException) as exc:
        await chat.chat_completions(ChatCompletionRequest(messages=[
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
        ]), request, principal)
    assert exc.value.status_code == 400


async def test_bidirectional_sse_drops_json_content_length(request_path, monkeypatch):
    from starlette.responses import JSONResponse

    async def fake_bidirectional(*args, **kwargs):
        return JSONResponse({"choices": [{"message": {"role": "assistant", "content": "answer"},
                                           "finish_reason": "stop"}]})

    monkeypatch.setattr("dual_lobe.bidirectional.handler.bidirectional_response", fake_bidirectional)
    request, principal, _, _ = request_path
    response = await chat.chat_completions(ChatCompletionRequest(
        model="sawii/dl-secure", stream=True,
        messages=[{"role": "user", "content": "Hey B, answer this."}],
    ), request, principal)

    assert "content-length" not in response.headers
    assert response.headers["content-type"].startswith("text/event-stream")
    body = "".join([chunk async for chunk in response.body_iterator])
    assert "answer" in body and "[DONE]" in body


async def test_bidirectional_json_recomputes_content_length_after_metadata(request_path, monkeypatch):
    from starlette.responses import JSONResponse

    async def fake_bidirectional(*args, **kwargs):
        return JSONResponse(
            {"choices": [{"message": {"role": "assistant", "content": "answer"},
                          "finish_reason": "stop"}]},
            headers={"Content-Length": "1"},
        )

    monkeypatch.setattr("dual_lobe.bidirectional.handler.bidirectional_response", fake_bidirectional)
    request, principal, _, _ = request_path
    response = await chat.chat_completions(ChatCompletionRequest(
        model="sawii/dl-secure", stream=False,
        messages=[{"role": "user", "content": "Hey B, answer this."}],
    ), request, principal)

    assert int(response.headers["content-length"]) == len(response.body)
    assert b"memory_space" in response.body


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
    joined = b"".join(chunks)
    assert b"first" in joined and b"last" in joined and b"[DONE]" in joined
    assert not persist.called


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


async def test_old_lobe_alias_is_rejected_as_a_public_model(request_path):
    from fastapi import HTTPException

    request, principal, _, _ = request_path
    with pytest.raises(HTTPException, match="unsupported model"):
        await chat.chat_completions(
            ChatCompletionRequest(model="lobe-a", messages=[{"role": "user", "content": "hi"}]),
            request, principal,
        )


def test_original_goal_is_not_a_long_system_prompt():
    assert chat._original_goal([
        {"role": "system", "content": "rules" * 10000},
        {"role": "user", "content": "Create the report"},
        {"role": "user", "content": "retry"},
    ]) == "Create the report"


def test_latest_user_text_takes_the_most_recent_user_message():
    assert chat._latest_user_text([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "Create the report"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": [{"type": "text", "text": "retry"}]},
    ]) == "retry"
    assert chat._latest_user_text([{"role": "assistant", "content": "hello"}]) == ""



async def test_read_context_retries_once_then_succeeds(monkeypatch):
    monkeypatch.setattr(chat, "get_settings",
                        lambda: Settings(_env_file=None, rollout_stage="context"))
    @asynccontextmanager
    async def session(*args):
        yield SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(chat, "tenant_session", session)
    attempts = 0

    async def flaky(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("transient")
        return {"payload": {"oversight_status": "orphaned"}}
    monkeypatch.setattr(chat.repo, "latest_b_state", flaky)
    context = await chat._read_context(1, "run", "", 1)
    assert attempts == 2
    assert context.status not in ("state_unavailable",)



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
