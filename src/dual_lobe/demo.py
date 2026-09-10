"""Offline plumbing demonstration. All A/B content below is a labelled fixture."""
from __future__ import annotations

import json
import time

from .api.chat import _effective_messages
from .b.channels import prepare_context, reviewed_state
from .b.prompts import build_cycle_prompt
from .b.protocol import ground_review, parse_review
from .core.settings import Settings


def run() -> dict:
    settings = Settings(_env_file=None, context_memory_enabled=True, claim_checks_enabled=True,
                        b_state_ttl_seconds=180, context_memory_ttl_seconds=86400)
    user = {"role": "user", "content": "Build a Windows application; the startup script keeps failing."}
    observation = "Original request: build a Windows application. Reported tool result: tests failed."
    output = "All tests passed."
    # Hand-authored fixture: this demo does not measure model judgement or honesty.
    fixture = {
        "goal": "Build a working Windows application",
        "questions": ["Does the startup script assume a Unix shell?"],
        "next_step": "Inspect the startup command's platform prerequisite.",
        "context_notes": ["Installation on Windows is part of the requested outcome."],
        "concerns": [{"signal": "CONTRADICTION", "claim_quote": output,
                      "basis_quote": "tests failed", "reason": "The supplied test result disagrees.",
                      "suggestion": "Correct the completion claim and investigate the failure."}],
    }
    first = prepare_context(None, "", 1, settings)
    first_messages = _effective_messages([user], first, True)
    prompt = build_cycle_prompt(observation, output, "", "")
    review = ground_review(parse_review(json.dumps(fixture)), prompt)
    state = reviewed_state({}, review, {"run_id": "offline-demo", "source_call": "fixture-1",
                                        "observed_at": time.time()})
    second = prepare_context(state, "", 1, settings)
    second_messages = _effective_messages([user], second, True)
    # Later claim expiry must not erase the broader mission memory.
    later = prepare_context(state, "", 1, settings, now=state["observed_at"] + 181)
    checks = {
        "user_message_unchanged": first_messages[-1] == second_messages[-1] == user,
        "monitoring_instruction_present_on_both_calls": all(m[0]["role"] == "system" for m in (first_messages, second_messages)),
        "memory_loaded_on_next_call": second.memory_version == 1,
        "claim_findings_separate_from_memory": bool(second.claims_text) and output not in second.memory_text,
        "claim_expiry_does_not_erase_memory": bool(later.memory_text) and not later.claims_text,
    }
    return {"mode": "OFFLINE FIXTURES — no model, database, or provider was contacted",
            "checks": checks, "call_1_delivery": first.receipt(),
            "call_2_delivery": second.receipt(), "call_2_model_input": second_messages}


def main() -> None:
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not all(result["checks"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
