import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from dual_lobe.api import chat
from dual_lobe.api.schemas import ChatCompletionRequest
from dual_lobe.b.channels import ObserverContext
from dual_lobe.provider.adapters import NormalizedRequest
from dual_lobe.state import memory
from tests.unit.test_request_path import request_path


def test_shared_memory_is_separate_untrusted_data_and_keeps_tool_adjacency():
    messages = [{"role": "system", "content": "App rules"},
                {"role": "user", "content": "Continue"},
                {"role": "assistant", "tool_calls": [{"id": "t"}]},
                {"role": "tool", "tool_call_id": "t", "content": "result"}]
    note = memory.compose("project", "User wants Windows support", [], 6000)
    result = chat._effective_messages(messages, ObserverContext(), True, shared_text=note)
    entry = next(m for m in result if m.get("name") == "shared_memory")
    assert entry["role"] == "user" and "not verified" in entry["content"]
    assert result[-2:] == messages[-2:] and messages[1]["content"] == "Continue"


@pytest.mark.parametrize("space", ["", "../another", "space with spaces", "x"*129, "x\nheader"])
def test_memory_space_names_reject_ambiguous_or_header_unsafe_values(space):
    with pytest.raises(ValueError):
        memory.validate_space(space)


def test_context_budget_preserves_notebook_and_valid_json():
    entries = [{"id": i, "excerpt": '"\\\n' * 4000} for i in range(8)]
    result = memory.compose("shared", "Durable constraints", entries, 6000)
    assert len(result) <= 6000
    data = json.loads(result.split("\n", 1)[1])
    assert data["pinned_notebook"] == "Durable constraints"
    assert "context omitted" in result


async def test_notebook_rejects_oversize_encoding_before_database_access():
    with pytest.raises(ValueError):
        await memory.MemoryStore(1, "space").notebook("\x01"*2000)


async def test_second_app_gets_saved_memory_without_sending_old_history(request_path, monkeypatch):
    request, principal, _, adapter = request_path
    saved = []
    async def record(tenant, space, run, call, messages, responses):
        saved.append({"tenant": tenant, "space": space, "messages": messages, "responses": responses})
    async def load(tenant, space, messages, **kwargs):
        matching = [e for e in saved if e["tenant"] == tenant and e["space"] == space]
        entries = [{"id": i, "excerpt": json.dumps(e)} for i, e in enumerate(matching)]
        return memory.LoadedMemory(space, memory.compose(space, "", entries, 6000), tuple(range(len(entries))))
    monkeypatch.setattr(chat, "record_memory", record)
    monkeypatch.setattr(chat, "load_memory", load)
    def source(run):
        return Request({"type": "http", "method": "POST", "path": "/", "headers": [
            (b"x-dl-memory-id", b"project"), (b"x-dl-run-id", run.encode())]})
    first = await chat.chat_completions(ChatCompletionRequest(messages=[
        {"role": "user", "content": "The deployment target is Windows."}]), source("app-one"), principal)
    assert first.status_code == 200 and len(saved) == 1
    second = await chat.chat_completions(ChatCompletionRequest(messages=[
        {"role": "user", "content": "What target should this agent use?"}]), source("app-two"), principal)
    received = adapter.buffered.call_args.args[0].messages
    assert "Windows" in next(m["content"] for m in received if m.get("name") == "shared_memory")
    assert received[-1]["content"] == "What target should this agent use?"
    assert second.headers["x-dual-lobe-shared-entries"] == "1"
    assert first.headers["x-dual-lobe-shared-entries"] == "0"


async def test_selected_memory_read_failure_does_not_silently_drop_memory(request_path, monkeypatch):
    _, principal, _, adapter = request_path
    request = Request({"type": "http", "headers": [(b"x-dl-memory-id", b"project")]})
    monkeypatch.setattr(chat, "load_memory", AsyncMock(side_effect=OSError()))
    with pytest.raises(HTTPException) as exc:
        await chat.chat_completions(ChatCompletionRequest(messages=[{"role": "user", "content": "go"}]), request, principal)
    assert exc.value.status_code == 503 and not adapter.buffered.called


async def test_memory_commits_before_terminal_stream_event_and_never_claims_saved_on_failure():
    async def source(req):
        yield {"choices": [{"index": 0, "delta": {"content": "early"}}]}
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    saved = False
    async def record(messages):
        nonlocal saved
        assert messages[0]["content"] == "early"
        saved = True
    audit = {"output": ""}
    async for event in chat._stream_body(SimpleNamespace(stream=source), NormalizedRequest(messages=[], timeout=1), "a", audit, record):
        if "early" in event:
            assert not saved
        if '"finish_reason": "stop"' in event:
            assert saved
    assert audit["status"] == "SUCCESS"
    broken = AsyncMock(side_effect=OSError("storage down"))
    wire = "".join([e async for e in chat._stream_body(SimpleNamespace(stream=source),
        NormalizedRequest(messages=[], timeout=1), "a", {"output": ""}, broken)])
    assert '"finish_reason": "stop"' not in wire and '"error"' in wire


def test_search_terms_do_not_contain_sql_or_user_supplied_query_operators():
    query = memory.query_text([{"role": "user", "content": "database'; DROP TABLE memory_entries; --"}])
    assert "'" not in query and ";" not in query and "--" not in query


async def test_server_default_shares_memory_without_extra_app_headers(request_path, monkeypatch):
    request, principal, _, _ = request_path
    from dual_lobe.core.settings import Settings
    monkeypatch.setattr(chat, "get_settings", lambda: Settings(_env_file=None, default_memory_id="main"))
    load = AsyncMock(return_value=memory.LoadedMemory("main"))
    record = AsyncMock()
    monkeypatch.setattr(chat, "load_memory", load)
    monkeypatch.setattr(chat, "record_memory", record)
    await chat.chat_completions(ChatCompletionRequest(messages=[{"role": "user", "content": "go"}]), request, principal)
    assert load.call_args.args[1] == record.call_args.args[1] == "main"
