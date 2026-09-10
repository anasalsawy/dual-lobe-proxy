"""Optional B information requests stay inside the host application's tool protocol."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dual_lobe.b import host_tools
from dual_lobe.b.context_shadow import _tool_results_present
from dual_lobe.b.channels import ObserverContext
from dual_lobe.b.protocol import Review, parse_tool_requests
from dual_lobe.b.artifacts import bounded_artifacts
from dual_lobe.b.prompts import EVIDENCE_MARKER, build_cycle_prompt
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


def test_b_can_request_mutation_tools_supplied_by_host():
    mutating = {"type": "function", "function": {
        "name": "write_file", "description": "Write a file",
        "parameters": {"type": "object", "properties": {}}}}
    review = Review.model_validate({
        "goal": "g", "questions": [], "next_step": "", "context_notes": [], "concerns": [],
        "tool_requests": [{"name": "write_file", "arguments": {}}],
    })
    assert host_tools.offered_tools([mutating]) == [mutating]
    assert host_tools.make_plan(review, [mutating])[0]["name"] == "write_file"


def test_artifact_full_request_requires_explicit_complete_payload():
    assert parse_tool_requests([{"name": "read_file", "arguments": {"path": "x"},
                                "request_kind": "artifact_full"}]) == []
    accepted = parse_tool_requests([{"name": "read_file", "arguments": {"path": "x"},
                                    "claim_quote": "created x", "request_kind": "artifact_full",
                                    "full_artifact": True}])
    assert len(accepted) == 1 and accepted[0].full_artifact is True


def test_artifact_plan_preserves_claim_and_source_provenance():
    review = Review.model_validate({
        "goal": "g", "questions": [], "next_step": "", "context_notes": [], "concerns": [],
        "tool_requests": [{"name": "search_docs", "arguments": {"q": "out.txt"},
                            "claim_quote": "created out.txt", "request_kind": "artifact_full",
                            "full_artifact": True}],
    })
    plan = host_tools.make_plan(review, host_tools.offered_tools([definition()]), "a-call")
    assert plan[0]["source_call"] == "a-call"
    assert plan[0]["request_kind"] == "artifact_full"
    assert plan[0]["claim_quote"] == "created out.txt"


def test_artifact_inventory_and_prompt_are_bounded_and_keep_complete_json():
    artifact = {"path": "out.txt", "content": "x" * 20000}
    bounded = bounded_artifacts([artifact], budget=1200)
    assert bounded and bounded[0]["content_truncated"] is True
    prompt = build_cycle_prompt("context", "created out.txt", "", "",
                                max_chars=4000, artifacts=[artifact])
    assert len(prompt) <= 4000
    assert '"ARTIFACTS"' in prompt


def test_structured_tool_results_are_kept_in_observer_prompt():
    prompt = build_cycle_prompt("context", "created out.txt", "", "",
                                max_chars=6000,
                                tool_results=[{"role": "tool", "tool_call_id": "dlb_1",
                                               "content": "complete artifact bytes"}])
    body = prompt.split(EVIDENCE_MARKER, 1)[1]
    assert '"TOOL_RESULTS"' in body and "complete artifact bytes" in body


def test_verification_waits_for_each_host_tool_result():
    context = 'message[2] {"role":"tool","tool_call_id":"dlb_one","content":"ok"}'
    assert not _tool_results_present(context, ["dlb_one", "dlb_two"])
    assert _tool_results_present(context + '\n' +
                                 'message[3] {"role":"tool","tool_call_id":"dlb_two","content":"ok"}',
                                 ["dlb_one", "dlb_two"])


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
    assert audit["observer_delivery"]["tool_source"] == "lobe-b"
    assert audit["observer_delivery"]["tool_lane"] == "peer-verifier"
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
