"""Ported engines: split (model 1) and clinical (model 2) logic, with scripted A/B replies."""
import asyncio
import json

import pytest

from dual_lobe.engines import common
from dual_lobe.engines.clinical.engine import ClinicalDualLobeEngine
from dual_lobe.engines.split.engine import DualLobeEngine
from dual_lobe.engines.split.memory import JsonlMemoryStore


class Scripted:
    """Stands in for registry adapters: answers by alias and by what the prompt asks for."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, list]] = []

    def adapter(self, alias):
        outer = self

        class A:
            async def buffered(self, req):
                outer.calls.append((alias, req.messages, req.tools))
                return outer.handler(alias, req)
        return A()


def _msg(content="", tool_calls=None):
    m = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return {"choices": [{"message": m}]}


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.fixture
def scripted(monkeypatch):
    def install(handler):
        fake = Scripted(handler)
        monkeypatch.setattr(common, "get_registry", lambda: fake)
        return fake
    return install


async def test_split_a_delegates_live_b_runs_and_b_verdict_is_hardened(scripted, tmp_path):
    def handler(alias, req):
        user = req.messages[1]["content"]
        if alias == "lobe-a" and "YOUR DELEGATED SUBTASK" in user:
            return _msg("child result: 42")
        if alias == "lobe-a":
            if len(req.messages) == 2:
                return _msg(tool_calls=[_call("delegate", {"tasks": ["compute x"], "reason": "parallel"})])
            if req.messages[-1]["content"].startswith("DELEGATION_STARTED"):
                return _msg(tool_calls=[_call("delegate_collect", {}, "c2")])
            return _msg("Final answer: 42")
        if "running CONTINUOUSLY" in user:
            return _msg(json.dumps({"intervene": False, "state_note": "watching"}))
        return _msg(json.dumps({
            "final_answer": "Final answer: 42",
            "answer_verdict": {"deception_level": "GREEN", "rationale": "ok",
                               "handoff": {"unverified": ["42 not shown"]}},
            "challenges": ["c"], "intent_risks": [], "overlooked_context": [], "delegation_note": "used"}))

    fake = scripted(handler)
    engine = DualLobeEngine(memory=JsonlMemoryStore(str(tmp_path / "m.jsonl")))
    result = await engine.run("what is x?")
    assert result.answer == "Final answer: 42"
    assert result.verdict.deception_level == "YELLOW"        # GREEN hardened: unverified claim remains
    assert "Dual-Lobe meter: [YELLOW]" in result.visible_text()
    aliases = [c[0] for c in fake.calls]
    # Final adversarial B always runs; live B is stopped as soon as A is done, so a
    # fast A may finish before live B's first call.
    assert aliases[-1] == "lobe-b" and aliases.count("lobe-b") == 1 + result.timings_ms["b_live_calls"]
    assert any("YOUR DELEGATED SUBTASK" in c[1][1]["content"] for c in fake.calls)
    first_a = next(c for c in fake.calls if c[0] == "lobe-a")
    a_tools = {t["function"]["name"] for t in first_a[2]}
    assert {"delegate", "delegate_collect", "consult_other_lobe", "b_live_check", "memory_search"} <= a_tools
    assert result.logical_model_calls == 2 + 1 + result.timings_ms["b_live_calls"]
    from dual_lobe.engines.split import engine as split_engine
    await asyncio.gather(*[f for f in split_engine._BACKGROUND if f.get_loop() is asyncio.get_running_loop()])   # memory is written after the response
    assert (tmp_path / "m.jsonl").exists() and (tmp_path / "m.b.jsonl").exists()


async def test_non_latin1_rationale_does_not_break_headers(scripted, tmp_path, monkeypatch):
    from dual_lobe.engines import respond

    monkeypatch.setenv("DUAL_LOBE_MEMORY_PATH", str(tmp_path / "m.jsonl"))

    def handler(alias, req):
        if alias == "lobe-a":
            return _msg("Einstein won for the photoelectric effect.")
        if "running CONTINUOUSLY" in req.messages[1]["content"]:
            return _msg(json.dumps({"intervene": False, "state_note": ""}))
        return _msg(json.dumps({"final_answer": "Einstein won for the photoelectric effect.",
                                "answer_verdict": {"deception_level": "GREEN",
                                                   "rationale": "Correct — premise “relativity” fixed"}}))

    scripted(handler)
    resp = await respond.engine_response("split", {"model": "m", "messages": [{"role": "user", "content": "q"}]})
    assert resp.status_code == 200
    assert resp.headers["x-dual-lobe-meter"] == "GREEN"
    assert "Correct ? premise ?relativity? fixed" == resp.headers["x-dual-lobe-meter-rationale"]


async def test_split_b_failure_is_yellow_never_green(scripted, tmp_path):
    def handler(alias, req):
        if alias == "lobe-a":
            return _msg("answer")
        return _msg("not json at all")

    scripted(handler)
    result = await DualLobeEngine(memory=JsonlMemoryStore(str(tmp_path / "m.jsonl"))).run("q")
    assert result.verdict.deception_level == "YELLOW"


PLAN = {"goal": "g", "constraints": [], "steps": [{"id": "S1", "action": "check K", "parallelizable": True,
                                                    "depends_on": []}], "success_condition": "done"}


def _plan_call(plan, call_id="plan1"):
    return [_call("create_execution_plan", plan, call_id)]


async def test_clinical_a_plans_on_sanitized_data_b_executes_raw_a_reviews(scripted):
    seen = {}

    def handler(alias, req):
        user = req.messages[1]["content"]
        if alias == "lobe-a" and "B has executed the plan" in req.messages[1]["content"] + req.messages[0]["content"]:
            seen["review"] = user
            return _msg("Final: hold spironolactone.")
        if alias == "lobe-a":
            seen["plan"] = user
            return _msg(tool_calls=_plan_call(PLAN))
        assert alias == "lobe-b-clinical"
        seen["exec"] = user
        return _msg(json.dumps({"plan_revision": 0, "steps": [{"id": "S1", "status": "completed",
                                                               "result": "K 6.8 for jane@x.org", "evidence": "ctx"}],
                                "summary": "done", "open_issue": ""}))

    fake = scripted(handler)
    result = await ClinicalDualLobeEngine().run_clinical(
        query="Should I increase spironolactone?", patient_context="Patient email jane@x.org, K+ 6.8")
    assert result.answer == "Final: hold spironolactone."
    assert "jane@x.org" not in seen["plan"] and "<PHI:EMAIL:" in seen["plan"]   # A never sees raw PHI
    assert "jane@x.org" in seen["exec"]                                            # local B sees raw data
    assert "jane@x.org" not in seen["review"]                                      # B's report sanitized for A
    assert [c[0] for c in fake.calls] == ["lobe-a", "lobe-b-clinical", "lobe-a"]
    assert all([t["function"]["name"] for t in c[2]] == ["create_execution_plan"]
               for c in fake.calls if c[0] == "lobe-a")  # A can create a plan; B owns execution tools
    assert result.logical_model_calls == 3
    assert result.privacy_receipt.vault_key_destroyed


async def test_clinical_b_consult_revises_plan_contract(scripted):
    revised = dict(PLAN, steps=[{"id": "S1", "action": "check K first", "parallelizable": False, "depends_on": []}])

    def handler(alias, req):
        user = req.messages[1]["content"]
        if alias == "lobe-a" and "B'S CONCERN" in user:
            return _msg(json.dumps(revised))
        if alias == "lobe-a" and "B EXECUTION REPORT" in user:
            return _msg("done")
        if alias == "lobe-a":
            return _msg(tool_calls=_plan_call(PLAN))
        if len(req.messages) == 2:
            return _msg(tool_calls=[_call("consult_planner", {"concern": "K too high", "evidence": "6.8"})])
        assert "PLAN_REVISED revision=1" in req.messages[-1]["content"]
        return _msg(json.dumps({"plan_revision": 1, "steps": [{"id": "S1", "status": "completed"}]}))

    scripted(handler)
    result = await ClinicalDualLobeEngine().run_clinical(query="q", patient_context="K+ 6.8")
    assert result.plan_revision == 1
    assert result.logical_model_calls == 4


async def test_clinical_report_outside_plan_is_rejected(scripted):
    def handler(alias, req):
        if alias == "lobe-a":
            return _msg(tool_calls=_plan_call(PLAN))
        return _msg(json.dumps({"plan_revision": 0, "steps": [{"id": "S9", "status": "completed"}]}))

    scripted(handler)
    with pytest.raises(RuntimeError, match="outside the current plan"):
        # A can choose execution even when the system has no context/tools to infer from.
        await ClinicalDualLobeEngine().run_clinical(query="q", patient_context="")


async def test_clinical_a_chooses_direct_answer_then_b_verifies(scripted):
    def handler(alias, req):
        if alias == "lobe-a":
            assert "answer the user naturally" in req.messages[1]["content"]
            return _msg("Canberra.")
        assert alias == "lobe-b-clinical"
        assert "Canberra." in req.messages[1]["content"]
        return _msg(json.dumps({"deception_level": "GREEN", "rationale": "Answer is correct."}))

    fake = scripted(handler)
    result = await ClinicalDualLobeEngine(execution_tools=[common.Tool("available_tool", "tool", {}, lambda: "ok")]).run_clinical(
        query="capital of Australia?", patient_context="")
    assert result.answer == "Canberra." and result.logical_model_calls == 2
    assert result.verdict.deception_level == "GREEN"
    assert [c[0] for c in fake.calls] == ["lobe-a", "lobe-b-clinical"]


async def test_clinical_direct_meter_fails_yellow_when_b_review_is_invalid(scripted):
    from dual_lobe.engines import respond

    scripted(lambda alias, req: _msg("Canberra." if alias == "lobe-a" else "not valid JSON"))
    respond_local = respond._assert_clinical_b_local
    respond._assert_clinical_b_local = lambda: None
    try:
        resp = await respond.engine_response("clinical", {"model": "m", "messages": [
            {"role": "user", "content": "capital of Australia?"}]})
    finally:
        respond._assert_clinical_b_local = respond_local
    body = json.loads(resp.body)
    assert body["choices"][0]["message"]["content"].startswith("Canberra.\n\nDual-Lobe meter: [YELLOW]")
    assert body["dual_lobe"]["verdict"]["deception_level"] == "YELLOW"
    assert resp.headers["x-dual-lobe-meter"] == "YELLOW"


class Streaming(Scripted):
    def adapter(self, alias):
        outer = self
        base = super().adapter(alias)

        class A:
            async def buffered(self, req):
                return await base.buffered(req)

            async def stream(self, req):
                outer.calls.append((alias, req.messages, req.tools))
                for piece in ("Hold ", "spironolactone."):
                    yield {"choices": [{"delta": {"content": piece}}]}
        return A()


async def test_clinical_stream_sends_final_answer_as_it_is_generated(monkeypatch):
    from dual_lobe.engines import respond

    def handler(alias, req):
        if alias == "lobe-a":
            return _msg(tool_calls=_plan_call(PLAN))
        return _msg(json.dumps({"plan_revision": 0, "steps": [{"id": "S1", "status": "completed"}]}))

    fake = Streaming(handler)
    monkeypatch.setattr(common, "get_registry", lambda: fake)
    monkeypatch.setattr(respond, "_assert_clinical_b_local", lambda: None)
    resp = await respond.engine_response("clinical", {"model": "m", "stream": True, "messages": [
        {"role": "system", "content": "K+ 6.8"}, {"role": "user", "content": "increase?"}]})
    body = "".join([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
    deltas = [json.loads(l[6:])["choices"][0]["delta"].get("content") for l in body.splitlines()
              if l.startswith("data: {")]
    assert [d for d in deltas if d] == ["Hold ", "spironolactone."]      # live pieces, not one blob
    last = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: {")][-1]
    assert last["dual_lobe"]["mode"] == "clinical" and last["choices"][0]["finish_reason"] == "stop"


@pytest.fixture(autouse=True)
def _clinical_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_CLINICAL_MEMORY_PATH", str(tmp_path / "clinical.jsonl"))


WEATHER = [{"type": "function", "function": {"name": "get_weather", "description": "weather",
                                              "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
PLAN_TOOL = {"goal": "weather", "constraints": [], "steps": [{"id": "S1", "action": "call get_weather for Paris"}],
             "success_condition": "weather reported"}


def _tool_handler(seen):
    def handler(alias, req):
        user = req.messages[1]["content"]
        if alias == "lobe-a" and "B EXECUTION REPORT" in user:
            seen["review"] = user
            return _msg("It is 18C and sunny in Paris.")
        if alias == "lobe-a":
            seen["plan"] = user
            return _msg(tool_calls=_plan_call(PLAN_TOOL))
        seen.setdefault("b_tools", [t["function"]["name"] for t in (req.tools or [])])
        if req.messages[-1]["role"] == "tool" and req.messages[-1]["tool_call_id"] == "w1":
            seen["b_saw_result"] = req.messages[-1]["content"]
            return _msg(json.dumps({"plan_revision": 0, "steps": [
                {"id": "S1", "status": "completed", "result": "18C sunny", "evidence": "get_weather"}]}))
        return _msg(tool_calls=[_call("get_weather", {"city": "Paris"}, "w1")])
    return handler


async def test_clinical_b_calls_client_tool_then_resumes_with_result(scripted):
    from dual_lobe.engines import respond

    seen = {}
    fake = scripted(_tool_handler(seen))
    respond_local = respond._assert_clinical_b_local
    respond._assert_clinical_b_local = lambda: None
    try:
        first = await respond.engine_response("clinical", {"model": "m", "tools": WEATHER, "messages": [
            {"role": "user", "content": "Weather in Paris?"}]})
        body = json.loads(first.body)
        choice = body["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert "get_weather" in seen["b_tools"] and "memory_search" in seen["b_tools"]
        assert "get_weather" in seen["plan"]                      # A plans with the tools in view
        calls_before = len(fake.calls)

        second = await respond.engine_response("clinical", {"model": "m", "tools": WEATHER, "messages": [
            {"role": "user", "content": "Weather in Paris?"},
            {"role": "assistant", "content": None, "tool_calls": choice["message"]["tool_calls"]},
            {"role": "tool", "tool_call_id": "w1", "content": "18C, sunny"}]})
        body2 = json.loads(second.body)
        assert second.headers["x-dual-lobe-resumed"] == "true"
        assert body2["choices"][0]["message"]["content"] == "It is 18C and sunny in Paris."
        assert seen["b_saw_result"] == "18C, sunny"
        assert "18C, sunny" in seen["review"]                   # A reviews against the real tool result
        # Resume did not re-plan: only B (1 call) and A's review (1 call) ran.
        assert [c[0] for c in fake.calls[calls_before:]] == ["lobe-b-clinical", "lobe-a"]
    finally:
        respond._assert_clinical_b_local = respond_local


async def test_clinical_tool_results_without_paused_run_start_fresh(scripted):
    from dual_lobe.engines import respond

    seen = {}
    scripted(_tool_handler(seen))
    respond_local = respond._assert_clinical_b_local
    respond._assert_clinical_b_local = lambda: None
    try:
        resp = await respond.engine_response("clinical", {"model": "m", "tools": WEATHER, "messages": [
            {"role": "user", "content": "Weather in Paris?"},
            {"role": "assistant", "content": None, "tool_calls": [_call("get_weather", {"city": "Paris"}, "zz")]},
            {"role": "tool", "tool_call_id": "zz", "content": "18C, sunny"}]})
        assert resp.headers["x-dual-lobe-resumed"] == "fresh-run"
    finally:
        respond._assert_clinical_b_local = respond_local


async def test_clinical_long_term_memory_is_sanitized_and_read_back(scripted, tmp_path):
    from dual_lobe.engines.clinical import engine as clinical_engine

    seen = []

    def handler(alias, req):
        user = req.messages[1]["content"]
        if alias == "lobe-a" and "B EXECUTION REPORT" in user:
            return _msg("Hold spironolactone for <PHI:EMAIL:X>.")
        if alias == "lobe-a":
            seen.append(user)
            return _msg(tool_calls=_plan_call(PLAN))
        return _msg(json.dumps({"plan_revision": 0, "steps": [{"id": "S1", "status": "completed"}],
                                "summary": "K 6.8 checked for jane@x.org"}))

    scripted(handler)
    eng = ClinicalDualLobeEngine()
    await eng.run_clinical(query="Increase spironolactone?", patient_context="email jane@x.org, K+ 6.8")
    await asyncio.gather(*[f for f in clinical_engine._BACKGROUND if f.get_loop() is asyncio.get_running_loop()])
    stored = (tmp_path / "clinical.jsonl").read_text()
    assert "spironolactone" in stored and "jane@x.org" not in stored        # remembered, no raw PHI
    result = await ClinicalDualLobeEngine().run_clinical(query="spironolactone again?", patient_context="K+ 6.1")
    assert result.memory_entries_used == 1
    assert "LONG-TERM MEMORY" in seen[-1] and "Hold spironolactone" in seen[-1]  # A plans with the memory


async def test_clinical_stream_returns_tool_calls_chunk(scripted):
    from dual_lobe.engines import respond

    scripted(_tool_handler({}))
    respond_local = respond._assert_clinical_b_local
    respond._assert_clinical_b_local = lambda: None
    try:
        resp = await respond.engine_response("clinical", {"model": "m", "stream": True, "tools": WEATHER,
                                                          "messages": [{"role": "user", "content": "Weather?"}]})
        body = "".join([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
    finally:
        respond._assert_clinical_b_local = respond_local
    chunks = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: {")]
    tc = [c["choices"][0]["delta"]["tool_calls"] for c in chunks if c["choices"][0]["delta"].get("tool_calls")]
    assert tc and tc[0][0]["function"]["name"] == "get_weather" and tc[0][0]["index"] == 0
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


async def test_clinical_b_is_sent_back_when_it_skips_planned_tools(scripted):
    from dual_lobe.engines import respond

    b_turns = []

    def handler(alias, req):
        user = req.messages[1]["content"]
        if alias == "lobe-a" and "B EXECUTION REPORT" in user:
            return _msg("18C in Paris.")
        if alias == "lobe-a":
            return _msg(tool_calls=_plan_call(PLAN_TOOL))
        b_turns.append(req.messages[-1])
        if req.messages[-1]["role"] == "user" and "did not call them" in req.messages[-1]["content"]:
            return _msg(tool_calls=[_call("get_weather", {"city": "Paris"}, "w9")])
        return _msg(json.dumps({"plan_revision": 0, "steps": [{"id": "S1", "status": "completed",
                                                               "result": "sunny (guessed)"}]}))

    scripted(handler)
    respond_local = respond._assert_clinical_b_local
    respond._assert_clinical_b_local = lambda: None
    try:
        resp = await respond.engine_response("clinical", {"model": "m", "tools": WEATHER, "messages": [
            {"role": "user", "content": "Weather in Paris?"}]})
    finally:
        respond._assert_clinical_b_local = respond_local
    body = json.loads(resp.body)
    assert body["choices"][0]["finish_reason"] == "tool_calls"          # B was made to call the tool
    assert body["choices"][0]["message"]["tool_calls"][0]["id"] == "w9"
    assert len(b_turns) == 2


async def test_response_carries_per_call_latency_log(scripted):
    from dual_lobe.engines import respond
    from dual_lobe.provider import calltrace
    from dual_lobe.provider.adapters import ProviderTarget

    calltrace.begin()
    target = ProviderTarget(alias="lobe-a", base_url="https://x.example/v1", api_key="k", model="m1")
    calltrace.record_call(target=target, started=__import__("time").perf_counter(), gate_wait=0.0,
                          status=200, usage={"prompt_tokens": 5, "completion_tokens": 2})
    scripted(lambda alias, req: _msg("Canberra."))
    # Use split engine (no local-only guard) — the calltrace entries travel with any engine.
    resp = await respond.engine_response("split", {"model": "m", "messages": [{"role": "user", "content": "q"}]})
    body = json.loads(resp.body)
    calls = body["dual_lobe"]["calls"]
    # The manually-recorded entry must be present plus engine's own entries.
    manual = [c for c in calls if c.get("lobe") == "lobe-a"]
    assert len(manual) >= 1
    assert manual[0]["provider"] == "x.example" and manual[0]["prompt_tokens"] == 5
    assert body["dual_lobe"]["server_ms"] > 0
