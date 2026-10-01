import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dual_lobe.gated import handler
from dual_lobe.core.settings import Settings
from dual_lobe.b import verification
from dual_lobe.b.protocol import Review


@pytest.mark.parametrize("value, answer, evidence, expected", [
    ({"deception_level": "GREEN", "meter_rationale": "Supported."}, "answer", "tool result", "GREEN"),
    ({"deception_level": "BANANA", "meter_rationale": "Fine."}, "answer", "", "YELLOW"),
    ({"deception_level": "GREEN", "meter_rationale": "Fine.", "unverified": ["claim"]}, "answer", "", "GREEN"),
    ({"deception_level": "RED", "meter_rationale": "Contradiction.", "concerns": []}, "answer", "evidence", "RED"),
    ({"deception_level": "RED", "meter_rationale": "Contradiction.", "concerns": [
        {"claim_quote": "answer", "evidence_quote": "tool result", "reason": "conflicts"}]},
     "answer", "tool result", "RED"),
])
def test_meter_requires_a_valid_evidence_based_rating(value, answer, evidence, expected):
    level, _, _ = handler._verified_meter(value, answer, evidence)
    assert level == expected


class _VerifierFailure:
    async def buffered(self, _request):
        raise TimeoutError("B timed out")


class _Registry:
    def adapter(self, _alias):
        return _VerifierFailure()


@pytest.mark.asyncio
async def test_missing_verification_never_emits_green(monkeypatch):
    monkeypatch.setattr(handler, "get_settings", lambda: Settings(
        _env_file=None, gated_b_handoff=False, gated_flip_back=False, b_timeout=0.1,
        max_shadow_input_chars=10000))
    monkeypatch.setattr(verification, "verify_output", AsyncMock(side_effect=TimeoutError("B timed out")))
    answer = {"choices": [{"message": {"role": "assistant", "content": "I completed it."},
                           "finish_reason": "stop"}]}
    messages = [{"role": "user", "content": "Please complete it."}]

    result, headers = await handler._complete(
        answer, payload={"messages": messages}, run_id="verification-timeout",
        tenant_id=1, public_model="sawii/dl-bidirectional", shared_space=None,
        enriched_messages=messages, a_adapter=SimpleNamespace(), proxy_used={})

    assert headers["X-Dual-Lobe-Meter"] == "YELLOW"
    assert "unverified" in result["choices"][0]["message"]["content"].lower()


@pytest.mark.asyncio
async def test_plain_greeting_still_receives_mandatory_verification(monkeypatch):
    monkeypatch.setattr(handler, "get_settings", lambda: Settings(
        _env_file=None, gated_b_handoff=False, gated_flip_back=False))
    verify = AsyncMock(return_value=(Review(
        goal="greeting", deception_level="GREEN", meter_rationale="No deception detected.",
        evidence_request=None, questions=[], next_step="", context_notes=[], concerns=[]), ""))
    monkeypatch.setattr(verification, "verify_output", verify)
    messages = [{"role": "user", "content": "hey"}]
    result, headers = await handler._complete(
        {"choices": [{"message": {"role": "assistant", "content": "Hello! How can I help?"}}]},
        payload={"messages": messages}, run_id="plain-greeting", tenant_id=1,
        public_model="sawii/dl-bidirectional", shared_space=None,
        enriched_messages=messages, a_adapter=SimpleNamespace(), proxy_used={})
    assert result["choices"][0]["message"]["content"].startswith("Hello! How can I help?")
    assert "Deception Meter" in result["choices"][0]["message"]["content"]
    assert headers["X-Dual-Lobe-Meter"] == "GREEN"
    assert verify.await_count == 1


@pytest.mark.asyncio
async def test_non_object_verifier_json_is_retried_then_fails(monkeypatch):
    class InvalidVerifier:
        def __init__(self):
            self.calls = 0

        async def buffered(self, _request):
            self.calls += 1
            return {"choices": [{"message": {"content": json.dumps(["not", "an", "object"])}}]}

    verifier = InvalidVerifier()

    class Registry:
        def adapter(self, _alias):
            return verifier

    monkeypatch.setattr(handler, "get_settings", lambda: Settings(_env_file=None, b_max_output_tokens=128))
    monkeypatch.setattr(handler, "get_registry", lambda: Registry())
    with pytest.raises(ValueError, match="not an object"):
        await handler._call_b_json("system", "verify", "contract")
    assert verifier.calls == 2
