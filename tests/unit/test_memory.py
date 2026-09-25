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
    async def load(tenant, space, messages):
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


def test_inject_shared_memory_lands_after_system_block_and_is_null_safe():
    original = [{"role": "system", "content": "rules"},
                {"role": "developer", "content": "dev rules"},
                {"role": "user", "content": "hi"}]
    assert memory.inject_shared_memory(original, None) is original
    result = memory.inject_shared_memory(original, "slice text")
    assert result[2] == {"role": "user", "name": "shared_memory", "content": "slice text"}
    assert result[3] is original[2] and original[2]["content"] == "hi"
    without_system = [{"role": "user", "content": "hi"}]
    assert memory.inject_shared_memory(without_system, "s")[0]["name"] == "shared_memory"


def test_query_words_are_unique_and_capped_at_twelve():
    words = memory.query_words(" ".join(f"word{i}x" for i in range(20)))
    assert len(words) <= 12
    assert len(words) == len(set(words))


async def test_search_memory_short_circuits_without_space_or_words():
    assert await memory.search_memory(1, None, "deployment target") is None
    assert await memory.search_memory(1, "project", "") is None


def test_record_background_is_skipped_without_space_or_choices():
    assert chat._record_background(1, None, "run", [], {}) is None
    assert chat._record_background(1, "project", "run", [],
                                   {"error": {"message": "x"}}) is None
    assert chat._record_background(1, "project", "run", [],
                                   {"choices": [{"message": None}]}) is None
    assert chat._record_background(1, "project", "run", [],
                                   {"choices": [{"message": {"role": "assistant"}}]}) is not None


async def test_record_background_records_response_messages(monkeypatch):
    calls = []

    async def record(*args):
        calls.append(args)

    monkeypatch.setattr(chat, "record_memory", record)
    messages = [{"role": "user", "content": "task"}]
    data = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    task = chat._record_background(1, "project", "run-1", messages, data)
    await task()
    assert len(calls) == 1
    tenant, space, run_id, call_id, sent, responses = calls[0]
    assert (tenant, space, run_id) == (1, "project", "run-1")
    assert sent is messages and responses == [data["choices"][0]["message"]]


async def test_dialogue_branch_loads_injects_and_records_memory(request_path, monkeypatch):
    request, principal, _, _ = request_path
    request = Request({"type": "http", "method": "POST", "path": "/",
                       "headers": [(b"x-dl-memory-id", b"project")]})
    monkeypatch.setattr(chat, "load_memory",
                        AsyncMock(return_value=memory.LoadedMemory("project", "slice text", (7,))))
    recorded = []

    async def record(tenant, space, run_id, call_id, messages, responses):
        recorded.append((space, responses))

    monkeypatch.setattr(chat, "record_memory", record)
    captured = {}

    async def fake_coauthor(payload, run_id, tenant_id, alias, *, shared_text=None):
        captured["shared_text"] = shared_text
        return ({"id": "c", "choices": [{"message": {"role": "assistant", "content": "A: ok"},
                                          "finish_reason": "stop"}]},
                {"X-Dual-Lobe-Coauthor": "on"})

    monkeypatch.setattr("dual_lobe.coauthor.handler.coauthor_response", fake_coauthor)
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "task"}],
                              model="sawii/dialogue"), request, principal)
    assert captured["shared_text"] == "slice text"
    assert response.headers["x-dual-lobe-memory-space"] == "project"
    assert response.headers["x-dual-lobe-shared-entries"] == "1"
    assert response.background is not None
    await response.background()
    assert recorded and recorded[0][0] == "project"
    assert recorded[0][1][0]["content"] == "A: ok"


async def test_dialogue_branch_load_failure_fails_open(request_path, monkeypatch):
    request, principal, _, _ = request_path
    request = Request({"type": "http", "method": "POST", "path": "/",
                       "headers": [(b"x-dl-memory-id", b"project")]})
    monkeypatch.setattr(chat, "load_memory", AsyncMock(side_effect=OSError("db down")))
    recorded = []

    async def record(*args):
        recorded.append(args)

    monkeypatch.setattr(chat, "record_memory", record)
    captured = {}

    async def fake_coauthor(payload, run_id, tenant_id, alias, *, shared_text=None):
        captured["shared_text"] = shared_text
        return ({"id": "c", "choices": [{"message": {"role": "assistant", "content": "ok"},
                                          "finish_reason": "stop"}]}, {})

    monkeypatch.setattr("dual_lobe.coauthor.handler.coauthor_response", fake_coauthor)
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "task"}],
                              model="sawii/dialogue"), request, principal)
    assert response.status_code == 200
    assert captured["shared_text"] is None
    assert response.headers["x-dual-lobe-shared-entries"] == "0"
    await response.background()
    assert recorded and recorded[0][1] == "project"


async def test_gated_branch_passes_shared_memory_and_records_in_background(request_path, monkeypatch):
    request, principal, _, _ = request_path
    request = Request({"type": "http", "method": "POST", "path": "/",
                       "headers": [(b"x-dl-memory-id", b"project")]})
    monkeypatch.setattr(chat, "load_memory",
                        AsyncMock(return_value=memory.LoadedMemory("project", "slice text", (3,))))
    recorded = []

    async def record(tenant, space, run_id, call_id, messages, responses):
        recorded.append((space, run_id, responses))

    monkeypatch.setattr(chat, "record_memory", record)
    captured = {}

    async def fake_gated(payload, run_id, tenant_id, alias, *,
                         shared_text=None, shared_space=None):
        captured.update(shared_text=shared_text, shared_space=shared_space)
        return ({"id": "g", "choices": [{"message": {"role": "assistant", "content": "answer"},
                                          "finish_reason": "stop"}]},
                {"X-Dual-Lobe-Gated": "on"})

    monkeypatch.setattr("dual_lobe.gated.handler.gated_response", fake_gated)
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "task"}],
                              model="sawii/dl-gated"), request, principal)
    assert captured == {"shared_text": "slice text", "shared_space": "project"}
    assert response.headers["x-dual-lobe-memory-space"] == "project"
    assert response.headers["x-dual-lobe-shared-entries"] == "1"
    assert response.background is not None
    await response.background()
    assert recorded and recorded[0][0] == "project"
    assert recorded[0][2][0]["content"] == "answer"


async def test_gated_branch_streams_with_record_background(request_path, monkeypatch):
    request, principal, _, _ = request_path
    request = Request({"type": "http", "method": "POST", "path": "/",
                       "headers": [(b"x-dl-memory-id", b"project")]})
    monkeypatch.setattr(chat, "load_memory",
                        AsyncMock(return_value=memory.LoadedMemory("project", "s", (1,))))
    recorded = []

    async def record(*args):
        recorded.append(args)

    monkeypatch.setattr(chat, "record_memory", record)

    async def fake_gated(payload, run_id, tenant_id, alias, *,
                         shared_text=None, shared_space=None):
        return ({"id": "g", "choices": [{"message": {"role": "assistant", "content": "answer"},
                                          "finish_reason": "stop"}]}, {})

    monkeypatch.setattr("dual_lobe.gated.handler.gated_response", fake_gated)
    response = await chat.chat_completions(
        ChatCompletionRequest(messages=[{"role": "user", "content": "task"}],
                              model="sawii/dl-gated", stream=True), request, principal)
    assert response.background is not None
    body = "".join([chunk async for chunk in response.body_iterator])
    assert "[DONE]" in body
    await response.background()
    assert recorded


async def test_coauthor_injects_shared_memory_without_mutating_canonical(monkeypatch):
    from dual_lobe.coauthor import handler as coauthor
    from dual_lobe.core.settings import Settings
    monkeypatch.setattr(coauthor, "get_settings", lambda: Settings(_env_file=None))
    prompts: list[str] = []

    async def fake_call_b(system, user, *, max_tokens=None):
        prompts.append(user)
        if len(prompts) == 1:
            return {"user_for_a": "task", "to_a": "", "b_only": ""}
        return {"action": "PASS", "reply_to_a": "", "reply_to_user": "",
                "coauthor_to_a": "", "coauthor_to_user": "",
                "verification": {"level": "GREEN", "to_a": "",
                                 "to_user": "checked", "rationale": "",
                                 "concerns": []}}

    monkeypatch.setattr(coauthor, "_call_b", fake_call_b)
    a_messages_seen = {}

    async def fake_buffered(req):
        a_messages_seen["messages"] = req.messages
        return {"choices": [{"message": {"role": "assistant", "content": "draft"}}]}

    monkeypatch.setattr(coauthor, "get_registry",
                        lambda: SimpleNamespace(adapter=lambda _: SimpleNamespace(buffered=fake_buffered)))
    canonical = [{"role": "system", "content": "rules"},
                 {"role": "user", "content": "task"}]
    payload = {"messages": canonical, "temperature": 0}
    out, headers = await coauthor.coauthor_response(
        payload, "run-1", 1, "sawii/dialogue", shared_text="slice text")
    assert "choices" in out and headers["X-Dual-Lobe-Coauthor"] == "on"
    assert canonical == [{"role": "system", "content": "rules"},
                         {"role": "user", "content": "task"}]
    assert "shared_memory" in prompts[0]
    assert "slice text" in prompts[0]
    injected = next(m for m in a_messages_seen["messages"]
                    if m.get("name") == "shared_memory")
    assert injected["content"] == "slice text"
    assert a_messages_seen["messages"][-1]["content"] == "task"
