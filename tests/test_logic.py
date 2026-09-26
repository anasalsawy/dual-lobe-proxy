import time

import pytest

import dual_lobe_crewai.engines as engines_module
from dual_lobe_crewai.engines import SplitEngine, RunResult
from dual_lobe_crewai.json_utils import extract_json_object, parse_model
from dual_lobe_crewai.memory import JsonlMemoryStore
from dual_lobe_crewai.models import SplitQuality, TurnReview, Verdict
from dual_lobe_crewai.tools import ProxyRunState, SelfSplitRunState


def test_empty_verdict_fails_closed_to_yellow():
    fallback = Verdict(deception_level="YELLOW", rationale="parse failed")
    got = parse_model("", Verdict, fallback)
    assert got.deception_level == "YELLOW"
    assert got.rationale == "parse failed"


def test_empty_object_verdict_fails_closed_to_yellow():
    fallback = Verdict(deception_level="YELLOW", rationale="parse failed")
    got = parse_model("{}", Verdict, fallback)
    assert got.deception_level == "YELLOW"


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


def test_split_timing_proxy_positive_when_parallel_saving_exceeds_merge():
    state = SelfSplitRunState()
    base = time.perf_counter()
    state.split_used = True
    state.split_started_perf = base + 1.0
    state.b_started_perf = base + 1.0
    state.b_finished_perf = base + 5.0
    telemetry = SplitEngine._timing_telemetry(
        state,
        primary_started=base,
        primary_finished=base + 6.0,
        merge_ms=500,
    )
    assert telemetry["a_half_ms"] >= 4900
    assert telemetry["b_half_ms"] >= 3900
    assert telemetry["parallel_gain_proxy_ms"] > 3000
    assert telemetry["measured_time_effect"] == "positive"
    state.executor.shutdown(wait=False, cancel_futures=True)


def test_split_timing_proxy_negative_when_merge_dominates():
    state = SelfSplitRunState()
    base = time.perf_counter()
    state.split_used = True
    state.split_started_perf = base
    state.b_started_perf = base
    state.b_finished_perf = base + 1.0
    telemetry = SplitEngine._timing_telemetry(
        state,
        primary_started=base,
        primary_finished=base + 1.1,
        merge_ms=1800,
    )
    assert telemetry["parallel_gain_proxy_ms"] < 0
    assert telemetry["measured_time_effect"] == "negative"
    state.executor.shutdown(wait=False, cancel_futures=True)


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
