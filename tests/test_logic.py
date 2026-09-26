import time

import pytest

import dual_lobe_crewai.engines as engines_module
from dual_lobe_crewai.engines import SplitEngine, RunResult
from dual_lobe_crewai.group_coordination import AgentIdentity, FloorMode, GroupCoordinator, GroupDualLobeRuntime, GroupMessage
from dual_lobe_crewai.json_utils import extract_json_object, parse_model
from dual_lobe_crewai.memory import JsonlMemoryStore
from dual_lobe_crewai.models import FinalizedTurn, SplitFragment, SplitPlan, SplitQuality, TurnReview, Verdict
from dual_lobe_crewai.prompts import VERIFICATION_PROTOCOL
from dual_lobe_crewai.tools import ProxyRunState, ProxyToolTrace, SelfSplitRunState


def test_empty_verdict_fails_closed_to_yellow():
    fallback = Verdict(deception_level="YELLOW", rationale="parse failed")
    got = parse_model("", Verdict, fallback)
    assert got.deception_level == "YELLOW"
    assert got.rationale == "parse failed"


def test_empty_object_verdict_fails_closed_to_yellow():
    fallback = Verdict(deception_level="YELLOW", rationale="parse failed")
    got = parse_model("{}", Verdict, fallback)
    assert got.deception_level == "YELLOW"


def test_blank_green_rationale_fails_closed_to_yellow():
    fallback = Verdict(deception_level="YELLOW", rationale="parse failed")
    got = parse_model('{"deception_level":"GREEN","rationale":""}', Verdict, fallback)
    assert got.deception_level == "YELLOW"
    assert got.rationale == "parse failed"


def test_finalized_turn_blank_answer_fails_closed():
    fallback = FinalizedTurn(
        final_answer="fallback preserved answer",
        answer_verdict=Verdict(deception_level="YELLOW", rationale="parse failed"),
        split_verdict=SplitQuality(
            used=True,
            valid=False,
            score=0,
            independence_score=0.0,
            balance_score=0.0,
            time_effect="unknown",
            feedback="parse failed",
        ),
    )
    got = parse_model(
        '{"final_answer":"","answer_verdict":{"deception_level":"GREEN","rationale":"ok"},'
        '"split_verdict":{"used":true,"valid":true,"score":90,"independence_score":0.9,'
        '"balance_score":0.9,"time_effect":"positive","feedback":"ok"}}',
        FinalizedTurn,
        fallback,
    )
    assert got.final_answer == "fallback preserved answer"
    assert got.answer_verdict.deception_level == "YELLOW"


def test_extract_first_balanced_object_when_two_objects():
    got = extract_json_object('{"a":1} {"b":2}')
    assert got == {"a": 1}


def test_unicode_memory_search(tmp_path):
    s = JsonlMemoryStore(str(tmp_path / "m.jsonl"))
    s.record("مرحبا العالم هذا اختبار الذاكرة")
    assert s.search("مرحبا العالم")


def test_split_experience_is_separate_from_ordinary_auto_slice(tmp_path):
    s = JsonlMemoryStore(str(tmp_path / "m.jsonl"))
    s.record("Project Atlas target is Windows Server 2022")
    s.record_split_experience('{"task_excerpt":"code review","grade":{"score":92}}')
    ordinary = s.auto_slice("Project Atlas")
    split = s.split_experience_slice("code review")
    assert "Windows Server 2022" in ordinary
    assert "SPLIT_EXPERIENCE" not in ordinary
    assert "score" in split


def test_legacy_non_split_caps_are_run_scoped():
    state = ProxyRunState()
    assert state.claim("delegate") is True
    assert state.claim("delegate") is False
    assert state.claim("consult") is True
    assert state.claim("consult") is False


def test_self_split_channel_can_only_be_claimed_once():
    state = SelfSplitRunState()
    assert state.claim_split(
        own_fragment="A",
        peer_fragment="B",
        reason="independent halves",
        merge_mode="append",
        peer_first=False,
    ) is True
    assert state.claim_split(
        own_fragment="A2",
        peer_fragment="B2",
        reason="again",
        merge_mode="append",
        peer_first=False,
    ) is False
    state.executor.shutdown(wait=False, cancel_futures=True)


def test_split_timing_reports_parallel_overlap_and_join_wait():
    state = SelfSplitRunState()
    base = time.perf_counter()
    state.split_used = True
    state.split_started_perf = base + 1.0
    state.b_started_perf = base + 1.0
    state.b_finished_perf = base + 5.0
    telemetry = SplitEngine._timing_telemetry(
        state,
        primary_started=base,
        primary_finished=base + 4.5,
    )
    assert telemetry["a_half_ms"] >= 3400
    assert telemetry["b_half_ms"] >= 3900
    assert telemetry["overlap_ms"] > 3000
    assert telemetry["parallel_gain_proxy_ms"] == telemetry["overlap_ms"]
    assert telemetry["join_wait_ms"] >= 400
    state.executor.shutdown(wait=False, cancel_futures=True)


def test_finalized_turn_contains_canonical_answer_and_both_grades():
    out = FinalizedTurn(
        final_answer="merged repaired answer",
        answer_verdict=Verdict(deception_level="GREEN", rationale="ok"),
        split_verdict=SplitQuality(
            used=True,
            valid=True,
            score=90,
            independence_score=0.9,
            balance_score=0.8,
            time_effect="positive",
            feedback="good split",
        ),
    )
    assert out.final_answer == "merged repaired answer"
    assert out.split_verdict.used is True


def test_turn_review_schema_supports_split_grade():
    review = TurnReview(
        answer_verdict=Verdict(deception_level="GREEN", rationale="ok"),
        split_verdict=SplitQuality(
            used=True,
            valid=True,
            score=91,
            independence_score=0.95,
            balance_score=0.8,
            time_effect="positive",
            feedback="Good split.",
        ),
    )
    assert review.split_verdict.score == 91


def test_run_result_exposes_self_split_feedback():
    grade = SplitQuality(
        used=False,
        valid=True,
        score=95,
        independence_score=1.0,
        balance_score=1.0,
        time_effect="unknown",
        feedback="Correctly stayed single-lane.",
    )
    r = RunResult(
        mode="split",
        answer="x",
        verdict=Verdict(deception_level="GREEN", rationale="ok"),
        logical_model_calls=2,
        route_source="a_self_normal",
        split_feedback=grade,
    )
    assert r.route_source == "a_self_normal"
    assert r.split_feedback.score == 95


@pytest.mark.asyncio
async def test_safe_run_one_converts_exception_to_fallback(monkeypatch):
    async def boom(*args, **kwargs):
        raise ValueError("Invalid response from LLM call - None or empty.")

    monkeypatch.setattr(engines_module, "run_one", boom)
    e = SplitEngine()
    out = await e._safe_run_one(
        object(),
        "x",
        "y",
        fallback_text="FALLBACK_SENTINEL",
    )
    assert out.startswith("FALLBACK_SENTINEL")
    assert "ValueError" in out


@pytest.mark.asyncio
async def test_safe_run_one_converts_blank_to_fallback(monkeypatch):
    async def blank(*args, **kwargs):
        return "   "

    monkeypatch.setattr(engines_module, "run_one", blank)
    e = SplitEngine()
    out = await e._safe_run_one(
        object(),
        "x",
        "y",
        fallback_text="FALLBACK_SENTINEL",
    )
    assert out == "FALLBACK_SENTINEL"


def test_verification_protocol_preserves_original_verifier_guards():
    assert "unsupported factual claims" in VERIFICATION_PROTOCOL
    assert "fabricated or exaggerated tool/action/file/external-event claims" in VERIFICATION_PROTOCOL
    assert "silent task drift" in VERIFICATION_PROTOCOL
    assert "unjustified certainty" in VERIFICATION_PROTOCOL
    assert "do not falsely say memory was unavailable or invisible" in VERIFICATION_PROTOCOL
    assert "Do not claim a required tool was unused" in VERIFICATION_PROTOCOL
    assert "not independent corroboration" in VERIFICATION_PROTOCOL
    assert "GREEN means only" in VERIFICATION_PROTOCOL
    assert "fail-closed" in VERIFICATION_PROTOCOL


@pytest.mark.asyncio
async def test_split_finalizer_receives_full_verifier_protocol(monkeypatch, tmp_path):
    captured = {}

    async def fake_safe(self, agent, description, expected_output, *, fallback_text, role_key=None):
        captured["description"] = description
        return (
            '{"final_answer":"merged","answer_verdict":{"deception_level":"GREEN","rationale":"no deception detected"},'
            '"split_verdict":{"used":true,"valid":true,"score":90,"independence_score":0.9,'
            '"balance_score":0.8,"time_effect":"unknown","feedback":"reasonable split"}}'
        )

    monkeypatch.setattr(SplitEngine, "_safe_run_one", fake_safe)
    monkeypatch.setattr(engines_module, "make_b_finalizer", lambda tools=None: object())
    store = JsonlMemoryStore(str(tmp_path / "m.jsonl"))
    engine = SplitEngine(memory=store)
    plan = SplitPlan(
        mode="split",
        fragments=[
            SplitFragment(owner="A", task="half a"),
            SplitFragment(owner="B", task="half b"),
        ],
        merge="integrate",
        reason="independent",
    )
    trace = ProxyToolTrace()
    result = await engine._finalize_split_with_b(
        task="original task",
        a_half="A result",
        b_half="B result",
        plan=plan,
        telemetry={"overlap_ms": 1000},
        trace=trace,
        memory_slice="known memory evidence",
        split_experience="prior lesson",
        canonical_state="",
    )
    prompt = captured["description"]
    assert VERIFICATION_PROTOCOL in prompt
    assert "A-half, B-half" in prompt
    assert "not independent corroboration" in prompt
    assert "answer_verdict MUST describe that exact emitted final_answer" in prompt
    assert result.final_answer == "merged"


def test_green_with_unverified_evidence_is_downgraded_to_yellow():
    verdict = Verdict(
        deception_level="GREEN",
        rationale="looked fine",
        handoff={"unverified": ["deployment actually happened"]},
    )
    hardened = SplitEngine._harden_verdict(verdict)
    assert hardened.deception_level == "YELLOW"


def test_green_with_proof_request_is_downgraded_to_yellow():
    verdict = Verdict(
        deception_level="GREEN",
        rationale="looked fine",
        handoff={"proof_requests": ["read the produced artifact"]},
    )
    hardened = SplitEngine._harden_verdict(verdict)
    assert hardened.deception_level == "YELLOW"


def test_split_grade_runtime_usage_mismatch_fails_closed():
    grade = SplitQuality(
        used=False,
        valid=True,
        score=95,
        independence_score=0.9,
        balance_score=0.9,
        time_effect="positive",
        feedback="model thought no split was used",
    )
    hardened = SplitEngine._harden_split_grade(
        grade,
        expected_used=True,
        overlap_ms=0,
    )
    assert hardened.used is True
    assert hardened.valid is False
    assert hardened.score <= 25
    assert hardened.time_effect == "unknown"


def test_proxy_trace_preserves_full_evidence_by_default():
    trace = ProxyToolTrace()
    payload = "x" * 12000
    trace.add("artifact_read", input_text="read artifact", output_text=payload, provenance="artifact")
    rendered = trace.render()
    assert payload in rendered
    assert "TRUNCATED_BY_RENDER" not in rendered


def test_proxy_trace_explicit_render_limit_marks_truncation():
    trace = ProxyToolTrace()
    payload = "y" * 1000
    trace.add("artifact_read", input_text="read artifact", output_text=payload, provenance="artifact")
    rendered = trace.render(max_chars_per_event=100)
    assert "TRUNCATED_BY_RENDER" in rendered
    assert "original_chars=1000" in rendered


def test_group_named_agent_only_can_respond():
    group = GroupCoordinator([
        AgentIdentity("sarah", "Sarah", aliases=("@sarah",)),
        AgentIdentity("david", "David", aliases=("@david",)),
        AgentIdentity("maya", "Maya", aliases=("@maya",)),
    ])
    deliveries = group.route(GroupMessage(text="Sarah, check the deployment logs"))
    assert deliveries["sarah"].mode == FloorMode.RESPOND
    assert deliveries["sarah"].can_emit is True
    assert deliveries["david"].mode == FloorMode.OBSERVE
    assert deliveries["david"].can_emit is False
    assert deliveries["maya"].can_emit is False


def test_group_non_addressed_agents_still_receive_awareness():
    group = GroupCoordinator([
        AgentIdentity("sarah", "Sarah"),
        AgentIdentity("david", "David"),
    ])
    group.states["david"].current_task = "write retry logic"
    deliveries = group.route(GroupMessage(text="Sarah, production is PostgreSQL 17"))
    assert deliveries["david"].can_emit is False
    assert group.states["david"].current_task == "write retry logic"
    awareness = group.consume_awareness_context("david")
    assert "PostgreSQL 17" in awareness
    assert "not granted the floor" in awareness
    assert group.consume_awareness_context("david") == ""


def test_group_output_gate_blocks_accidental_model_output():
    group = GroupCoordinator([
        AgentIdentity("sarah", "Sarah"),
        AgentIdentity("david", "David"),
    ])
    deliveries = group.route(GroupMessage(text="Sarah, status?"))
    assert GroupCoordinator.gate_output(deliveries["david"], "Sure, here is my status") is None
    assert GroupCoordinator.gate_output(deliveries["sarah"], "Checking now.") == "Checking now."


def test_group_multiple_named_agents_can_respond():
    group = GroupCoordinator([
        AgentIdentity("sarah", "Sarah"),
        AgentIdentity("david", "David"),
        AgentIdentity("maya", "Maya"),
    ])
    deliveries = group.route(GroupMessage(text="Sarah and David, compare your findings."))
    assert deliveries["sarah"].can_emit is True
    assert deliveries["david"].can_emit is True
    assert deliveries["maya"].can_emit is False


def test_group_broadcast_allows_everyone():
    group = GroupCoordinator([
        AgentIdentity("sarah", "Sarah"),
        AgentIdentity("david", "David"),
    ])
    deliveries = group.route(GroupMessage(text="Everyone, status?"))
    assert all(d.can_emit for d in deliveries.values())
    assert all(d.mode == FloorMode.BROADCAST for d in deliveries.values())


def test_group_reply_metadata_overrides_plain_text_ambiguity():
    group = GroupCoordinator([
        AgentIdentity("sarah", "Sarah"),
        AgentIdentity("david", "David"),
    ])
    deliveries = group.route(GroupMessage(
        text="Can you fix it?",
        reply_to_agent_id="sarah",
    ))
    assert deliveries["sarah"].can_emit is True
    assert deliveries["david"].can_emit is False


def test_group_explicit_platform_targets_are_authoritative():
    group = GroupCoordinator([
        AgentIdentity("sarah", "Sarah"),
        AgentIdentity("david", "David"),
    ])
    deliveries = group.route(GroupMessage(
        text="please review",
        explicit_target_ids=("david",),
    ))
    assert deliveries["david"].can_emit is True
    assert deliveries["sarah"].can_emit is False


def test_group_alias_collision_is_rejected():
    with pytest.raises(ValueError):
        GroupCoordinator([
            AgentIdentity("one", "Alpha", aliases=("coder",)),
            AgentIdentity("two", "Beta", aliases=("coder",)),
        ])


@pytest.mark.asyncio
async def test_group_live_runtime_invokes_only_addressed_agent():
    calls = []

    class FakeResult:
        def __init__(self, answer):
            self.answer = answer

    class FakeEngine:
        def __init__(self, agent_id):
            self.agent_id = agent_id

        async def run(self, task):
            calls.append((self.agent_id, task))
            return FakeResult(f"{self.agent_id} reply")

    runtime = GroupDualLobeRuntime(
        [
            AgentIdentity("sarah", "Sarah"),
            AgentIdentity("david", "David"),
            AgentIdentity("maya", "Maya"),
        ],
        engine_factory=lambda identity: FakeEngine(identity.agent_id),
    )

    result = await runtime.process_message(GroupMessage(text="Sarah, check the logs"))

    assert [agent_id for agent_id, _ in calls] == ["sarah"]
    assert result.published == {"sarah": "sarah reply"}
    assert set(result.suppressed) == {"david", "maya"}


@pytest.mark.asyncio
async def test_group_live_runtime_keeps_observer_awareness_for_next_real_turn():
    calls = []

    class FakeResult:
        def __init__(self, answer):
            self.answer = answer

    class FakeEngine:
        def __init__(self, agent_id):
            self.agent_id = agent_id

        async def run(self, task):
            calls.append((self.agent_id, task))
            return FakeResult("ok")

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("sarah", "Sarah"), AgentIdentity("david", "David")],
        engine_factory=lambda identity: FakeEngine(identity.agent_id),
    )

    await runtime.process_message(GroupMessage(text="Sarah, production is PostgreSQL 17"))
    assert [x[0] for x in calls] == ["sarah"]

    await runtime.process_message(GroupMessage(text="David, adjust your migration plan"))
    david_tasks = [task for agent_id, task in calls if agent_id == "david"]
    assert len(david_tasks) == 1
    assert "PostgreSQL 17" in david_tasks[0]
    assert "SELF agent_id=david" in david_tasks[0]
    assert "Sarah (agent_id=sarah)" in david_tasks[0]


@pytest.mark.asyncio
async def test_group_live_runtime_multiple_targets_run_concurrently_and_identity_isolated():
    seen = {}

    class FakeResult:
        def __init__(self, answer):
            self.answer = answer

    class FakeEngine:
        def __init__(self, agent_id):
            self.agent_id = agent_id

        async def run(self, task):
            seen[self.agent_id] = task
            return FakeResult(self.agent_id)

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("sarah", "Sarah"), AgentIdentity("david", "David"), AgentIdentity("maya", "Maya")],
        engine_factory=lambda identity: FakeEngine(identity.agent_id),
    )

    result = await runtime.process_message(GroupMessage(text="Sarah and David, compare findings"))

    assert set(result.published) == {"sarah", "david"}
    assert result.suppressed == ("maya",)
    assert "SELF agent_id=sarah" in seen["sarah"]
    assert "OTHER AGENTS: David (agent_id=david), Maya (agent_id=maya)" in seen["sarah"]
    assert "SELF agent_id=david" in seen["david"]
    assert "OTHER AGENTS: Sarah (agent_id=sarah), Maya (agent_id=maya)" in seen["david"]


@pytest.mark.asyncio
async def test_group_live_runtime_no_target_makes_zero_model_calls():
    calls = []

    class FakeEngine:
        async def run(self, task):
            calls.append(task)
            raise AssertionError("No agent should be invoked when nobody has the floor.")

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("sarah", "Sarah"), AgentIdentity("david", "David")],
        engine_factory=lambda identity: FakeEngine(),
    )

    result = await runtime.process_message(GroupMessage(text="The deployment finished at noon."))

    assert calls == []
    assert result.published == {}
    assert set(result.suppressed) == {"sarah", "david"}


@pytest.mark.asyncio
async def test_group_live_runtime_broadcast_invokes_all_agents():
    calls = []

    class FakeResult:
        def __init__(self, answer):
            self.answer = answer

    class FakeEngine:
        def __init__(self, agent_id):
            self.agent_id = agent_id

        async def run(self, task):
            calls.append(self.agent_id)
            return FakeResult(f"{self.agent_id}-ok")

    runtime = GroupDualLobeRuntime(
        [
            AgentIdentity("sarah", "Sarah"),
            AgentIdentity("david", "David"),
            AgentIdentity("maya", "Maya"),
        ],
        engine_factory=lambda identity: FakeEngine(identity.agent_id),
    )

    result = await runtime.process_message(GroupMessage(text="Everyone, status?"))

    assert set(calls) == {"sarah", "david", "maya"}
    assert set(result.published) == {"sarah", "david", "maya"}
    assert result.suppressed == ()


@pytest.mark.asyncio
async def test_group_live_runtime_multi_target_execution_is_concurrent():
    class FakeResult:
        def __init__(self, answer):
            self.answer = answer

    class FakeEngine:
        def __init__(self, agent_id):
            self.agent_id = agent_id

        async def run(self, task):
            import asyncio
            await asyncio.sleep(0.15)
            return FakeResult(self.agent_id)

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("sarah", "Sarah"), AgentIdentity("david", "David")],
        engine_factory=lambda identity: FakeEngine(identity.agent_id),
    )

    started = time.perf_counter()
    result = await runtime.process_message(GroupMessage(text="Sarah and David, compare findings"))
    elapsed = time.perf_counter() - started

    assert set(result.published) == {"sarah", "david"}
    assert elapsed < 0.27


def test_group_default_runtime_uses_separate_memory_per_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("DUAL_LOBE_GROUP_MEMORY_DIR", str(tmp_path))
    runtime = GroupDualLobeRuntime([
        AgentIdentity("sarah", "Sarah"),
        AgentIdentity("david", "David"),
    ])

    sarah_path = runtime.engines["sarah"].memory.path
    david_path = runtime.engines["david"].memory.path

    assert sarah_path != david_path
    assert sarah_path.name == "sarah.jsonl"
    assert david_path.name == "david.jsonl"


@pytest.mark.asyncio
async def test_group_single_agent_bypasses_all_group_routing_and_semantic_resolution():
    seen = {"semantic": 0, "task": None}

    class FakeResult:
        answer = "single reply"

    class FakeEngine:
        async def run(self, task):
            seen["task"] = task
            return FakeResult()

    async def semantic_should_not_run(message, identities):
        seen["semantic"] += 1
        raise AssertionError("semantic resolver must not run for one agent")

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("solo", "Solo")],
        engine_factory=lambda identity: FakeEngine(),
        semantic_resolver=semantic_should_not_run,
    )

    result = await runtime.process_message(GroupMessage(text="hello there"))

    assert seen["semantic"] == 0
    assert seen["task"] == "hello there"
    assert result.published == {"solo": "single reply"}


@pytest.mark.asyncio
async def test_group_deterministic_target_skips_semantic_fallback():
    seen = {"semantic": 0, "calls": []}

    class FakeResult:
        def __init__(self, answer):
            self.answer = answer

    class FakeEngine:
        def __init__(self, agent_id):
            self.agent_id = agent_id

        async def run(self, task):
            seen["calls"].append(self.agent_id)
            return FakeResult("ok")

    async def semantic_should_not_run(message, identities):
        seen["semantic"] += 1
        return ("david",)

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("sarah", "Sarah"), AgentIdentity("david", "David")],
        engine_factory=lambda identity: FakeEngine(identity.agent_id),
        semantic_resolver=semantic_should_not_run,
    )

    result = await runtime.process_message(GroupMessage(text="Sarah, check this"))

    assert seen["semantic"] == 0
    assert seen["calls"] == ["sarah"]
    assert set(result.published) == {"sarah"}


@pytest.mark.asyncio
async def test_group_ambiguous_message_uses_semantic_fallback_only_then():
    seen = {"semantic": 0, "calls": []}

    class FakeResult:
        def __init__(self, answer):
            self.answer = answer

    class FakeEngine:
        def __init__(self, agent_id):
            self.agent_id = agent_id

        async def run(self, task):
            seen["calls"].append(self.agent_id)
            return FakeResult(f"{self.agent_id}-reply")

    async def semantic_resolver(message, identities):
        seen["semantic"] += 1
        assert message.text == "Can the backend person check this?"
        assert {x.agent_id for x in identities} == {"sarah", "david"}
        return ("david",)

    runtime = GroupDualLobeRuntime(
        [
            AgentIdentity("sarah", "Sarah", role="research"),
            AgentIdentity("david", "David", role="backend engineering"),
        ],
        engine_factory=lambda identity: FakeEngine(identity.agent_id),
        semantic_resolver=semantic_resolver,
    )

    result = await runtime.process_message(GroupMessage(text="Can the backend person check this?"))

    assert seen["semantic"] == 1
    assert seen["calls"] == ["david"]
    assert result.published == {"david": "david-reply"}


@pytest.mark.asyncio
async def test_group_semantic_fallback_can_choose_no_target_without_invoking_agents():
    seen = {"semantic": 0, "calls": 0}

    class FakeEngine:
        async def run(self, task):
            seen["calls"] += 1
            raise AssertionError("no agent should run")

    async def semantic_resolver(message, identities):
        seen["semantic"] += 1
        return ()

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("sarah", "Sarah"), AgentIdentity("david", "David")],
        engine_factory=lambda identity: FakeEngine(),
        semantic_resolver=semantic_resolver,
    )

    result = await runtime.process_message(GroupMessage(text="The deployment finished at noon."))

    assert seen["semantic"] == 1
    assert seen["calls"] == 0
    assert result.published == {}


@pytest.mark.asyncio
async def test_one_agent_multi_user_group_does_not_bypass_floor_control():
    calls = []

    class FakeResult:
        answer = "agent reply"

    class FakeEngine:
        async def run(self, task):
            calls.append(task)
            return FakeResult()

    async def semantic_none(message, identities):
        return ()

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("helper", "Helper")],
        engine_factory=lambda identity: FakeEngine(),
        semantic_resolver=semantic_none,
    )

    # Two humans may be talking in the same group. The sole agent must not
    # answer just because it is the only agent attached.
    result = await runtime.process_message(
        GroupMessage(
            text="John, did you finish the spreadsheet?",
            sender_id="alice",
            group_id="team-chat",
            is_group=True,
        )
    )

    assert calls == []
    assert result.published == {}
    assert result.suppressed == ("helper",)


@pytest.mark.asyncio
async def test_one_agent_multi_user_group_responds_when_named():
    calls = []

    class FakeResult:
        answer = "agent reply"

    class FakeEngine:
        async def run(self, task):
            calls.append(task)
            return FakeResult()

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("helper", "Helper", aliases=("@helper",))],
        engine_factory=lambda identity: FakeEngine(),
    )

    result = await runtime.process_message(
        GroupMessage(
            text="Helper, can you check the spreadsheet?",
            sender_id="alice",
            group_id="team-chat",
            is_group=True,
        )
    )

    assert len(calls) == 1
    assert result.published == {"helper": "agent reply"}


@pytest.mark.asyncio
async def test_one_agent_direct_chat_still_uses_zero_routing_fast_path():
    seen = {"semantic": 0, "task": None}

    class FakeResult:
        answer = "direct reply"

    class FakeEngine:
        async def run(self, task):
            seen["task"] = task
            return FakeResult()

    async def semantic_should_not_run(message, identities):
        seen["semantic"] += 1
        raise AssertionError("semantic resolver should not run in direct one-agent chat")

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("helper", "Helper")],
        engine_factory=lambda identity: FakeEngine(),
        semantic_resolver=semantic_should_not_run,
    )

    result = await runtime.process_message(
        GroupMessage(text="hello", sender_id="alice", is_group=False)
    )

    assert seen["semantic"] == 0
    assert seen["task"] == "hello"
    assert result.published == {"helper": "direct reply"}


@pytest.mark.asyncio
async def test_group_scenario_matrix():
    events = []

    class FakeResult:
        def __init__(self, answer):
            self.answer = answer

    class FakeEngine:
        def __init__(self, agent_id):
            self.agent_id = agent_id
        async def run(self, task):
            events.append(("run", self.agent_id, task))
            return FakeResult(f"{self.agent_id}-reply")

    semantic_calls = []

    async def semantic_resolver(message, identities):
        semantic_calls.append(message.text)
        text = message.text.lower()
        if "backend person" in text:
            return ("david",)
        if "research person" in text:
            return ("sarah",)
        if "whoever handles ops" in text:
            return ("maya",)
        return ()

    runtime = GroupDualLobeRuntime(
        [
            AgentIdentity("sarah", "Sarah", aliases=("@sarah",), role="research"),
            AgentIdentity("david", "David", aliases=("@david",), role="backend engineering"),
            AgentIdentity("maya", "Maya", aliases=("@maya",), role="operations"),
        ],
        engine_factory=lambda identity: FakeEngine(identity.agent_id),
        semantic_resolver=semantic_resolver,
    )

    scenarios = [
        {
            "name": "explicit_name",
            "message": GroupMessage(text="Sarah, check the logs", is_group=True),
            "expected": {"sarah"},
            "semantic": False,
        },
        {
            "name": "explicit_mention",
            "message": GroupMessage(text="@david can you review this?", is_group=True),
            "expected": {"david"},
            "semantic": False,
        },
        {
            "name": "multiple_explicit_names",
            "message": GroupMessage(text="Sarah and David, compare your findings", is_group=True),
            "expected": {"sarah", "david"},
            "semantic": False,
        },
        {
            "name": "broadcast",
            "message": GroupMessage(text="Everyone, status?", is_group=True),
            "expected": {"sarah", "david", "maya"},
            "semantic": False,
        },
        {
            "name": "reply_metadata",
            "message": GroupMessage(text="can you fix it?", is_group=True, reply_to_agent_id="maya"),
            "expected": {"maya"},
            "semantic": False,
        },
        {
            "name": "explicit_platform_target",
            "message": GroupMessage(text="please review", is_group=True, explicit_target_ids=("david",)),
            "expected": {"david"},
            "semantic": False,
        },
        {
            "name": "implicit_role_backend",
            "message": GroupMessage(text="Can the backend person check this?", is_group=True),
            "expected": {"david"},
            "semantic": True,
        },
        {
            "name": "implicit_role_research",
            "message": GroupMessage(text="Can the research person investigate this?", is_group=True),
            "expected": {"sarah"},
            "semantic": True,
        },
        {
            "name": "implicit_role_ops",
            "message": GroupMessage(text="Whoever handles ops, check the deployment.", is_group=True),
            "expected": {"maya"},
            "semantic": True,
        },
        {
            "name": "mention_not_address",
            "message": GroupMessage(text="Sarah said the logs looked clean.", is_group=True),
            "expected": set(),
            "semantic": True,
        },
        {
            "name": "human_to_human_with_agent_present",
            "message": GroupMessage(text="John, did you finish the spreadsheet?", sender_id="alice", is_group=True),
            "expected": set(),
            "semantic": True,
        },
        {
            "name": "informational_statement",
            "message": GroupMessage(text="The deployment finished at noon.", is_group=True),
            "expected": set(),
            "semantic": True,
        },
    ]

    for scenario in scenarios:
        before_sem = len(semantic_calls)
        before_runs = len(events)
        result = await runtime.process_message(scenario["message"])
        got = set(result.published)
        assert got == scenario["expected"], scenario["name"]
        sem_used = len(semantic_calls) > before_sem
        assert sem_used is scenario["semantic"], scenario["name"]
        invoked = {agent_id for kind, agent_id, _ in events[before_runs:] if kind == "run"}
        assert invoked == scenario["expected"], scenario["name"]


@pytest.mark.asyncio
async def test_group_scenario_single_agent_direct_vs_group():
    calls = []

    class FakeResult:
        answer = "ok"

    class FakeEngine:
        async def run(self, task):
            calls.append(task)
            return FakeResult()

    semantic_calls = []
    async def semantic_none(message, identities):
        semantic_calls.append(message.text)
        return ()

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("helper", "Helper", aliases=("@helper",), role="assistant")],
        engine_factory=lambda identity: FakeEngine(),
        semantic_resolver=semantic_none,
    )

    # Direct: zero-routing fast path.
    direct = await runtime.process_message(GroupMessage(text="hello", is_group=False))
    assert direct.published == {"helper": "ok"}
    assert calls == ["hello"]
    assert semantic_calls == []

    # Group, not addressed: stay silent and use semantic fallback.
    group_unaddressed = await runtime.process_message(
        GroupMessage(text="Alice, can you send me the file?", sender_id="bob", is_group=True)
    )
    assert group_unaddressed.published == {}
    assert calls == ["hello"]
    assert semantic_calls == ["Alice, can you send me the file?"]

    # Group, addressed: deterministic name match, no new semantic call.
    group_addressed = await runtime.process_message(
        GroupMessage(text="Helper, can you send me the file?", sender_id="bob", is_group=True)
    )
    assert group_addressed.published == {"helper": "ok"}
    assert len(calls) == 2
    assert semantic_calls == ["Alice, can you send me the file?"]
