"""Gated streaming: A's text streams live, B's meter arrives in the last chunk."""
import json

import dual_lobe.api.chat  # noqa: F401  (import order: the api package loads gated.handler's deps)
from dual_lobe.gated import handler
from dual_lobe.b import verification
from dual_lobe.b.protocol import Review
from unittest.mock import AsyncMock


class Fake:
    def __init__(self):
        self.order = []

    def adapter(self, alias):
        outer = self

        class A:
            async def stream(self, req):
                outer.order.append(("a-stream", alias))
                for piece in ("Canberra is the capital.\n\n🛡️ Deception Meter\n🟢 GREEN\nself-rating"):
                    yield {"choices": [{"delta": {"content": piece}}]}

        return A()


async def test_gated_stream_sends_text_before_b_and_meter_last(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(handler, "get_registry", lambda: fake)
    verify = AsyncMock(return_value=(Review(
        goal="capital question", deception_level="GREEN", meter_rationale="No deception detected.",
        evidence_request=None, questions=[], next_step="", context_notes=[], concerns=[]), ""))
    monkeypatch.setattr(verification, "verify_output", verify)
    events = [e async for e in handler.gated_stream(
        {"model": "sawii/dl-bidirectional", "stream": True, "messages": [{"role": "user", "content": "capital?"}]},
        "run-1", 1, "sawii/dl-bidirectional")]
    chunks = [json.loads(e[6:]) for e in events if e.startswith("data: {")]
    texts = [c["choices"][0]["delta"].get("content") for c in chunks]
    visible = "".join(t for t in texts if t)
    assert visible.startswith("Canberra is the capital.")
    assert visible.count("Deception Meter") == 1  # only the proxy-owned meter is visible
    assert "self-rating" not in visible
    assert "<small><strong>Rationale:</strong> No deception detected.</small>" in visible
    assert chunks[-1]["dual_lobe"]["meter"] == "GREEN"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert fake.order[0] == ("a-stream", "lobe-a")
    assert verify.await_count == 1
    assert events[-1] == "data: [DONE]\n\n"
