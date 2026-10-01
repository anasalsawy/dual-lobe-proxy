"""Gated streaming: A's text streams live, B's meter arrives in the last chunk."""
import json

import dual_lobe.api.chat  # noqa: F401  (import order: the api package loads gated.handler's deps)
from dual_lobe.gated import handler


class Fake:
    def __init__(self):
        self.order = []

    def adapter(self, alias):
        outer = self

        class A:
            async def stream(self, req):
                outer.order.append(("a-stream", alias))
                for piece in ("Canberra ", "is the capital."):
                    yield {"choices": [{"delta": {"content": piece}}]}

            async def buffered(self, req):
                outer.order.append(("buffered", alias))
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
    assert texts[1:3] == ["Canberra ", "is the capital."]            # streamed before B ran
    assert "Deception Meter: GREEN" in "".join(t for t in texts if t)  # meter appended after
    assert chunks[-1]["dual_lobe"]["meter"] == "GREEN"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert fake.order[0] == ("a-stream", "lobe-a") and ("buffered", "lobe-b") in fake.order
    assert events[-1] == "data: [DONE]\n\n"
