"""Gated streaming: A's text streams live, B's meter arrives in the last chunk."""
import json

import dual_lobe.api.chat  # noqa: F401  (import order: the api package loads gated.handler's deps)
from dual_lobe.gated import handler


class Fake:
    def __init__(self):
        self.order = []
        self.b_response_format = None

    def adapter(self, alias):
        outer = self

        class A:
            async def stream(self, req):
                outer.order.append(("a-stream", alias))
                for piece in ("Canberra is the capital.\n\n🛡️ Deception Meter\n🟢 GREEN\nself-rating"):
                    yield {"choices": [{"delta": {"content": piece}}]}

            async def buffered(self, req):
                outer.order.append(("buffered", alias))
                outer.b_response_format = req.response_format
                return {"choices": [{"message": {"content": json.dumps(
                    {"deception_level": "GREEN", "meter_rationale": "Correct."})}}]}
        return A()


async def test_gated_stream_sends_text_before_b_and_meter_last(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(handler, "get_registry", lambda: fake)
    events = [e async for e in handler.gated_stream(
        {"model": "sawii/dl-bidirectional", "stream": True, "messages": [{"role": "user", "content": "capital?"}]},
        "run-1", 1, "sawii/dl-bidirectional")]
    chunks = [json.loads(e[6:]) for e in events if e.startswith("data: {")]
    texts = [c["choices"][0]["delta"].get("content") for c in chunks]
    visible = "".join(t for t in texts if t)
    assert visible.startswith("Canberra is the capital.")
    assert visible.count("Deception Meter") == 1  # only the proxy-owned meter is visible
    assert "self-rating" not in visible
    assert "> *Rationale:* Correct\\." in visible
    assert chunks[-1]["dual_lobe"]["meter"] == "GREEN"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert fake.order[0] == ("a-stream", "lobe-a") and ("buffered", "lobe-b") in fake.order
    assert fake.b_response_format == {"type": "json_object"}
    assert events[-1] == "data: [DONE]\n\n"
