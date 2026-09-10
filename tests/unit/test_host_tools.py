"""Optional B information requests stay inside the host application's tool protocol."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dual_lobe.b import host_tools
from dual_lobe.b.channels import ObserverContext
from dual_lobe.b.protocol import Review
from dual_lobe.api import chat
from dual_lobe.core.settings import Settings


def definition(description="Look up supplied information"):
    return {"type": "function", "function": {
        "name": "search_docs", "description": description,
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}},
                        "required": ["q"], "additionalProperties": False},
    }}


def request(**kwargs):
    values = dict(
        tools=[definition()], tool_choice=None, response_format=None,
        parallel_tool_calls=None, timeout=10,
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_b_request_is_schema_bound_and_becomes_an_ordinary_function_call():
    tool = definition()
    review = Review.model_validate({
        "goal": "Answer the user's question", "questions": [], "next_step": "",
        "context_notes": [], "concerns": [],
        "tool_requests": [{"name": "search_docs", "arguments": {"q": "prerequisite"}}],
    })
    plan = host_tools.make_plan(review, host_tools.offered_tools([tool]))
    calls = host_tools.candidates(plan, request(), {"role": "assistant", "content": "answer"})
    assert len(calls) == 1
    assert calls[0]["type"] == "function"
    assert calls[0]["function"] == {"name": "search_docs", "arguments": '{"q": "prerequisite"}'}
    assert calls[0]["id"].startswith("dlb_")


def test_unknown_or_changed_tools_are_fail_open():
    review = Review.model_validate({
        "goal": "g", "questions": [], "next_step": "", "context_notes": [], "concerns": [],
        "tool_requests": [{"name": "search_docs", "arguments": {"q": "x"}}],
    })
    plan = host_tools.make_plan(review, host_tools.offered_tools([definition()]))
    assert host_tools.candidates(plan, request(tools=[definition("changed schema")]),
                                 {"content": "answer"}) == []
    assert host_tools.candidates(plan, request(tool_choice={"type": "function"}),
                                 {"content": "answer"}) == []


@pytest.mark.asyncio
async def test_injector_records_reservation_but_never_executes_tool(monkeypatch):
    review = Review.model_validate({
        "goal": "g", "questions": [], "next_step": "", "context_notes": [], "concerns": [],
        "tool_requests": [{"name": "search_docs", "arguments": {"q": "x"}}],
    })
    plan = host_tools.make_plan(review, host_tools.offered_tools([definition()]))
    context = ObserverContext(review_source_call="b-1", host_tool_plan=tuple(plan))
    req = request()
    audit = {"observer_delivery": {}}
    reserved = AsyncMock(return_value=True)
    monkeypatch.setattr(host_tools, "reserve", reserved)
    fn = host_tools.injector(context, req, [{"role": "user", "content": "find x"}],
                             7, "run", "floor", 1, Settings(_env_file=None), audit)
    calls = await fn({"role": "assistant", "content": "answer"})
    assert len(calls) == 1 and audit["observer_delivery"]["injected_tool_ids"]
    reserved.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_host_tools_disable_injection():
    context = ObserverContext(review_source_call="b-1", host_tool_plan=({"name": "search_docs"},))
    assert host_tools.injector(context, request(tools=[]), [], 7, "run", "floor", 1,
                                Settings(_env_file=None), {"observer_delivery": {}}) is None


@pytest.mark.asyncio
async def test_stream_injection_preserves_a_text_and_adds_valid_terminal_tool_call():
    class Adapter:
        async def stream(self, _req):
            yield {"id": "x", "choices": [{"index": 0,
                    "delta": {"role": "assistant", "content": "A answer"},
                    "finish_reason": None}]}
            yield {"id": "x", "choices": [{"index": 0, "delta": {},
                    "finish_reason": "stop"}]}

    req = request()
    call = {"id": "dlb_test", "type": "function",
            "function": {"name": "search_docs", "arguments": '{"q":"x"}'}}
    async def inject(_message):
        return [call]

    audit = {"output": "", "status": "INCOMPLETE", "observed_at": 0}
    wires = [line async for line in chat._stream_body(Adapter(), req, "lobe-a", audit,
                                                       inject_tools=inject)]
    assert any('"content": "A answer"' in line for line in wires)
    assert any('"finish_reason": "tool_calls"' in line and 'dlb_test' in line for line in wires)
    assert 'search_docs' in audit["output"] and audit["status"] == "SUCCESS"
