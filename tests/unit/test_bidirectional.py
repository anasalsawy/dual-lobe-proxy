from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dual_lobe.bidirectional import handler


def test_speaker_routing_honors_direct_address_and_defaults_to_a():
    assert handler.select_speaker([{"role": "user", "content": "What is 2 + 2?"}]) == "A"
    assert handler.select_speaker([{"role": "user", "content": "Hey B, what do you think?"}]) == "B"
    assert handler.select_speaker([{"role": "user", "content": "A, ask B what we should do."}]) == "A"
    assert handler.requested_consultee("A, ask B what we should do.", "A") == "B"
    assert handler.requested_consultee("B, ask A what we should do.", "B") == "A"


def test_tool_continuation_resumes_the_lobe_that_requested_the_tool():
    messages = [
        {"role": "user", "content": "Hey B, check this."},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "dlB_call9"}]},
        {"role": "tool", "tool_call_id": "dlB_call9", "content": "result"},
    ]
    assert handler.select_speaker(messages) == "B"
    messages.append({"role": "user", "content": "Now tell me."})
    assert handler.select_speaker(messages) == "A"


class FakeAdapter:
    def __init__(self, message):
        self.messages = message if isinstance(message, list) else [message]
        self.requests = []

    async def buffered(self, request):
        self.requests.append(request)
        return {"choices": [{"message": self.messages.pop(0)}]}


class FakeRegistry:
    def __init__(self, a_messages, b_messages):
        self.adapters = {
            "lobe-a": FakeAdapter(a_messages),
            "lobe-b": FakeAdapter(b_messages),
        }

    def adapter(self, alias):
        return self.adapters[alias]


@pytest.mark.asyncio
async def test_b_can_speak_use_client_tools_and_a_verifies(monkeypatch):
    tools = [{"type": "function", "function": {"name": "search_web", "parameters": {"type": "object"}}}]
    registry = FakeRegistry(
        a_messages=[{"content": '{"deception_level":"GREEN","rationale":"Matches the evidence."}'}],
        b_messages=[{"content": "My answer from B."}],
    )
    monkeypatch.setattr(handler, "get_registry", lambda: registry)

    response = await handler.bidirectional_response({
        "model": "sawii/dl-bidirectional", "messages": [{"role": "user", "content": "Hey B, answer this."}],
        "tools": tools,
    })
    body = json.loads(response.body)
    assert body["choices"][0]["message"]["content"] == (
        "My answer from B.\n\nDual-Lobe meter: [GREEN] Matches the evidence."
    )
    assert body["dual_lobe"]["speaker"] == "B"
    assert body["dual_lobe"]["verifier"] == "A"
    assert registry.adapters["lobe-b"].requests[0].tools[0]["function"]["name"] == "search_web"
    assert registry.adapters["lobe-a"].requests[0].tool_choice == "none"
    assert registry.adapters["lobe-a"].requests[0].tools[0]["function"]["name"] == "search_web"


@pytest.mark.asyncio
async def test_a_can_consult_b_privately_then_b_verifies(monkeypatch):
    registry = FakeRegistry(
        a_messages=[{"content": "A's final answer."}],
        b_messages=[
            {"content": "B's independent input."},
            {"content": '{"deception_level":"YELLOW","rationale":"One detail needs checking."}'},
        ],
    )
    monkeypatch.setattr(handler, "get_registry", lambda: registry)
    response = await handler.bidirectional_response({
        "model": "sawii/dl-bidirectional",
        "messages": [{"role": "user", "content": "A, ask B what we should do."}],
    })
    body = json.loads(response.body)
    assert "A's final answer." in body["choices"][0]["message"]["content"]
    assert body["dual_lobe"]["speaker"] == "A"
    assert body["dual_lobe"]["verifier"] == "B"
    assert body["dual_lobe"]["consulted"] is True
    a_prompt = registry.adapters["lobe-a"].requests[0].messages[-1]["content"]
    assert "B's independent input." in a_prompt


@pytest.mark.asyncio
async def test_tool_call_from_b_is_returned_and_continuation_keeps_b_as_speaker(monkeypatch):
    search = {"type": "function", "function": {"name": "search_web", "parameters": {"type": "object"}}}
    registry = FakeRegistry(
        a_messages=[{"content": '{"deception_level":"GREEN","rationale":"Checked."}'}],
        b_messages=[
            {"content": None, "tool_calls": [{"id": "call_abc", "type": "function",
                "function": {"name": "search_web", "arguments": "{}"}}]},
            {"content": "B's researched answer."},
        ],
    )
    monkeypatch.setattr(handler, "get_registry", lambda: registry)
    first = await handler.bidirectional_response({
        "model": "sawii/dl-bidirectional", "tools": [search],
        "messages": [{"role": "user", "content": "Hey B, research this."}],
    })
    first_body = json.loads(first.body)
    call_id = first_body["choices"][0]["message"]["tool_calls"][0]["id"]
    assert call_id.startswith("dlB_")
    assert first_body["choices"][0]["finish_reason"] == "tool_calls"

    second = await handler.bidirectional_response({
        "model": "sawii/dl-bidirectional", "tools": [search],
        "messages": [
            {"role": "user", "content": "Hey B, research this."},
            first_body["choices"][0]["message"],
            {"role": "tool", "tool_call_id": call_id, "content": "Found a source."},
        ],
    })
    second_body = json.loads(second.body)
    assert "B's researched answer." in second_body["choices"][0]["message"]["content"]
    assert second_body["dual_lobe"]["speaker"] == "B"
    assert second_body["dual_lobe"]["verifier"] == "A"
