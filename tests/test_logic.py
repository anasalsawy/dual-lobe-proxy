import time

import pytest

import dual_lobe_crewai.engines as engines_module
from dual_lobe_crewai.engines import DualLobeEngine, SplitEngine
from dual_lobe_crewai.group_coordination import (
    AgentIdentity,
    GroupCoordinator,
    GroupDualLobeRuntime,
    GroupMessage,
)
from dual_lobe_crewai.json_utils import parse_model
from dual_lobe_crewai.memory import JsonlMemoryStore
from dual_lobe_crewai.models import AdversarialReview, Verdict
from dual_lobe_crewai.prompts import (
    ADVERSARIAL_PROTOCOL,
    A_PERSONA,
    B_ADVERSARY_PERSONA,
    VERIFICATION_PROTOCOL,
)
from dual_lobe_crewai.live_b import LiveBMonitor
from dual_lobe_crewai.tools import LobeConsultTool, LiveBState, ProxyToolEvent, ProxyToolTrace


def test_compatibility_alias_points_to_current_engine():
    assert SplitEngine is DualLobeEngine


def test_empty_verdict_fails_closed_to_yellow():
    fallback = Verdict(deception_level="YELLOW", rationale="parse failed")
    got = parse_model("", Verdict, fallback)
    assert got.deception_level == "YELLOW"


def test_green_with_unverified_is_hardened_to_yellow():
    verdict = Verdict(
        deception_level="GREEN",
        rationale="looked fine",
        handoff={"unverified": ["deployment actually happened"]},
    )
    hardened = DualLobeEngine._harden_verdict(verdict)
    assert hardened.deception_level == "YELLOW"


def test_green_with_proof_request_is_hardened_to_yellow():
    verdict = Verdict(
        deception_level="GREEN",
        rationale="looked fine",
        handoff={"proof_requests": ["read the actual artifact"]},
    )
    hardened = DualLobeEngine._harden_verdict(verdict)
    assert hardened.deception_level == "YELLOW"


def test_anti_deception_protocol_is_strict():
    required = [
        "ORIGINAL USER INTENT",
        "CLAIM LEDGER",
        "OBSERVED",
        "INFERRED",
        "ASSUMED",
        "UNKNOWN",
        "ACTION AND ARTIFACT CLAIMS REQUIRE POSITIVE PROOF",
        "ACTIVELY SEEK DISCONFIRMING EVIDENCE",
        "EXACT-ANSWER BINDING",
        "FAIL CLOSED",
    ]
    for item in required:
        assert item in VERIFICATION_PROTOCOL


def test_a_prompt_defines_delegation_as_speed_primitive():
    assert "COMPUTE-ACCELERATION PRIMITIVE" in A_PERSONA
    assert "not management" in A_PERSONA.lower()
    assert "reduce the user's waiting time" in A_PERSONA
    assert "MUST delegate" in A_PERSONA


def test_b_prompt_is_adversarial_not_polite_reviewer():
    persona = B_ADVERSARY_PERSONA.lower()
    protocol = ADVERSARIAL_PROTOCOL.lower()
    assert "persistent independent adversary" in persona
    assert "assume this may be wrong" in persona
    assert "user's actual intent" in persona
    assert "missing fact" in persona
    assert "existing system" in persona or "existing category" in persona
    assert "try to break the core logic" in protocol
    assert "attack user-goal fit" in protocol
    assert "find the fact that changes the whole approach" in protocol


def test_proxy_trace_preserves_full_evidence_and_can_snapshot():
    trace = ProxyToolTrace()
    payload = "x" * 12000
    trace.add("artifact_read", input_text="read", output_text=payload, provenance="artifact")
    assert payload in trace.render()
    snap = trace.snapshot_from(0)
    assert len(snap) == 1
    assert snap[0].output_text == payload


def test_proxy_trace_explicit_limit_marks_truncation():
    trace = ProxyToolTrace()
    trace.add("artifact_read", output_text="y" * 1000, provenance="artifact")
    rendered = trace.render(max_chars_per_event=100)
    assert "TRUNCATED_BY_RENDER" in rendered
    assert "original_chars=1000" in rendered


def test_live_b_state_streams_intervention_once_to_a():
    state = LiveBState()
    state.add_intervention("A is assuming the API supports writes.", severity="critical")
    first = state.drain_for_a()
    second = state.drain_for_a()
    assert "CRITICAL" in first
    assert "API supports writes" in first
    assert second == ""




def test_nonblocking_consultation_posts_for_existing_live_b_loop():
    state = LiveBState()
    trace = ProxyToolTrace()
    tool = LobeConsultTool(live_b_state=state, trace=trace)

    out = tool._run(
        blocker="same API failure twice",
        what_i_tried="retried same endpoint",
        what_i_need="different frame",
        current_hypothesis="wrong endpoint shape",
    )

    assert "CONSULTATION_POSTED_NONBLOCKING" in out
    pending = state.drain_consultations_for_b()
    assert len(pending) == 1
    assert pending[0]["blocker"] == "same API failure twice"
    assert trace.snapshot_from(0)[0].provenance == "a_to_b_consultation"


def test_deterministic_stuck_detector_fires_without_model_call(tmp_path):
    state = LiveBState()
    trace = ProxyToolTrace()
    monitor = LiveBMonitor(
        task="fix it",
        b_memory=JsonlMemoryStore(str(tmp_path / "b.jsonl")),
        trace=trace,
        state=state,
    )
    repeated = ProxyToolEvent(
        name="tool_x",
        input_text="same",
        output_text="ERROR: timeout contacting endpoint",
        provenance="tool",
    )

    monitor._detect_stuck([repeated])
    assert state.drain_for_a() == ""
    monitor._detect_stuck([repeated])
    intervention = state.drain_for_a()

    assert "Repeated failure detected" in intervention
    assert any(e.name == "b_stuck_detector" for e in trace.snapshot_from(0))


def test_split_experience_memory_can_be_retrieved_as_strategy_prior(tmp_path):
    store = JsonlMemoryStore(str(tmp_path / "a.jsonl"))
    store.record_split_experience(
        "Task: inspect API integration\nOutcome verdict: GREEN\n"
        "Strategy: inspect schema before retrying endpoint"
    )
    got = store.split_experience_slice("API endpoint integration")
    assert "inspect schema before retrying endpoint" in got


def test_b_has_independent_default_memory(tmp_path):
    a = JsonlMemoryStore(str(tmp_path / "agent.jsonl"))
    engine = DualLobeEngine(memory=a)
    assert engine.b_memory.path != engine.memory.path
    assert ".b" in engine.b_memory.path.name


@pytest.mark.asyncio
async def test_safe_run_one_fails_to_fallback(monkeypatch, tmp_path):
    async def boom(*args, **kwargs):
        raise ValueError("empty")

    monkeypatch.setattr(engines_module, "run_one", boom)
    e = DualLobeEngine(memory=JsonlMemoryStore(str(tmp_path / "a.jsonl")))
    out = await e._safe_run_one(
        object(),
        "x",
        "y",
        fallback_text="FALLBACK",
        role_key="A",
    )
    assert out.startswith("FALLBACK")


def test_group_coordinator_only_enforces_validated_b_targets():
    group = GroupCoordinator([
        AgentIdentity("sarah", "Sarah"),
        AgentIdentity("david", "David"),
    ])
    deliveries = group.route_to_targets(("david", "invented"))
    assert deliveries["david"].can_emit is True
    assert deliveries["sarah"].can_emit is False
    assert deliveries["david"].addressed_agents == ("david",)


@pytest.mark.asyncio
async def test_group_mode_b_routes_and_only_addressed_a_runs():
    calls = []
    seen_history = []

    class FakeResult:
        def __init__(self, answer):
            self.answer = answer

    class FakeEngine:
        def __init__(self, agent_id):
            self.agent_id = agent_id

        async def run(self, task):
            calls.append((self.agent_id, task))
            return FakeResult(f"{self.agent_id}-reply")

    async def b_router(message, identities, history):
        seen_history.append(history)
        assert message.text == "Can the backend person check this?"
        return ("david",), "The message directly calls on the backend role."

    runtime = GroupDualLobeRuntime(
        [
            AgentIdentity("sarah", "Sarah", role="research"),
            AgentIdentity("david", "David", role="backend"),
        ],
        engine_factory=lambda identity: FakeEngine(identity.agent_id),
        b_router=b_router,
    )

    result = await runtime.process_message(
        GroupMessage(
            text="Can the backend person check this?",
            sender_id="alice",
            group_id="g1",
            is_group=True,
        )
    )
    assert [x[0] for x in calls] == ["david"]
    assert result.published == {"david": "david-reply"}
    assert result.suppressed == ("sarah",)
    assert "backend role" in result.router_reason
    assert seen_history == [()]


@pytest.mark.asyncio
async def test_b_group_router_receives_conversation_history():
    histories = []

    class FakeResult:
        answer = "ok"

    class FakeEngine:
        async def run(self, task):
            return FakeResult()

    async def b_router(message, identities, history):
        histories.append(history)
        if "Sarah" in message.text:
            return ("sarah",), "Sarah addressed"
        return (), "nobody addressed"

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("sarah", "Sarah"), AgentIdentity("david", "David")],
        engine_factory=lambda identity: FakeEngine(),
        b_router=b_router,
    )

    await runtime.process_message(GroupMessage(text="Sarah, status?", group_id="g", is_group=True))
    await runtime.process_message(GroupMessage(text="what about that?", group_id="g", is_group=True))

    assert histories[0] == ()
    assert histories[1]
    assert histories[1][0]["text"] == "Sarah, status?"
    assert histories[1][0]["addressed_agent_ids"] == ["sarah"]


@pytest.mark.asyncio
async def test_group_router_failure_fails_closed_without_metadata():
    class FakeEngine:
        async def run(self, task):
            raise AssertionError("A should not be invoked")

    async def broken_router(message, identities, history):
        raise RuntimeError("router down")

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("sarah", "Sarah"), AgentIdentity("david", "David")],
        engine_factory=lambda identity: FakeEngine(),
        b_router=broken_router,
    )
    result = await runtime.process_message(
        GroupMessage(text="someone check this", is_group=True)
    )
    assert result.published == {}
    assert set(result.suppressed) == {"sarah", "david"}


@pytest.mark.asyncio
async def test_direct_single_agent_bypasses_group_router():
    called = {"router": 0, "task": ""}

    class FakeResult:
        answer = "hello"

    class FakeEngine:
        async def run(self, task):
            called["task"] = task
            return FakeResult()

    async def router(message, identities, history):
        called["router"] += 1
        return (), ""

    runtime = GroupDualLobeRuntime(
        [AgentIdentity("solo", "Solo")],
        engine_factory=lambda identity: FakeEngine(),
        b_router=router,
    )
    result = await runtime.process_message(GroupMessage(text="hi", is_group=False))
    assert called["router"] == 0
    assert called["task"] == "hi"
    assert result.published == {"solo": "hello"}
