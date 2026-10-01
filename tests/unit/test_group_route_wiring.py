from types import SimpleNamespace
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request

from dual_lobe.api import chat
from dual_lobe.api.auth import Principal
from dual_lobe.api.schemas import ChatCompletionRequest
from dual_lobe.b.channels import ObserverContext
from dual_lobe.core.settings import Settings


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
    monkeypatch.setattr(chat, "_persist_observation", AsyncMock())
    monkeypatch.setattr(chat.limits, "check_limits", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    adapter = SimpleNamespace(buffered=AsyncMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "result"}, "finish_reason": "stop"}]
    }))
    monkeypatch.setattr(chat, "get_registry", lambda: SimpleNamespace(
        target=lambda _: SimpleNamespace(enabled=True, model="fake", kind="chat_completions"),
        adapter=lambda _: adapter,
    ))
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    return request, Principal(1, "tenant", frozenset()), None, adapter


@pytest.mark.parametrize(
    "model, expected_b_alias",
    [
        ("sawii/dl-bidirectional", "lobe-b"),
        ("sawii/dl-secure", "lobe-b-clinical"),
    ],
)
async def test_public_models_use_existing_b_group_router(request_path, monkeypatch, model, expected_b_alias):
    request, principal, _, _ = request_path
    monkeypatch.setattr(chat, "get_settings", lambda: Settings(
        _env_file=None, recipient_routing_enabled=True, routing_mode="flat",
        shared_memory_enabled=False, default_memory_id=""))
    analysis = SimpleNamespace(should_respond=False, reasoning="addressed to another agent", confidence=0.98)
    route = AsyncMock(return_value=(analysis, True))
    monkeypatch.setattr(chat, "_route_message", route)

    response = await chat.chat_completions(ChatCompletionRequest(
        model=model, messages=[{"role": "user", "content": "Hey John, please handle this."}]),
        request, principal)

    assert response.headers["x-dual-lobe-group-routing"] == "suppressed"
    assert route.await_args.kwargs["model_alias"] == expected_b_alias
    assert route.await_args.kwargs["mode"] == "flat"


async def test_existing_b_group_router_is_skipped_when_disabled(request_path, monkeypatch):
    request, principal, _, adapter = request_path
    monkeypatch.setattr(chat, "get_settings", lambda: Settings(
        _env_file=None, recipient_routing_enabled=False, routing_mode="flat",
        shared_memory_enabled=False, default_memory_id=""))
    route = AsyncMock()
    monkeypatch.setattr(chat, "_route_message", route)

    response = await chat.chat_completions(ChatCompletionRequest(
        model="sawii/dl-bidirectional", messages=[{"role": "user", "content": "Hey John, handle this."}]),
        request, principal)

    assert not route.await_count
    assert response.status_code == 200


async def test_a_b_direct_address_keeps_using_lobe_router_not_group_router(request_path, monkeypatch):
    request, principal, _, _ = request_path
    monkeypatch.setattr(chat, "get_settings", lambda: Settings(
        _env_file=None, recipient_routing_enabled=True, routing_mode="flat",
        shared_memory_enabled=False, default_memory_id=""))
    route = AsyncMock()
    monkeypatch.setattr(chat, "_route_message", route)
    from dual_lobe.bidirectional import handler
    result = JSONResponse({"choices": [{"message": {"content": "B answered."}}]})
    monkeypatch.setattr(handler, "bidirectional_response", AsyncMock(return_value=result))

    response = await chat.chat_completions(ChatCompletionRequest(
        model="sawii/dl-bidirectional", messages=[{"role": "user", "content": "Hey B, answer this."}]),
        request, principal)

    assert not route.await_count
    import json

    assert json.loads(response.body)["choices"][0]["message"]["content"] == "B answered."
