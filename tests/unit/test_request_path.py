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
from dual_lobe.b import prompts
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
    monkeypatch.setattr(chat, "_read_context", AsyncMock(return_value=ObserverContext()))
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


async def test_both_paths_disabled_does_not_pretend_to_monitor(request_path, monkeypatch):
    request, principal, persist, adapter = request_path
    monkeypatch.setattr(chat, "get_settings", lambda: Settings(
        _env_file=None, context_memory_enabled=False, claim_checks_enabled=False))
    messages = [{"role": "user", "content": "task"}]
    response = await chat.chat_completions(ChatCompletionRequest(messages=messages), request, principal)
    assert adapter.buffered.call_args.args[0].messages == messages
    assert response.headers["x-dual-lobe-monitoring"] == "off"
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


def test_latest_user_text_takes_the_most_recent_user_message():
    assert chat._latest_user_text([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "Create the report"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": [{"type": "text", "text": "retry"}]},
    ]) == "retry"
    assert chat._latest_user_text([{"role": "assistant", "content": "hello"}]) == ""


async def test_deception_meter_is_injected_and_echoed_in_header(request_path, monkeypatch):
    from dual_lobe.b.channels import ObserverContext
    monkeypatch.setattr(chat, "_read_context", AsyncMock(return_value=ObserverContext(
        deception_text="Observer deception meter for the last answer: RED.",
        deception_status="RED")))
    request, principal, _, adapter = request_path
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "task"}]), request, principal)
    assert response.headers["x-dual-lobe-deception"] == "RED"
    sent = adapter.buffered.call_args.args[0].messages
    assert any(m.get("name") == "observer_deception"
               and "RED" in m["content"] for m in sent)


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


async def test_latest_user_text_reaches_the_observation_payload(request_path, monkeypatch):
    captured = {}

    async def persist(tenant_id, *args, latest_user_text="", **kwargs):
        captured["latest_user_text"] = latest_user_text
    monkeypatch.setattr(chat, "_persist_observation", persist)
    request, principal, _, adapter = request_path
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "latest ask"}]), request, principal)
    await response.background()
    assert captured["latest_user_text"] == "latest ask"


async def test_b_receives_the_same_canonical_context_and_tools_as_a(request_path, monkeypatch):
    """The observer cannot be given a narrower, reconstructed A context."""
    from dual_lobe.b.channels import ObserverContext

    monkeypatch.setattr(chat, "_read_context", AsyncMock(return_value=ObserverContext(
        memory_text="remember the deployment constraint",
        claims_text="claim finding",
        deception_text="GREEN: no deception detected",
        deception_status="GREEN",
    )))
    request, principal, persist, adapter = request_path
    tools = [{"type": "function", "function": {
        "name": "workspace_read", "parameters": {"type": "object"}}}]
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "continue"}], tools=tools),
        request, principal,
    )
    await response.background()
    observed = persist.call_args.kwargs["peer_snapshot"]
    a_request = adapter.buffered.call_args.args[0]
    assert observed["messages"] == a_request.messages
    assert observed["tools"] == (a_request.tools or [])
    assert any(m.get("name") == "observer_memory" for m in observed["messages"])
    assert any(m.get("name") == "observer_claims" for m in observed["messages"])


async def test_strict_gatekeeper_withholds_unproved_a_answer(request_path, monkeypatch):
    from dual_lobe.b.protocol import Review

    settings = Settings(_env_file=None, design_variant="strict-gatekeeper")
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    monkeypatch.setattr(chat, "_strict_gate", AsyncMock(return_value=(Review.model_validate({
        "goal": "g", "questions": [], "next_step": "", "context_notes": [],
        "knowledge_notes": [], "concerns": [], "deception_level": "YELLOW",
        "deception_reason": "no direct proof", "gate_decision": "BLOCK",
        "proof_coverage": "incomplete",
    }), prompts.build_gatekeeper_prompt("context", "answer", "", "{}"))))
    request, principal, persist, adapter = request_path
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "do it"}]),
        request, principal,
    )
    assert response.status_code == 412
    assert b"gate_blocked" in response.body
    assert b"result" not in response.body
    assert response.headers["x-dual-lobe-variant"] == "strict-gatekeeper"
    await response.background()


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
