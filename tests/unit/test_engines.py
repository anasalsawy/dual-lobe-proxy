"""Ported engines: split (model 1) and clinical (model 2) logic, with scripted A/B replies."""
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
    assert aliases.count("lobe-b") >= 2                       # live B + final adversarial B
    assert any("YOUR DELEGATED SUBTASK" in c[1][1]["content"] for c in fake.calls)
    first_a = next(c for c in fake.calls if c[0] == "lobe-a")
    a_tools = {t["function"]["name"] for t in first_a[2]}
    assert {"delegate", "delegate_collect", "consult_other_lobe", "b_live_check", "memory_search"} <= a_tools
    assert result.logical_model_calls == 2 + 1 + result.timings_ms["b_live_calls"]
    assert (tmp_path / "m.jsonl").exists() and (tmp_path / "m.b.jsonl").exists()


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


async def test_clinical_a_plans_on_sanitized_data_b_executes_raw_a_reviews(scripted):
    seen = {}

    def handler(alias, req):
        user = req.messages[1]["content"]
        if alias == "lobe-a" and "B has executed the plan" in req.messages[1]["content"] + req.messages[0]["content"]:
            seen["review"] = user
            return _msg("Final: hold spironolactone.")
        if alias == "lobe-a":
            seen["plan"] = user
            return _msg(json.dumps(PLAN))
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
    assert all(not c[2] for c in fake.calls if c[0] == "lobe-a")                   # A has no tools
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
            return _msg(json.dumps(PLAN))
        if len(req.messages) == 2:
            return _msg(tool_calls=[_call("consult_planner", {"concern": "K too high", "evidence": "6.8"})])
        assert "PLAN_REVISED revision=1" in req.messages[-1]["content"]
        return _msg(json.dumps({"plan_revision": 1, "steps": [{"id": "S1", "status": "completed"}]}))

    scripted(handler)
    result = await ClinicalDualLobeEngine().run_clinical(query="q", patient_context="")
    assert result.plan_revision == 1
    assert result.logical_model_calls == 4


async def test_clinical_report_outside_plan_is_rejected(scripted):
    def handler(alias, req):
        if alias == "lobe-a":
            return _msg(json.dumps(PLAN))
        return _msg(json.dumps({"plan_revision": 0, "steps": [{"id": "S9", "status": "completed"}]}))

    scripted(handler)
    with pytest.raises(RuntimeError, match="outside the current plan"):
        await ClinicalDualLobeEngine().run_clinical(query="q", patient_context="")
