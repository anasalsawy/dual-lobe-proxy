"""Check information separation, freshness, failure behavior and actual delivery."""
import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from dual_lobe.api import chat
from dual_lobe.api.auth import Principal
from dual_lobe.api.schemas import ChatCompletionRequest
from dual_lobe.b import context_shadow, prompts
from dual_lobe.b.channels import completed_memory, prepare_context, reviewed_state
from dual_lobe.b.protocol import Review, ground_review
from dual_lobe.core.settings import Settings


def example_review():
    return Review.model_validate({
        "goal": "Make the application run on Windows",
        "questions": ["Does the startup command work on Windows?"],
        "next_step": "Check the platform prerequisite before changing credentials again.",
        "context_notes": ["The original goal includes an installable application."],
        "concerns": [{"signal": "CONTRADICTION", "claim_quote": "All tests passed.",
                      "basis_quote": "tests failed", "reason": "The supplied result disagrees.",
                      "suggestion": "Report the remaining failure."}],
    })


def snapshot(observed_at=None, **extra):
    return reviewed_state({}, example_review(), {
        "run_id": "run", "source_call": "call-1", "floor_id": "f1", "attempt_id": 1,
        "observed_at": time.time() if observed_at is None else observed_at, **extra,
    })


def test_three_paths_are_distinct_and_user_input_is_unchanged():
    settings = Settings(_env_file=None)
    context = prepare_context(snapshot(), "f1", 1, settings)
    messages = [{"role": "system", "content": "Existing application rules"},
                {"role": "user", "content": "Continue the work"}]
    original = copy.deepcopy(messages)
    result = chat._effective_messages(messages, context, True)
    assert result[0] == messages[0] and result[-1] == messages[-1] and messages == original
    assert result[1] == {"role": "system", "content": prompts.OBSERVATION_REMINDER}
    deception, memory, claims = result[2:5]
    assert deception["name"] == "observer_deception"
    assert deception["content"].startswith("Observer deception meter")
    assert memory["name"] == "observer_memory" and claims["name"] == "observer_claims"
    assert memory["role"] == claims["role"] == deception["role"] == "user"
    assert "Windows" in memory["content"] and "All tests passed." not in memory["content"]
    assert "All tests passed." in claims["content"] and "Windows" not in claims["content"]


def test_memory_survives_expired_claims_but_not_its_own_expiry():
    settings = Settings(_env_file=None, context_memory_ttl_seconds=86400, b_state_ttl_seconds=180)
    state = snapshot(observed_at=1000.)
    context = prepare_context(state, "f1", 1, settings, now=1400)
    assert context.memory_version == 1 and context.memory_text
    assert context.claims_text is None and context.claim_status == "stale"
    expired = prepare_context(state, "f1", 1, settings, now=90000)
    assert expired.memory_text is None and expired.memory_status == "stale"


@pytest.mark.parametrize("floor,attempt", [("f2", 1), ("f1", 2)])
def test_memory_and_claims_respect_scope(floor, attempt):
    context = prepare_context(snapshot(), floor, attempt, Settings(_env_file=None))
    assert not context.memory_text and not context.claims_text
    assert context.memory_status == "scope_mismatch"


@pytest.mark.parametrize("memory,claims", [(False, True), (True, False), (False, False)])
def test_independent_switches(memory, claims):
    context = prepare_context(snapshot(), "f1", 1, Settings(
        _env_file=None, context_memory_enabled=memory, claim_checks_enabled=claims))
    assert bool(context.memory_text) == memory and bool(context.claims_text) == claims


def test_memory_is_replaced_not_accumulated_and_resolved_claims_clear():
    prior = snapshot()
    review = Review(goal="New clarified goal", questions=[], next_step="", concerns=[])
    state = reviewed_state(prior, review, {"run_id": "run", "observed_at": time.time()})
    assert state["context_memory"]["version"] == 2
    assert "Windows" not in json.dumps(state["context_memory"])
    assert state["claim_review"]["concerns"] == []


def test_prompt_caps_preserve_valid_data_and_no_privileged_generated_content():
    review = example_review()
    review.goal = "g" * 400
    review.questions = ["q" * 400, "r" * 400]
    review.context_notes = ["n" * 400, "m" * 400]
    review.next_step = "s" * 500
    review.concerns *= 3
    state = reviewed_state({}, review, {"run_id": "run", "observed_at": time.time()})
    context = prepare_context(state, "", 1, Settings(_env_file=None, max_memory_chars=600, max_injection_chars=600))
    for value in (context.memory_text, context.claims_text):
        assert len(value) <= 600
        assert isinstance(json.loads(value.split("\n", 1)[1]), dict)


def test_legacy_state_converts_without_mutating_or_renewing_it():
    legacy = {"schema_version": 2, "oversight_status": "reviewed", "observed_at": 1000.,
              "reviewed_at": 1001., "floor_id": "f1", "attempt_id": 1,
              "review": example_review().model_dump()}
    original = copy.deepcopy(legacy)
    memory = completed_memory(legacy)
    assert memory.version == 1 and memory.observed_at == 1000 and legacy == original
    assert "concerns" not in memory.content.model_dump()
    assert "deception_level" not in memory.content.model_dump()


def test_deception_level_lives_outside_memory_and_is_delivered():
    state = reviewed_state({}, Review.model_validate({
        "goal": "g", "deception_level": "YELLOW", "questions": [], "next_step": "",
        "context_notes": [], "concerns": []}), {"run_id": "run", "observed_at": time.time()})
    assert state["deception_level"] == "YELLOW"
    assert "deception_level" not in state["context_memory"]["content"]
    context = prepare_context(state, "", 1, Settings(_env_file=None))
    assert context.deception_status == "YELLOW"
    assert context.deception_text == "Observer deception meter for the last answer: YELLOW."
    assert context.receipt()["deception_status"] == "YELLOW"


async def test_degraded_worker_preserves_completed_memory_without_renewal(monkeypatch):
    prior = snapshot(observed_at=time.time() - 20)
    saved = AsyncMock()
    monkeypatch.setattr(context_shadow.repo, "latest_b_state", AsyncMock(return_value={"payload": prior}))
    monkeypatch.setattr(context_shadow.repo, "list_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(context_shadow.repo, "save_b_state", saved)
    monkeypatch.setattr(context_shadow.repo, "append_event", AsyncMock())
    monkeypatch.setattr(context_shadow, "_call_b", AsyncMock(side_effect=ValueError("bad JSON")))
    monkeypatch.setattr(context_shadow, "get_settings", lambda: Settings(_env_file=None, b_cooldown_seconds=0))
    result = await context_shadow.run_shadow_cycle(None, {"payload": {
        "run_id": "run", "floor_id": "f1", "attempt_id": 1, "observed_at": time.time(),
    }}, 780)
    state = saved.call_args.args[3]
    assert result["degraded"] and state["context_memory"] == prior["context_memory"]
    context = prepare_context(state, "f1", 1, Settings(_env_file=None))
    assert context.memory_version == 1 and context.memory_text and not context.claims_text
    assert context.claim_status == "degraded"


async def test_successful_worker_stores_two_channels_atomically(monkeypatch):
    saved, event = AsyncMock(), AsyncMock()
    monkeypatch.setattr(context_shadow.repo, "latest_b_state", AsyncMock(return_value=None))
    monkeypatch.setattr(context_shadow.repo, "list_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(context_shadow.repo, "save_b_state", saved)
    monkeypatch.setattr(context_shadow.repo, "append_event", event)
    monkeypatch.setattr(context_shadow, "_call_b",
                        AsyncMock(return_value=json.dumps(example_review().model_dump())))
    result = await context_shadow.run_shadow_cycle(None, {"payload": {
        "run_id": "run", "observed_at": time.time(), "context_text": "tests failed",
        "response_text": "All tests passed.",
    }}, 781)
    state = saved.call_args.args[3]
    assert result["ok"] and saved.await_count == 1
    assert "concerns" not in state["context_memory"]["content"]
    assert state["claim_review"]["concerns"][0]["signal"] == "CONTRADICTION"
    assert any(c.args[1] == "context_memory_updated" for c in event.call_args_list)


async def test_new_memory_delivered_on_next_call_while_b_is_still_busy(monkeypatch):
    """A receives changing DB snapshots without asking B or modifying user history."""
    settings = Settings(_env_file=None, rollout_stage="context")
    state = snapshot()
    read = AsyncMock(side_effect=lambda *_: {"payload": state})
    calls = []
    b_busy = asyncio.Event()
    async def b_work():
        await b_busy.wait()
    background_b = asyncio.create_task(b_work())
    @asynccontextmanager
    async def session(*args):
        yield SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    monkeypatch.setattr(chat, "tenant_session", session)
    monkeypatch.setattr(chat.repo, "latest_b_state", read)
    monkeypatch.setattr(chat.repo, "get_or_create_run", AsyncMock(return_value=SimpleNamespace(id="run", goal="goal")))
    monkeypatch.setattr(chat.limits, "check_limits", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(chat, "_persist_observation", AsyncMock())
    async def a(req):
        calls.append(req.messages)
        assert not background_b.done()
        return {"choices": [{"message": {"content": "ordinary response"}}]}
    monkeypatch.setattr(chat, "get_registry", lambda: SimpleNamespace(
        target=lambda _: SimpleNamespace(enabled=True, model="fake", kind="chat_completions"),
        adapter=lambda _: SimpleNamespace(buffered=a)))
    request = Request({"type": "http", "headers": [(b"x-dl-run-id", b"run"),
                       (b"x-dl-floor-id", b"f1")], "method": "POST", "path": "/"})
    body = ChatCompletionRequest(messages=[{"role": "user", "content": "Continue"}])
    try:
        first = await asyncio.wait_for(chat.chat_completions(body, request, Principal(1, "t", frozenset())), .5)
        state = reviewed_state(state, example_review(), {
            "run_id": "run", "floor_id": "f1", "observed_at": time.time(), "source_call": "call-2"})
        second = await asyncio.wait_for(chat.chat_completions(body, request, Principal(1, "t", frozenset())), .5)
        assert first.headers["x-dual-lobe-memory"] == "v1"
        assert second.headers["x-dual-lobe-memory"] == "v2"
        assert second.headers["x-dual-lobe-claims"] == "available"
        assert second.headers["x-dual-lobe-monitoring"] == "on"
        assert read.await_count == 2
        assert all(c[-1] == body.messages[0] for c in calls)
        assert all(sum(m.get("name") == "observer_memory" for m in c) == 1 for c in calls)
        assert json.loads(second.body)["choices"][0]["message"]["content"] == "ordinary response"
        await second.background()
        receipt = chat._persist_observation.call_args.args[-2]["observer_delivery"]
        assert receipt["memory_version"] == 2 and receipt["monitoring"]
    finally:
        b_busy.set()
        await background_b


async def test_state_inspection_shows_memory_when_claim_review_expires(monkeypatch):
    from dual_lobe.api import state as state_api
    prior = snapshot(observed_at=time.time() - 400)
    @asynccontextmanager
    async def session(*args):
        yield None
    monkeypatch.setattr(state_api, "tenant_session", session)
    monkeypatch.setattr(state_api.repo, "_get_run_or_none", AsyncMock(return_value=SimpleNamespace(
        id="run", current_floor="f1", current_attempt=1)))
    monkeypatch.setattr(state_api.repo, "latest_b_state", AsyncMock(return_value={
        "revision": 1, "pulse": "post", "payload": prior}))
    for name in ("list_claims", "list_evidence", "list_events"):
        monkeypatch.setattr(state_api.repo, name, AsyncMock(return_value=[]))
    result = await state_api.run_state("run", None, Principal(1, "t", frozenset()))
    assert result.payload["oversight_status"] == "stale"
    assert result.payload["context_memory_status"] == "available"
    assert result.payload["context_memory"]["version"] == 1
    assert prior["oversight_status"] == "reviewed"  # no mutation during inspection
