"""Deterministic provider scripts exercise the real loop and HTTP serialization."""
import asyncio
import copy
import json
import time
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dual_lobe.core.settings import Settings
from dual_lobe.director.engine import DirectorLoop, stream
from dual_lobe.director.protocol import begin
from dual_lobe.director.store import SessionConflict
from dual_lobe.provider.adapters import NormalizedRequest

TOOLS = [{"type": "function", "function": {"name": "check", "parameters": {"type": "object"}}}]
CALL = {"id": "call-1", "type": "function", "function": {"name": "check", "arguments": '{"path":"app.py"}'}}
USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
SCOPE = {"floor": "build", "memory_space": "project"}


class MemorySession:
    """Test substitute for the SQL store; deliberately no durability claim."""
    def __init__(self):
        self.payload = {}
        self.released = False
        self.history = []

    async def save(self, token, payload, release=False):
        self.payload = copy.deepcopy(payload)
        self.history.append(copy.deepcopy(payload))
        self.released = release


class ScriptA:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []
        self.closed = False

    async def stream(self, req):
        self.requests.append(copy.deepcopy(req))
        output = self.outputs.pop(0)
        try:
            if isinstance(output, list):
                for chunk in output:
                    yield chunk
                return
            if isinstance(output, Exception):
                raise output
            if output == "WAIT":
                yield {"choices": [{"index": 0, "delta": {"content": "partial"}}]}
                await asyncio.Event().wait()
                return
            if isinstance(output, dict):
                calls = output["tool_calls"]
                for i, call in enumerate(calls):
                    args = call["function"]["arguments"]
                    yield {"choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": i, **call, "function": {"name": call["function"]["name"], "arguments": args[:3]}}]}}]}
                    yield {"choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": i, "function": {"arguments": args[3:]}}]}}]}
                finish = "tool_calls"
            else:
                for part in (output[:3], output[3:]):
                    yield {"choices": [{"index": 0, "delta": {"content": part}}]}
                finish = "stop"
            yield {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
            yield {"choices": [], "usage": USAGE}
        finally:
            self.closed = True


class ScriptB:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.requests = []

    async def buffered(self, req):
        self.requests.append(copy.deepcopy(req))
        decision = self.decisions.pop(0)
        content = decision if isinstance(decision, str) else json.dumps(decision)
        return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], "usage": USAGE}


def loop(a, b, *, state=None, settings=None, store=None):
    settings = settings or Settings(_env_file=None)
    state = state or begin({}, [{"role": "user", "content": "Fix the app and verify it."}], SCOPE, settings)
    store = store or MemorySession()
    compose = AsyncMock(side_effect=lambda m: copy.deepcopy(m))
    return DirectorLoop(state, store, "lease", NormalizedRequest(messages=[], tools=TOOLS),
                        a, b, settings, compose, AsyncMock(), AsyncMock())


def wire_events(wire):
    return [json.loads(line[6:]) for line in wire.splitlines() if line.startswith("data: ") and line[6:] != "[DONE]"]


async def test_visible_real_alternation_preserves_tools_and_one_outer_completion():
    a = ScriptA(["I need another credential.", "The existing credential was valid; the path was wrong."])
    b = ScriptB([{"action": "continue", "message": "Check the path before changing credentials."},
                 {"action": "stop", "message": "The blocker is identified; no further direction needed."}])
    run = loop(a, b)
    wire = "".join([x async for x in stream(run, "lobe-a-director", True)])
    assert "A (turn 1)" in wire and "A (turn 2)" in wire and "B (director)" in wire
    assert len(a.requests) == len(b.requests) == 2
    assert all(r.tools == TOOLS for r in a.requests)
    assert all(r.tools is None and r.tool_choice is None for r in b.requests)
    assert a.requests[1].messages[-1] == {"role": "user", "name": "director_b", "content": "Check the path before changing credentials."}
    assert "I need another credential." in b.requests[0].messages[-1]["content"]
    events = wire_events(wire)
    assert len({e["id"] for e in events}) == 1
    assert sum(c.get("finish_reason") is not None for e in events for c in e["choices"]) == 1
    assert events[-1]["usage"] == {k: 4*v for k, v in USAGE.items()}
    assert wire.count("data: [DONE]") == 1 and run.state["reason"] == "b_stop"
    assert run.compose_a.await_count == 2 and run.record_a.await_count == 2


@pytest.mark.parametrize("delta", [False, True])
async def test_parallel_tool_handoff_and_resume_without_repeating_calls(delta):
    second_call = {**CALL, "id": "call-2"}
    a = ScriptA([{"tool_calls": [CALL, second_call]}, "Two checks succeeded."])
    b = ScriptB([{"action": "stop", "message": "Both supplied results report success."}])
    run = loop(a, b)
    output = []
    async for event in run.events():
        if event["kind"] == "tools":
            assert run.store.released and run.store.payload["status"] == "waiting_tools"
        output.append(event)
    assert output[-1] == {"kind": "end", "finish_reason": "tool_calls"}
    assert not b.requests and len(a.requests) == 1
    results = [{"role": "tool", "tool_call_id": "call-2", "content": "PASS: second"},
               {"role": "tool", "tool_call_id": "call-1", "content": "PASS: first"}]
    state = begin(run.store.payload, results if delta else run.state["wire"] + results,
                  {**SCOPE, "delta": delta}, run.s)
    resumed = loop(a, b, state=state)
    events = [e async for e in resumed.events()]
    assert events[-1]["finish_reason"] == "stop" and len(a.requests) == 2
    assert a.requests[-1].messages[-2:] == results
    assert a.requests[-1].messages[-3]["tool_calls"] == [CALL, second_call]
    assert "A (turn" not in json.dumps(a.requests[-1].messages)
    assert resumed.state["a_calls"] == 2


@pytest.mark.parametrize("results", [[], [{"role": "tool", "tool_call_id": "wrong", "content": "ok"}],
    [{"role": "tool", "tool_call_id": "call-1", "content": "ok"}]*2,
    [{"role": "user", "content": "The tool worked"}]])
async def test_missing_duplicate_forged_or_mismatched_tool_results_cannot_resume(results):
    run = loop(ScriptA([{"tool_calls": [CALL]}]), ScriptB([]))
    _ = [e async for e in run.events()]
    with pytest.raises(SessionConflict):
        begin(run.state, run.state["wire"] + results, SCOPE, run.s)


async def test_budget_survives_tool_boundary_and_no_automatic_tool_replay():
    settings = Settings(_env_file=None, director_max_a_calls=1)
    a = ScriptA([{"tool_calls": [CALL]}])
    run = loop(a, ScriptB([]), settings=settings)
    _ = [e async for e in run.events()]
    state = begin(run.state, run.state["wire"] + [{"role": "tool", "tool_call_id": "call-1", "content": "FAIL"}], SCOPE, settings)
    resumed = loop(a, ScriptB([]), state=state, settings=settings)
    _ = [e async for e in resumed.events()]
    assert len(a.requests) == 1 and resumed.state["reason"] == "call_limit"
    with pytest.raises(SessionConflict):
        begin(resumed.state, resumed.state["wire"], SCOPE, settings)


@pytest.mark.parametrize("b_output", ["not-json", '{"action":"continue"}', '{"action":"continue","message":" "}',
    '{"action":"stop","message":"ok","extra":true}'])
async def test_invalid_b_stops_explicitly_without_an_extra_a_call(b_output):
    a = ScriptA(["First answer"])
    run = loop(a, ScriptB([b_output]))
    events = [e async for e in run.events()]
    assert events[-1]["kind"] == "error" and run.state["status"] == "failed"
    assert not any(e["kind"] == "end" for e in events) and len(a.requests) == 1


@pytest.mark.parametrize("ending", [None, "length", "content_filter"])
async def test_truncated_a_is_never_passed_to_b_as_a_completed_answer(ending):
    a = ScriptA([[{"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": ending}]}]])
    b = ScriptB([])
    run = loop(a, b)
    wire = "".join([e async for e in stream(run, "lobe-a-director", False)])
    assert "director_incomplete" in wire and not b.requests
    assert all(not c.get("finish_reason") for e in wire_events(wire) for c in e.get("choices", []))


async def test_cancellation_closes_upstream_and_marks_session_incomplete():
    a = ScriptA(["WAIT"])
    run = loop(a, ScriptB([]))
    generator = stream(run, "lobe-a-director", False)
    while "partial" not in await anext(generator):
        pass
    waiting = asyncio.create_task(anext(generator))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await generator.aclose()
    assert a.closed and run.state["status"] == "cancelled" and run.store.released


async def test_asgi_send_failure_closes_director_upstream_immediately():
    from dual_lobe.api.director import ClosingStreamingResponse
    from starlette.requests import ClientDisconnect
    a = ScriptA(["WAIT"])
    run = loop(a, ScriptB([]))
    response = ClosingStreamingResponse(stream(run, "lobe-a-director", False))
    async def send(event):
        if b"partial" in event.get("body", b""):
            raise OSError("client gone")
    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send)
    assert a.closed and run.state["status"] == "cancelled"


async def test_a_text_reaches_asgi_client_before_b_runs():
    from dual_lobe.api.director import ClosingStreamingResponse
    sent = asyncio.Event()
    class B(ScriptB):
        async def buffered(self, req):
            assert sent.is_set()
            return await super().buffered(req)
    run = loop(ScriptA(["First answer"]), B([{"action": "stop", "message": "Sufficient"}]))
    async def send(event):
        if b"First" in event.get("body", b"") or b"Fir" in event.get("body", b""):
            sent.set()
    await ClosingStreamingResponse(stream(run, "lobe-a-director", False))(
        {"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send)
    assert sent.is_set() and run.state["reason"] == "b_stop"


async def test_state_write_failure_does_not_expose_executable_tools():
    run = loop(ScriptA([{"tool_calls": [CALL]}]), ScriptB([]))
    original = run.store.save
    async def fail_release(token, payload, release=False):
        if release:
            raise OSError("storage down")
        await original(token, payload)
    run.store.save = fail_release
    events = [e async for e in run.events()]
    assert events[-1]["kind"] == "error"
    assert not any(e["kind"] in ("tools", "end") for e in events)


async def test_deadline_includes_time_awaiting_tools():
    run = loop(ScriptA([]), ScriptB([]))
    run.state["deadline"] = time.time() - 1
    events = [e async for e in run.events()]
    assert run.state["reason"] == "time_limit" and events[-1]["kind"] == "end"
    assert not run.a.requests


def test_new_user_turn_keeps_internal_history_but_starts_fresh_budget():
    settings = Settings(_env_file=None)
    prior = begin({}, [{"role": "user", "content": "goal"}], SCOPE, settings)
    prior.update(status="stopped", a_calls=8)
    prior["transcript"].append({"role": "user", "name": "director_b", "content": "Consider the root cause"})
    prior["wire"].append({"role": "assistant", "content": "A/B labelled output"})
    state = begin(prior, [{"role": "user", "content": "Now explain the fix"}], {**SCOPE, "delta": True}, settings)
    assert state["transcript"][:-1] == prior["transcript"]
    assert state["a_calls"] == 0 and state["cycle_id"] != prior["cycle_id"]
