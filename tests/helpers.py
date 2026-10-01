"""Dumb fakes for upstream providers and the registry, without any network I/O."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeAdapter:
    def __init__(self, content: str = "pong!", fail: Exception | None = None, stream_deltas: list | None = None):
        self.content = content
        self.fail = fail
        self.stream_deltas = stream_deltas or [
            {"delta": {"content": "po"}, "finish_reason": None},
            {"delta": {"content": "ng!"}, "finish_reason": "stop"},
        ]
        self.last_request = None

    async def buffered(self, req):
        self.last_request = req
        if self.fail is not None:
            raise self.fail
        return {
            "id": "fake-cmpl",
            "object": "chat.completion",
            "model": "fake",
            "created": 0,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": self.content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

    async def stream(self, req):
        self.last_request = req
        if self.fail is not None:
            raise self.fail
        for d in self.stream_deltas:
            yield {"id": "fake-stream", "object": "chat.completion.chunk",
                   "created": 0, "model": "fake",
                   "choices": [{"index": 0, **d}]}


def fake_target(alias: str = "lobe-a", model: str = "fake") -> SimpleNamespace:
    return SimpleNamespace(alias=alias, model=model, enabled=True)


class FakeRegistry:
    def __init__(self, a: FakeAdapter | None = None, b: FakeAdapter | None = None):
        self._a = a or FakeAdapter("pong!")
        self._b = b or FakeAdapter("ignored")
        self._targets = {
            "sawii/dl-bidirectional": fake_target("sawii/dl-bidirectional"),
            "sawii/dl-secure": fake_target("sawii/dl-secure"),
            "lobe-a": fake_target("lobe-a"), "lobe-b": fake_target("lobe-b"),
            "sawii/dual-lobe": fake_target("sawii/dual-lobe"),
        }

    def target(self, alias: str | None = None):
        return self._targets.get(alias) if alias else self._targets["sawii/dl-bidirectional"]

    def adapter(self, alias: str | None = None):
        key = alias or "sawii/dl-bidirectional"
        return self._b if key == "lobe-b" else self._a

    def models(self) -> list[dict[str, Any]]:
        return [{"id": alias, "logical_model": self._targets[alias].model, "capabilities": {}}
                for alias in ("sawii/dl-bidirectional", "sawii/dl-secure")]


class FakeBExtractor:
    """Deterministic `_call_b` replacement for worker tests."""

    def __init__(self, payload: dict | None = None, fail: Exception | None = None):
        self.payload = payload or {
            "oversight": {"pulse": "post", "challenges": []},
            "context_injection": [],
            "evidence_ledger": [],
            "blocker_insights": [],
            "required_preconditions": [],
            "state_corrections": [],
            "unasked_questions": [],
            "hidden_assumptions": [],
            "context_expansion": [],
            "blocker_hypotheses": [],
            "severity": "LOW",
        }
        self.fail = fail
        self.calls = 0

    async def __call__(self, target_alias: str, prompt: str) -> dict:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return self.payload
