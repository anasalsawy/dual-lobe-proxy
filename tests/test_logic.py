import time

import pytest

import dual_lobe_crewai.engines as engines_module
from dual_lobe_crewai.engines import SplitEngine, RunResult
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
        return '{"final_answer":"merged","answer_verdict":{"deception_level":"GREEN","rationale":"no deception detected"},'
        '"split_verdict":{"used":true,"valid":true,"score":90,"independence_score":0.9,'
        '"balance_score":0.8,"time_effect":"unknown","feedback":"reasonable split"}}'

    monkeypatch.setattr(SplitEngine, "_safe_run_one", fake_safe)
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
