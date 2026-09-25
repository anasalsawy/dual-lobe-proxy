from dual_lobe_crewai.engines import SplitEngine
from dual_lobe_crewai.models import SplitPlan, SplitFragment


def test_accepts_semantic_split_with_a_and_b():
    e = SplitEngine()
    p = SplitPlan(
        mode="split",
        reason="test",
        fragments=[
            SplitFragment(owner="A", task="half a"),
            SplitFragment(owner="B", task="half b"),
        ],
        merge="integrate",
    )
    assert e._accept_split(p) is True


def test_rejects_normal_mode():
    e = SplitEngine()
    p = SplitPlan(mode="normal", reason="test")
    assert e._accept_split(p) is False


def test_rejects_invalid_fragment_ownership():
    e = SplitEngine()
    p = SplitPlan(
        mode="split",
        reason="test",
        fragments=[
            SplitFragment(owner="A", task="half a"),
            SplitFragment(owner="A", task="half b"),
        ],
        merge="append",
    )
    assert e._accept_split(p) is False


from dual_lobe_crewai.json_utils import parse_model
from dual_lobe_crewai.models import Verdict


def test_empty_verdict_fails_closed_to_yellow():
    fallback = Verdict(deception_level="YELLOW", rationale="parse failed")
    got = parse_model("", Verdict, fallback)
    assert got.deception_level == "YELLOW"
    assert got.rationale == "parse failed"


def test_empty_object_verdict_fails_closed_to_yellow():
    fallback = Verdict(deception_level="YELLOW", rationale="parse failed")
    got = parse_model("{}", Verdict, fallback)
    assert got.deception_level == "YELLOW"


def test_bad_splitter_output_uses_explicit_fallback():
    fallback = SplitPlan(
        mode="normal",
        reason="ROUTING_PARSE_FAILURE: splitter output was empty, truncated, or invalid JSON",
    )
    got = parse_model('{"mode":"split","fragments":[', SplitPlan, fallback)
    assert got.mode == "normal"
    assert got.reason.startswith("ROUTING_PARSE_FAILURE")


def test_splitplan_empty_object_cannot_silently_validate_normal():
    fallback = SplitPlan(mode="normal", reason="ROUTING_PARSE_FAILURE")
    got = parse_model("{}", SplitPlan, fallback)
    assert got.reason == "ROUTING_PARSE_FAILURE"


import pytest
import dual_lobe_crewai.engines as engines_module


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


from dual_lobe_crewai.json_utils import extract_json_object
from dual_lobe_crewai.memory import JsonlMemoryStore
from dual_lobe_crewai.tools import ProxyRunState


def test_extract_first_balanced_object_when_two_objects():
    got = extract_json_object('{"a":1} {"b":2}')
    assert got == {"a": 1}


def test_unicode_memory_search(tmp_path):
    s = JsonlMemoryStore(str(tmp_path / "m.jsonl"))
    s.record("مرحبا العالم هذا اختبار الذاكرة")
    assert s.search("مرحبا العالم")


def test_run_scoped_proxy_cap_claims_once():
    state = ProxyRunState()
    assert state.claim("delegate") is True
    assert state.claim("delegate") is False
    assert state.claim("consult") is True
    assert state.claim("consult") is False


def test_route_source_can_distinguish_semantic_from_fallback():
    from dual_lobe_crewai.engines import RunResult
    from dual_lobe_crewai.models import Verdict
    r = RunResult(
        mode="split",
        answer="x",
        verdict=Verdict(deception_level="GREEN", rationale="ok"),
        logical_model_calls=3,
        route_source="fallback",
    )
    assert r.route_source == "fallback"
    assert r.logical_model_calls == 3
