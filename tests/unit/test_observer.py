"""No Docker, network, API keys, or model calls: deterministic contract tests."""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from dual_lobe.api import chat
from dual_lobe.b import context_shadow, prompts
from dual_lobe.b.protocol import Review, ground_review, parse_review, usable_state
from dual_lobe.b.channels import ObserverContext, prepare_context, reviewed_state
from dual_lobe.core.settings import Settings
from dual_lobe.provider.adapters import ChatCompletionsAdapter, NormalizedRequest, ProviderTarget, resolve_request
from dual_lobe.provider.registry import Registry


def review(**kwargs):
    return {"goal": "Fix tests", "questions": [], "next_step": "", "context_notes": [], "concerns": [], **kwargs}


@pytest.mark.parametrize("raw", ["", "{}", "[]", "not json", '{"goal":', 'prose {"goal":"x"}'])
def test_invalid_review_rejected(raw):
    with pytest.raises(ValueError):
        parse_review(raw)


def test_review_tolerates_fences_and_prose_but_not_schema_incompleteness():
    assert parse_review("```json\n" + json.dumps(review()) + "\n```").concerns == []
    assert parse_review("Here you go:\n" + json.dumps(review())).concerns == []
    with pytest.raises(ValueError):  # found JSON object but missing required keys.
        parse_review('prefix {"goal": "only"} suffix')


def test_empty_but_well_formed_review_is_valid():
    assert parse_review(json.dumps(review())).concerns == []


@pytest.mark.parametrize("change", [
    {"questions": ["a", "b", "c"]}, {"next_step": "x" * 501},
    {"goal": "x" * 401}, {"verdict": "PASS"}, {"goal": 7},
    {"deception_level": "ORANGE"}, {"deception_level": 1},
])
def test_bounded_schema(change):
    with pytest.raises(ValidationError):
        Review.model_validate(review(**change))


@pytest.mark.parametrize("level", ["GREEN", "YELLOW", "RED"])
def test_all_deception_levels_are_valid(level):
    assert Review.model_validate(review(deception_level=level)).deception_level == level


def test_missing_legacy_color_is_not_invented():
    assert Review.model_validate(review()).deception_level is None


def concern(**kwargs):
    return {"signal": "CONTRADICTION", "claim_quote": "All tests passed.",
            "basis_quote": "tests failed", "reason": "Results disagree.",
            "suggestion": "Report the failure.", **kwargs}


def test_concern_must_quote_supplied_output_and_basis():
    prompt = prompts.build_cycle_prompt("tool result: tests failed", "All tests passed.", "", "")
    good = Review.model_validate(review(concerns=[concern()]))
    assert ground_review(good, prompt) is good
    for bad in (concern(claim_quote="invented"), concern(basis_quote="nonexistent"),
                concern(basis_quote=""), concern(signal="VERIFIED")):
        with pytest.raises(ValueError):
            ground_review(Review.model_validate(review(concerns=[bad])), prompt)


def test_missing_evidence_can_be_unsupported_not_false():
    prompt = prompts.build_cycle_prompt("record incomplete", "All tests passed.", "", "")
    result = ground_review(Review.model_validate(review(
        concerns=[concern(signal="UNSUPPORTED", basis_quote="")]
    )), prompt)
    assert result.concerns[0].signal == "UNSUPPORTED"


def test_shift_basis_may_come_from_latest_request_not_original_goal():
    prompt = prompts.build_cycle_prompt(
        "original goal: DNS round-robin", "PostgreSQL works now.", "",
        "", latest_request="Fix the PostgreSQL migration error.")
    data = json.loads(prompt.split(prompts.EVIDENCE_MARKER)[1])
    assert "LATEST_REQUEST" in data and "Fix the PostgreSQL migration error." in data["LATEST_REQUEST"]
    shift = Review.model_validate(review(concerns=[concern(
        signal="CONTRADICTION", claim_quote="PostgreSQL works now.",
        basis_quote="Fix the PostgreSQL migration error.")]))
    assert ground_review(shift, prompt) is shift


def test_complete_prompt_budget_and_goal_retention():
    prompt = prompts.build_cycle_prompt(
        "ORIGINAL GOAL " + "x" * 40000 + " LAST ERROR", "y" * 30000,
        "\x00" * 30000, "z" * 30000, max_chars=6000,
    )
    assert len(prompt) <= 6000
    assert "ORIGINAL GOAL" in prompt and "LAST ERROR" in prompt
    assert "context omitted" in prompt
    assert json.loads(prompt.split(prompts.EVIDENCE_MARKER)[1])


@pytest.mark.parametrize("changes", [
    {"observed_at": 1}, {"observed_at": 2000}, {"observed_at": "bad"},
    {"floor_id": "other"}, {"attempt_id": 2}, {"oversight_status": "degraded"},
])
def test_stale_wrong_floor_or_degraded_state_not_injected(changes):
    state = {"observed_at": 990, "floor_id": "f1", "attempt_id": 1,
             "oversight_status": "reviewed", **changes}
    assert not usable_state(state, "f1", 1, 180, now=1000)


def test_fresh_notes_are_bounded_advisory_not_system_data():
    state = reviewed_state({}, Review.model_validate(review(
        questions=["What prerequisite is missing?"], next_step="Read the supplied configuration.")),
        {"run_id": "run", "observed_at": time.time()})
    context = prepare_context(state, "", 1, Settings(_env_file=None, max_memory_chars=600))
    assert context.memory_text and len(context.memory_text) <= 600 and "untrusted" in context.memory_text
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"},
                {"role": "assistant", "tool_calls": [{"id": "t1"}]},
                {"role": "tool", "tool_call_id": "t1", "content": "failed"}]
    effective = chat._effective_messages(messages, context, True)
    assert effective[2]["role"] == "user"
    assert effective[-2:] == messages[-2:]
    assert len(messages) == 4


def test_context_preserves_tool_requests_results_and_mission():
    text = chat._messages_text([
        {"role": "user", "content": "Original mission"},
        {"role": "assistant", "tool_calls": [{"function": {"name": "test"}}]},
        {"role": "tool", "tool_call_id": "abc", "content": "3 failures"},
    ], 5000)
    assert all(s in text for s in ("Original mission", "tool_calls", "3 failures", "abc"))


def test_unknown_alias_never_routes_silently_to_a():
    registry = Registry()
    registry.register(ProviderTarget("lobe-a", "https://invalid", "key", "model"))
    with pytest.raises(KeyError):
        registry.target("typo")


def test_forwarded_options_and_blocked_provider_override():
    req = resolve_request({"messages": [{"role": "user", "content": "hi"}],
                           "frequency_penalty": .5, "presence_penalty": .2,
                           "parallel_tool_calls": True, "max_completion_tokens": 50,
                           "api_key": "evil"})
    assert req.to_kwargs()["frequency_penalty"] == .5
    assert req.to_kwargs()["parallel_tool_calls"] is True
    assert "api_key" not in req.to_kwargs()


def chunk(delta, finish=None):
    return {"id": "stream-id", "object": "chat.completion.chunk", "created": 123,
            "model": "upstream", "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}]}


def audit():
    return {"output": "", "status": "INCOMPLETE"}


async def test_first_chunk_arrives_before_upstream_completion():
    gate = asyncio.Event()
    class Slow:
        async def stream(self, req):
            yield chunk({"role": "assistant", "content": "first"})
            await gate.wait()
            yield chunk({"content": "last"}, "stop")
    record = audit()
    generator = chat._stream_body(Slow(), NormalizedRequest(messages=[], timeout=2), "lobe-a", record)
    first = await asyncio.wait_for(anext(generator), .3)
    assert "first" in first and not gate.is_set()
    assert record["status"] != "SUCCESS"
    gate.set()
    rest = [item async for item in generator]
    assert "last" in rest[0] and rest[-1] == "data: [DONE]\n\n"
    assert record["status"] == "SUCCESS"


async def test_stream_preserves_all_tool_fragments_usage_and_no_fake_stop():
    calls = [{"index": 0, "id": "t0", "function": {"name": "a", "arguments": "{}"}},
             {"index": 1, "id": "t1", "function": {"name": "b", "arguments": "{}"}}]
    class Tools:
        async def stream(self, req):
            yield chunk({"role": "assistant", "tool_calls": calls}, "tool_calls")
            yield {"id": "stream-id", "choices": [], "usage": {"total_tokens": 42}}
    record = audit()
    result = [s async for s in chat._stream_body(
        Tools(), NormalizedRequest(messages=[], timeout=2), "lobe-a", record)]
    data = json.loads(result[0].removeprefix("data: "))
    assert data["choices"][0]["delta"]["tool_calls"] == calls
    assert data["created"] == 123 and data["id"] == "stream-id"
    assert record["usage"]["total_tokens"] == 42
    assert '"finish_reason": "stop"' not in "".join(result)


async def test_interrupted_stream_keeps_partial_output_and_reports_error():
    class Broken:
        async def stream(self, req):
            yield chunk({"content": "partial"})
            raise ConnectionError("secret-provider-detail")
    record = audit()
    result = "".join([s async for s in chat._stream_body(
        Broken(), NormalizedRequest(messages=[], timeout=2), "lobe-a", record)])
    assert "partial" in result and "upstream_stream_error" in result
    assert "secret-provider-detail" not in result and '"stop"' not in result
    assert record["status"] == "INCOMPLETE"


async def test_missing_terminal_is_not_success():
    class Missing:
        async def stream(self, req):
            yield chunk({"content": "partial"})
    record = audit()
    result = [s async for s in chat._stream_body(
        Missing(), NormalizedRequest(messages=[], timeout=2), "lobe-a", record)]
    assert "upstream_stream_error" in "".join(result)
    assert record["status"] == "INCOMPLETE"


async def test_adapter_async_and_closes_stream(monkeypatch):
    import httpx

    class Source(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (": keepalive\n\ndata: " + json.dumps(chunk({"content": "ok"}, "stop"))
                   + "\n\ndata: [DONE]\n\n").encode()
        async def aclose(self):
            self.closed = True
    source = Source()
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=source)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("dual_lobe.provider.adapters.get_http_client", lambda: client)
        adapter = ChatCompletionsAdapter(ProviderTarget("a", "https://invalid/v1", "key", "model"))
        result = [c async for c in adapter.stream(NormalizedRequest(messages=[], stream=True))]
    assert result[0]["choices"][0]["delta"]["content"] == "ok"
    assert source.closed and len(requests) == 1
    assert str(requests[0].url) == "https://invalid/v1/chat/completions"
    body = json.loads(requests[0].content)
    assert body["model"] == "model" and "timeout" not in body
    assert requests[0].headers["Authorization"] == "Bearer key"


async def test_empty_buffered_response_rejected_but_tool_only_is_valid():
    empty = {"choices": [{"message": {"content": ""}}]}
    with pytest.raises(ValueError):
        await chat._call_a_with_retry(AsyncMock(return_value=empty), 1)
    tools = {"choices": [{"message": {"tool_calls": [{"id": "t"}]}}]}
    assert await chat._call_a_with_retry(AsyncMock(return_value=tools), 1) == tools


async def test_slow_state_read_fails_open(monkeypatch):
    @asynccontextmanager
    async def session(*args):
        yield None
    async def slow(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(chat, "tenant_session", session)
    monkeypatch.setattr(chat.repo, "latest_b_state", slow)
    context = await asyncio.wait_for(chat._read_context(1, "run", "", 1), .3)
    assert context.memory_text is None and context.claims_text is None
    assert context.status == "state_unavailable"


async def test_persistence_failure_does_not_raise(monkeypatch):
    @asynccontextmanager
    async def broken(*args):
        raise ConnectionError("unavailable")
        yield
    monkeypatch.setattr(chat, "tenant_session", broken)
    await chat._persist_observation(1, "run", "external", {}, "lobe-a", "", {
        "output": "result", "call_id": "id",
    }, True)


async def test_b_call_has_no_tools_one_attempt_and_output_cap(monkeypatch):
    settings = Settings(_env_file=None, b_max_output_tokens=800)
    completion = AsyncMock(return_value={"choices": [{"message": {"content": json.dumps(review())}}]})
    monkeypatch.setattr(context_shadow, "get_settings", lambda: settings)
    monkeypatch.setattr(context_shadow, "get_registry",
                        lambda: SimpleNamespace(adapter=lambda _: SimpleNamespace(buffered=completion)))
    assert await context_shadow._call_b("lobe-b", "input") == json.dumps(review())
    req = completion.call_args.args[0]
    assert req.tools is None and req.max_tokens == 800
    completion.side_effect = RuntimeError("down")
    with pytest.raises(RuntimeError):
        await context_shadow._call_b("lobe-b", "input")
    assert completion.call_count == 2  # one per invocation, not retries


async def test_review_failure_records_degraded_not_clean(monkeypatch):
    payload = {"run_id": "run", "observed_at": time.time(), "context_text": "ctx",
               "response_text": "out"}
    monkeypatch.setattr(context_shadow.repo, "latest_b_state", AsyncMock(return_value=None))
    monkeypatch.setattr(context_shadow.repo, "list_events", AsyncMock(return_value=[]))
    saved = AsyncMock()
    monkeypatch.setattr(context_shadow.repo, "save_b_state", saved)
    monkeypatch.setattr(context_shadow.repo, "append_event", AsyncMock())
    monkeypatch.setattr(context_shadow, "_call_b", AsyncMock(return_value="not json"))
    result = await context_shadow.run_shadow_cycle(None, {"payload": payload}, 777)
    assert result["degraded"]
    assert saved.call_args.args[3]["oversight_status"] == "degraded"


async def test_corrective_retry_salvages_invalid_first_review(monkeypatch):
    calls = []
    prompt = prompts.build_cycle_prompt("record incomplete", "All tests passed.", "", "")

    async def flaky_b(target, prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return "```\n{\n  \"goal\": \"Fix tests\"\n}\n```"  # schema-incomplete, fixed on retry.
        return json.dumps({**review(), "concerns": [{
            "signal": "UNSUPPORTED", "claim_quote": "All tests passed.",
            "basis_quote": "", "reason": "no result in record", "suggestion": "report it"}]})
    monkeypatch.setattr(context_shadow, "_call_b", flaky_b)
    result = await context_shadow._obtain_review("lobe-b", prompt)
    assert result.goal == "Fix tests"
    assert len(calls) == 2 and "rejected" not in calls[0] and "rejected" in calls[1]


async def test_corrective_retry_still_fails_open_after_two_attempts(monkeypatch):
    monkeypatch.setattr(context_shadow, "_call_b",
                        AsyncMock(return_value="not json at all"))
    with pytest.raises(ValueError):
        await context_shadow._obtain_review("lobe-b", "input")


async def test_transport_error_is_not_double_fired(monkeypatch):
    calls = []

    async def failing_b(target, prompt):
        calls.append(prompt)
        raise RuntimeError("429 rate limit")
    monkeypatch.setattr(context_shadow, "_call_b", failing_b)
    with pytest.raises(RuntimeError):
        await context_shadow._obtain_review("lobe-b", "input")
    assert len(calls) == 1  # no corrective retry on transport/provider errors


async def test_model_color_is_not_recalculated_from_concern_count(monkeypatch):
    payload = {"run_id": "run", "observed_at": time.time(), "context_text": "ctx",
               "response_text": "out"}
    monkeypatch.setattr(context_shadow.repo, "latest_b_state", AsyncMock(return_value=None))
    monkeypatch.setattr(context_shadow.repo, "list_events", AsyncMock(return_value=[]))
    saved = AsyncMock()
    monkeypatch.setattr(context_shadow.repo, "save_b_state", saved)
    monkeypatch.setattr(context_shadow.repo, "append_event", AsyncMock())
    red_bare = json.dumps({**review(deception_level="RED"), "concerns": []})
    monkeypatch.setattr(context_shadow, "_call_b", AsyncMock(return_value=red_bare))
    result = await context_shadow.run_shadow_cycle(None, {"payload": payload}, 777)
    assert result["ok"]
    state = saved.call_args.args[3]
    assert state["deception_level"] == "RED"


async def test_degraded_cycle_resets_color_and_keeps_failure_status(monkeypatch):
    payload = {"run_id": "run", "observed_at": time.time(), "context_text": "ctx",
               "response_text": "out"}
    previous = {"deception_level": "RED", "context_memory": None}
    monkeypatch.setattr(context_shadow.repo, "latest_b_state",
                        AsyncMock(return_value={"payload": previous}))
    monkeypatch.setattr(context_shadow.repo, "list_events", AsyncMock(return_value=[]))
    saved = AsyncMock()
    monkeypatch.setattr(context_shadow.repo, "save_b_state", saved)
    monkeypatch.setattr(context_shadow.repo, "append_event", AsyncMock())
    monkeypatch.setattr(context_shadow, "_call_b", AsyncMock(return_value="not json"))
    result = await context_shadow.run_shadow_cycle(None, {"payload": payload}, 777)
    assert result["degraded"]
    assert saved.call_args.args[3]["deception_level"] == "GREEN"


def test_legacy_enforcement_is_advisory_and_rls_is_required():
    from dual_lobe.core.stage import challenge_mode
    assert challenge_mode("enforcement") == "note"
    with pytest.raises(ValueError):
        _ = Settings(_env_file=None, rls_database_url=None).rls_url


async def test_tpm_weight_cannot_exceed_limit():
    from dual_lobe.api.limits import _local_sliding
    allowed, _ = await _local_sliding("unit-budget", 60, 5, weight=6)
    assert not allowed
