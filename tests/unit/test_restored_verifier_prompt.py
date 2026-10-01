from dual_lobe.b.prompts import B_SYSTEM, CYCLE_PROMPT
from dual_lobe.b.verification import build_prompt, verifier_system
import json

import pytest

from dual_lobe.b.protocol import Review, parse_review


def test_restored_contract_has_full_meter_evidence_quote_and_solution_fields():
    for field in ('"goal"', '"deception_level"', '"meter_rationale"',
                  '"evidence_request"', '"questions"', '"next_step"',
                  '"context_notes"', '"concerns"', '"signal"',
                  '"claim_quote"', '"basis_quote"', '"reason"', '"suggestion"'):
        assert field in CYCLE_PROMPT
    assert "Run these five checks" in B_SYSTEM
    assert "OTHER DECEPTION" in B_SYSTEM


def test_review_schema_matches_the_original_two_concern_limit():
    assert Review.model_fields["concerns"].metadata[0].max_length == 2


def test_inline_verifier_requires_every_json_key_and_keeps_full_rationale():
    payload = {
        "goal": "answer the user",
        "deception_level": "GREEN",
        "meter_rationale": "r" * 300,
        "evidence_request": None,
        "questions": [],
        "next_step": "",
        "context_notes": [],
        "concerns": [],
    }
    assert len(parse_review(json.dumps(payload), require_complete=True).meter_rationale) == 300
    del payload["evidence_request"]
    with pytest.raises(ValueError, match="required contract fields"):
        parse_review(json.dumps(payload), require_complete=True)


@pytest.mark.parametrize("level,concerns", [
    ("GREEN", [{"signal": "UNSUPPORTED", "claim_quote": "claim", "basis_quote": "",
                 "reason": "missing result", "suggestion": "qualify the claim"}]),
    ("RED", []),
])
def test_inline_verifier_enforces_meter_concern_invariants(level, concerns):
    payload = {
        "goal": "answer the user", "deception_level": level,
        "meter_rationale": "evidence-based explanation", "evidence_request": None,
        "questions": [], "next_step": "", "context_notes": [], "concerns": concerns,
    }
    with pytest.raises(ValueError):
        parse_review(json.dumps(payload), require_complete=True)


def test_b_candidate_uses_the_same_rubric_with_only_role_labels_adapted():
    system = verifier_system("B")
    prompt = build_prompt(candidate_lobe="B", context="conversation", output="candidate",
                          events="tool result", latest_request="latest user request")
    assert "Job 1: help Lobe B" in system
    assert '"goal": "original outcome' in prompt
    assert '"OUTPUT": "candidate"' in prompt
    assert "OBSERVATION_JSON:" in prompt


def test_inline_verification_does_not_clip_supplied_context():
    full_context = "x" * 100_000 + "UNIQUE_CONTEXT_TAIL"
    prompt = build_prompt(candidate_lobe="A", context=full_context, output="answer",
                          events="complete tool trace", latest_request="latest")
    assert "UNIQUE_CONTEXT_TAIL" in prompt
    assert len(prompt) > 100_000
