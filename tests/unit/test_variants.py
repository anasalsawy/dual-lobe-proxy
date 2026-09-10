"""Explicit policy variants for later head-to-head evaluation."""
import json

from dual_lobe.b import prompts
from dual_lobe.b.protocol import Review
from dual_lobe.b.variant_policy import gate_allows, policy


def review(**extra):
    value = {
        "goal": "g", "questions": [], "next_step": "", "context_notes": [],
        "knowledge_notes": [], "concerns": [],
        "deception_level": "GREEN", "deception_reason": "",
    }
    value.update(extra)
    return Review.model_validate(value)


def test_variants_are_explicit_and_default_is_nonblocking():
    assert policy(None).name == "peer-observer"
    assert not policy("peer-observer").synchronous_gate
    assert policy("strict-gatekeeper").synchronous_gate
    assert policy("strategist-executor").exclusive_roles
    assert policy("director").visible_director


def test_strict_gate_requires_both_explicit_release_fields():
    assert not gate_allows(review())
    assert not gate_allows(review(gate_decision="ALLOW"))
    assert not gate_allows(review(proof_coverage="complete"))
    assert gate_allows(review(gate_decision="ALLOW", proof_coverage="complete"))
    assert not gate_allows(review(gate_decision="BLOCK", proof_coverage="complete"))


def test_gate_prompt_is_fail_closed_and_retains_machine_readable_evidence():
    prompt = prompts.build_gatekeeper_prompt(
        "[canonical messages]", "A says it is done", "", "{}", max_chars=12000,
        host_tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    assert "STRICT GATEKEEPER VARIANT" in prompt
    assert "gate_decision" in prompt and "proof_coverage" in prompt
    evidence = json.loads(prompt.split(prompts.EVIDENCE_MARKER, 1)[1])
    assert evidence["CONTEXT"] == "[canonical messages]"
    assert evidence["HOST_TOOLS"][0]["function"]["name"] == "read_file"
