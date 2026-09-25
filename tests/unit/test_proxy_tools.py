"""Proxy-owned tools: schemas, inline execution, one-continuation round."""
import json
from types import SimpleNamespace

import pytest

from dual_lobe.core.settings import Settings
from dual_lobe.api import chat as _chat  # noqa: F401  (anchors import order)
from dual_lobe.gated import handler as gh
from dual_lobe.proxy import tools as pt


def _handoff_b_response():
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps({
        "deception_level": "GREEN", "meter_rationale": "fine", "concerns": [],
        "unverified": [], "tool_review": {"verdict": "none", "issue": ""},
        "next_step": "", "missing": [], "widen": [], "memory_query": "",
    })}, "finish_reason": "stop"}]}


def _setup(monkeypatch, a_replies, *, proxy_enabled=True, b_json=None):
    """Patch the gated handler with scripted A replies and a fixed B."""
    settings = Settings(_env_file=None, proxy_tools_enabled=proxy_enabled)
    monkeypatch.setattr(gh, "get_settings", lambda: settings)
    a_reqs = []
    replies = list(a_replies)

    async def a_buffered(req):
        a_reqs.append(req)
        if not replies:
            raise AssertionError("unexpected extra A call")
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def b_buffered(req):
        if b_json is None:
            return _handoff_b_response()
        return {"choices": [{"message": {"role": "assistant",
                                         "content": json.dumps(b_json)},
                             "finish_reason": "stop"}]}

    monkeypatch.setattr(gh, "get_registry", lambda: SimpleNamespace(
        adapter=lambda name: SimpleNamespace(
            buffered=a_buffered if name == "lobe-a" else b_buffered)))
    return a_reqs


def _payload(**extra):
    payload = {"messages": [{"role": "user", "content": "do the task"}],
               "temperature": 0, "max_tokens": 100}
    payload.update(extra)
    return payload


def _proxy_call(name, args, call_id="call-1"):
    return {"choices": [{"message": {"role": "assistant", "content": None,
                                      "tool_calls": [{"id": call_id, "type": "function",
                                                      "function": {"name": name,
                                                                   "arguments": json.dumps(args)}}]},
                         "finish_reason": "tool_calls"}]}


def _final(text="final answer"):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


# ── schemas ─────────────────────────────────────────────────────────


def test_schemas_expose_exactly_the_three_proxy_tools():
    schemas = pt.proxy_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert names == ["proxy_memory_search", "proxy_delegate", "proxy_consult"]
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["function"]["parameters"]["required"]


def test_strip_proxy_calls_keeps_client_calls():
    message = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "proxy_delegate", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}},
    ]}
    cleaned = pt.strip_proxy_calls(message)
    assert [c["function"]["name"] for c in cleaned["tool_calls"]] == ["get_weather"]
    only_proxy = pt.strip_proxy_calls(_proxy_call("proxy_consult", {"question": "q"})["choices"][0]["message"])
    assert "tool_calls" not in only_proxy


# ── inline execution ────────────────────────────────────────────────


async def test_memory_search_executes_deterministic_fts(monkeypatch):
    calls = {}

    async def fake_search(tenant_id, space, query, *, limit=4, budget=1600):
        calls.update(tenant=tenant_id, space=space, query=query)
        return "stored history: Windows"

    monkeypatch.setattr("dual_lobe.state.memory.search_memory", fake_search)
    used: dict[str, int] = {}
    result = await pt.execute_proxy_call(
        {"function": {"name": "proxy_memory_search", "arguments": '{"query": "deployment target"}'}},
        tenant_id=7, space="project", messages=[], used=used,
        caps={"proxy_memory_search": 2})
    assert result == "stored history: Windows"
    assert calls == {"tenant": 7, "space": "project", "query": "deployment target"}
    assert used == {"proxy_memory_search": 1}


async def test_memory_search_miss_returns_explicit_no_match(monkeypatch):
    async def fake_search(*args, **kwargs):
        return None

    monkeypatch.setattr("dual_lobe.state.memory.search_memory", fake_search)
    result = await pt.execute_proxy_call(
        {"function": {"name": "proxy_memory_search", "arguments": '{"query": "nothing"}'}},
        tenant_id=1, space="s", messages=[], used={}, caps={"proxy_memory_search": 2})
    assert result == "No matching stored history."


async def test_delegate_and_consult_call_b_and_stay_inline(monkeypatch):
    seen = []

    async def fake_b(system, user, max_tokens):
        seen.append((system, max_tokens, user))
        return "B deliverable" if system == pt.DELEGATE_SYSTEM else "B advice"

    monkeypatch.setattr(pt, "_call_b_text", fake_b)
    messages = [{"role": "user", "content": "do the task"}]
    used: dict[str, int] = {}
    caps = {"proxy_delegate": 1, "proxy_consult": 1}
    delegate = await pt.execute_proxy_call(
        {"function": {"name": "proxy_delegate", "arguments": '{"task": "write the parser"}'}},
        tenant_id=1, space="s", messages=messages, used=used, caps=caps)
    consult = await pt.execute_proxy_call(
        {"function": {"name": "proxy_consult", "arguments": '{"question": "what next?"}'}},
        tenant_id=1, space="s", messages=messages, used=used, caps=caps)
    assert delegate == "B deliverable" and consult == "B advice"
    assert used == {"proxy_delegate": 1, "proxy_consult": 1}
    assert seen[0][1] == pt.DELEGATE_MAX_TOKENS and seen[1][1] == pt.CONSULT_MAX_TOKENS
    assert "do the task" in seen[0][2] and "write the parser" in seen[0][2]
    assert "what next?" in seen[1][2]


async def test_caps_block_second_call_without_executing(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("B must not be called over cap")

    monkeypatch.setattr(pt, "_call_b_text", boom)
    used = {"proxy_delegate": 1}
    result = await pt.execute_proxy_call(
        {"function": {"name": "proxy_delegate", "arguments": '{"task": "again"}'}},
        tenant_id=1, space="s", messages=[], used=used,
        caps={"proxy_delegate": 1})
    assert "Per-turn limit reached" in result
    assert used == {"proxy_delegate": 1}


async def test_failures_never_raise(monkeypatch):
    used: dict[str, int] = {}
    caps = {"proxy_memory_search": 2, "proxy_delegate": 1, "proxy_consult": 1}
    bad_json = await pt.execute_proxy_call(
        {"function": {"name": "proxy_memory_search", "arguments": "{not json"}},
        tenant_id=1, space="s", messages=[], used=used, caps=caps)
    assert "invalid arguments" in bad_json

    async def raise_b(*args, **kwargs):
        raise ConnectionError("B down")

    monkeypatch.setattr(pt, "_call_b_text", raise_b)
    failed = await pt.execute_proxy_call(
        {"function": {"name": "proxy_delegate", "arguments": '{"task": "x"}'}},
        tenant_id=1, space="s", messages=[], used=used, caps=caps)
    assert "failed (ConnectionError)" in failed


# ── one-continuation round ──────────────────────────────────────────


async def test_resolve_returns_input_when_no_proxy_calls():
    a_data = _final("plain")
    out, used = await gh._resolve_proxy_tools(
        a_data=a_data, enriched_messages=[], payload={}, tenant_id=1,
        space=None, run_id="r", a_adapter=None, s=Settings(_env_file=None))
    assert out is a_data and used == {}


async def test_resolve_runs_proxy_then_one_continuation(monkeypatch):
    async def fake_search(*args, **kwargs):
        return "memory hits"

    monkeypatch.setattr("dual_lobe.state.memory.search_memory", fake_search)
    settings = Settings(_env_file=None)
    a_reqs = []
    replies = [_final("answer after memory")]

    async def a_buffered(req):
        a_reqs.append(req)
        return replies.pop(0)

    a_data = _proxy_call("proxy_memory_search", {"query": "target"})
    enriched = [{"role": "system", "content": "rules"},
                {"role": "user", "content": "do the task"}]
    out, used = await gh._resolve_proxy_tools(
        a_data=a_data, enriched_messages=enriched,
        payload={"tools": [{"type": "function", "function": {"name": "get_weather"}}]},
        tenant_id=1, space="project", run_id="r",
        a_adapter=SimpleNamespace(buffered=a_buffered), s=settings)

    assert out["choices"][0]["message"]["content"] == "answer after memory"
    assert used == {"proxy_memory_search": 1}
    assert len(a_reqs) == 1
    cont = a_reqs[0]
    # exchange: assistant proxy call, then tool result, at the tail
    assert cont.messages[-2]["role"] == "assistant"
    assert cont.messages[-2]["tool_calls"][0]["function"]["name"] == "proxy_memory_search"
    assert cont.messages[-1]["role"] == "tool"
    assert "memory hits" in cont.messages[-1]["content"]
    # continuation carries the CLIENT tools only — no proxy re-entry
    client_names = [t["function"]["name"] for t in (cont.tools or [])]
    assert "proxy_memory_search" not in client_names
    assert client_names == ["get_weather"]


async def test_resolve_falls_back_with_proxy_calls_stripped(monkeypatch):
    async def fake_search(*args, **kwargs):
        return "memory hits"

    monkeypatch.setattr("dual_lobe.state.memory.search_memory", fake_search)
    settings = Settings(_env_file=None)

    async def a_buffered(req):
        raise TimeoutError("continuation down")

    a_data = _proxy_call("proxy_memory_search", {"query": "target"})
    out, used = await gh._resolve_proxy_tools(
        a_data=a_data, enriched_messages=[{"role": "user", "content": "x"}],
        payload={}, tenant_id=1, space="s", run_id="r",
        a_adapter=SimpleNamespace(buffered=a_buffered), s=settings)
    message = out["choices"][0]["message"]
    assert "tool_calls" not in message
    assert message["content"] == ""
    assert used == {"proxy_memory_search": 1}


# ── handler integration ─────────────────────────────────────────────


async def test_gated_turn_executes_proxy_search_invisible_to_client(monkeypatch):
    async def fake_search(*args, **kwargs):
        return "stored history: Windows"

    monkeypatch.setattr("dual_lobe.state.memory.search_memory", fake_search)
    a_reqs = _setup(monkeypatch, [
        _proxy_call("proxy_memory_search", {"query": "deployment target"}),
        _final("The deployment target is Windows."),
    ])
    data, headers = await gh.gated_response(
        _payload(), "run-proxy-1", 1, "sawii/dl-gated",
        shared_text=None, shared_space="project")
    assert data["choices"][0]["message"]["content"].startswith(
        "The deployment target is Windows.")
    assert "proxy_memory_search" not in json.dumps(data)
    assert headers["X-Dual-Lobe-Proxy-Tools"] == "proxy_memory_searchx1"
    first, second = a_reqs
    first_names = [t["function"]["name"] for t in (first.tools or [])]
    assert set(first_names) == {"proxy_memory_search", "proxy_delegate", "proxy_consult"}
    assert second.tools is None  # client sent no tools; continuation gets none
    assert "stored history: Windows" in second.messages[-1]["content"]


async def test_gated_turn_without_proxy_calls_is_untouched(monkeypatch):
    a_reqs = _setup(monkeypatch, [_final("plain answer")])
    data, headers = await gh.gated_response(
        _payload(), "run-proxy-2", 1, "sawii/dl-gated",
        shared_text=None, shared_space=None)
    assert data["choices"][0]["message"]["content"].startswith("plain answer")
    assert "X-Dual-Lobe-Proxy-Tools" not in headers
    assert len(a_reqs) == 1  # no continuation


async def test_proxy_tools_disabled_sends_no_schemas(monkeypatch):
    a_reqs = _setup(monkeypatch, [_final("plain")], proxy_enabled=False)
    await gh.gated_response(_payload(), "run-proxy-3", 1, "sawii/dl-gated",
                            shared_text=None, shared_space=None)
    assert a_reqs[0].tools is None


# ── B downstream tool calls ─────────────────────────────────────────


def test_extract_b_tool_calls_allowlists_and_normalizes():
    allowed = {"get_weather"}
    calls = gh._extract_b_tool_calls(
        {"tool_calls": [
            {"name": "get_weather", "arguments": {"city": "Paris"}},
            {"name": "proxy_memory_search", "arguments": {"query": "x"}},
            {"name": "not_offered", "arguments": "{}"},
            {"name": "get_weather", "arguments": "{broken"},
        ]},
        allowed)
    assert len(calls) == 1
    call = calls[0]
    assert call["type"] == "function"
    assert call["id"].startswith("b-")
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}


def test_extract_b_tool_calls_respects_limit():
    raw = {"tool_calls": [{"name": f"t{i}", "arguments": {}} for i in range(4)]}
    assert len(gh._extract_b_tool_calls(raw, {f"t{i}" for i in range(4)})) == 2


def test_extract_b_tool_calls_ignores_malformed_contract():
    assert gh._extract_b_tool_calls("not a dict", {"t"}) == []
    assert gh._extract_b_tool_calls({"tool_calls": "nope"}, {"t"}) == []


def test_merge_appends_and_fixes_finish_reason():
    a_data = _final("here you go")
    added = [{"id": "b-1", "type": "function",
              "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]
    out, n = gh._merge_b_tool_calls(a_data, added)
    assert n == 1
    choice = out["choices"][0]
    assert choice["message"]["content"] == "here you go"
    assert [c["function"]["name"] for c in choice["message"]["tool_calls"]] == ["get_weather"]
    assert choice["finish_reason"] == "tool_calls"


def test_merge_dedupes_exact_duplicate_of_a_call():
    a_data = _proxy_call("get_weather", {"b": 2, "a": 1})
    b_calls = [{"id": "b-9", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"a": 1, "b": 2}'}}]
    out, n = gh._merge_b_tool_calls(a_data, b_calls)
    assert n == 0
    assert len(out["choices"][0]["message"]["tool_calls"]) == 1


def test_both_downstream_contracts_mention_tool_calls():
    from dual_lobe.gated.prompts import (
        DOWNSTREAM_CONTRACT, DOWNSTREAM_CONTRACT_HANDOFF)
    assert '"tool_calls"' in DOWNSTREAM_CONTRACT
    assert '"tool_calls"' in DOWNSTREAM_CONTRACT_HANDOFF


_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "city weather",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    },
}


def _b_with_tool_calls(*, name="get_weather", arguments=None):
    arguments = arguments if arguments is not None else {"city": "Paris"}
    return {
        "deception_level": "GREEN", "meter_rationale": "fine", "concerns": [],
        "assist": "", "tool_calls": [{"name": name, "arguments": arguments}],
        "unverified": [], "tool_review": {"verdict": "none", "issue": ""},
        "next_step": "", "missing": [], "widen": [], "memory_query": "",
    }


async def test_gated_turn_merges_b_tool_calls_and_header(monkeypatch):
    _setup(monkeypatch, [_final("kicking off")], b_json=_b_with_tool_calls())
    data, headers = await gh.gated_response(
        _payload(tools=[_WEATHER_TOOL]), "run-btools-1", 1, "sawii/dl-gated",
        shared_text=None, shared_space=None)
    choice = data["choices"][0]
    assert choice["message"]["content"].startswith("kicking off")
    assert [c["function"]["name"] for c in choice["message"]["tool_calls"]] == ["get_weather"]
    assert choice["finish_reason"] == "tool_calls"
    assert headers["X-Dual-Lobe-B-Tool-Calls"] == "1"
    assert "proxy_delegate" not in json.dumps(choice)


async def test_gated_turn_drops_b_request_for_unoffered_tool(monkeypatch):
    _setup(monkeypatch, [_final("plain")],
           b_json=_b_with_tool_calls(name="not_offered"))
    data, headers = await gh.gated_response(
        _payload(tools=[_WEATHER_TOOL]), "run-btools-2", 1, "sawii/dl-gated",
        shared_text=None, shared_space=None)
    assert "tool_calls" not in data["choices"][0]["message"]
    assert "X-Dual-Lobe-B-Tool-Calls" not in headers
