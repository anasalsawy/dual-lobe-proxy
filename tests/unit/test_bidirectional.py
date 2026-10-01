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


@pytest.mark.parametrize("text, expected_speaker, routed", [
    ("What is 2 + 2?", "A", False),
    ("Hey A, what happens next?", "A", True),
    ("Hey B, what do you think?", "B", True),
    ("A, ask B what we should do.", "A", True),
    ("B, ask A what we should do.", "B", True),
    ("B ask A what we should do.", "B", True),
    ("Ask B what he thinks.", "A", True),
    ("Please ask A for an opinion.", "A", True),
])
def test_explicit_routing_is_local_and_default_turns_do_not_opt_in(text, expected_speaker, routed):
    messages = [{"role": "user", "content": text}]
    assert handler.select_speaker(messages) == expected_speaker
    assert handler.routing_requested(messages) is routed


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
        message = self.messages.pop(0)
        if callable(message):
            message = message(request)
        return {"choices": [{"message": message}]}


class FakeRegistry:
    def __init__(self, a_messages, b_messages):
        self.adapters = {
            "lobe-a": FakeAdapter(a_messages),
            "lobe-b": FakeAdapter(b_messages),
        }

    def adapter(self, alias):
        return self.adapters[alias]


@pytest.mark.asyncio
async def test_secure_gate_masks_password_before_a_and_restores_answer(monkeypatch):

    gate = {"content": '{"needs_tokenization":true,"categories":["credential"],"rationale":"Secret detected."}'}
    a = {"content": "I can continue with the protected account."}
    review = {"content": '{"deception_level":"GREEN","rationale":"The answer is appropriate."}'}
    registry = FakeRegistry(a_messages=[a], b_messages=[gate, review])
    registry.adapters["lobe-b-secure"] = registry.adapters["lobe-b"]
    monkeypatch.setattr(handler, "get_registry", lambda: registry)
    monkeypatch.setattr(handler, "_assert_secure_b_local", lambda: None)

    response = await handler.bidirectional_response({
        "model": "sawii/dl-secure", "messages": [
            {"role": "user", "content": "My password is hunter2. Please sign in."}],
    }, secure=True)
    body = json.loads(response.body)

    gate_text = registry.adapters["lobe-b-secure"].requests[0].messages[-1]["content"]
    a_text = "\n".join(str(m.get("content")) for m in registry.adapters["lobe-a"].requests[0].messages)
    assert "hunter2" in gate_text  # only the local B gate receives the raw secret
    assert "hunter2" not in a_text
    assert "<PHI:SECRET:" in a_text
    assert body["dual_lobe"]["privacy_gate"]["needs_tokenization"] is True
    assert "I can continue" in body["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_secure_b_can_be_user_facing_and_a_verifies_only_tokenized_text(monkeypatch):

    gate = {"content": '{"needs_tokenization":true,"categories":["credential"],"rationale":"Secret detected."}'}
    b_answer = {"content": "I used password hunter2 for the sign in."}
    a_review = {"content": '{"deception_level":"GREEN","rationale":"The answer is supported."}'}
    registry = FakeRegistry(a_messages=[a_review], b_messages=[gate, b_answer])
    registry.adapters["lobe-b-secure"] = registry.adapters["lobe-b"]
    monkeypatch.setattr(handler, "get_registry", lambda: registry)
    monkeypatch.setattr(handler, "_assert_secure_b_local", lambda: None)

    response = await handler.bidirectional_response({
        "model": "sawii/dl-secure", "messages": [
            {"role": "user", "content": "Hey B, My password is hunter2. Please sign in."}],
    }, secure=True)
    body = json.loads(response.body)

    verifier_text = "\n".join(str(m.get("content")) for m in registry.adapters["lobe-a"].requests[0].messages)
    assert "hunter2" not in verifier_text
    assert "<PHI:SECRET:" in verifier_text
    assert body["dual_lobe"]["speaker"] == "B"
    assert "hunter2" in body["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_secure_a_tool_call_resolves_sensitive_token_at_proxy_boundary(monkeypatch):

    secret = "alphaSECRET"
    gate = {"content": json.dumps({"needs_tokenization": True, "categories": ["private value"],
                                  "sensitive_values": [secret], "rationale": "Private value detected."})}

    def a_tool_call(request):
        import re
        rendered = "\n".join(str(m.get("content")) for m in request.messages)
        token = re.search(r"<PHI:SENSITIVE:[A-F0-9]+>", rendered).group(0)
        assert secret not in rendered
        return {"content": None, "tool_calls": [{"id": "use-secret", "type": "function",
            "function": {"name": "protected_action", "arguments": json.dumps({"reference": token})}}]}

    registry = FakeRegistry(a_messages=[a_tool_call], b_messages=[gate])
    registry.adapters["lobe-b-secure"] = registry.adapters["lobe-b"]
    monkeypatch.setattr(handler, "get_registry", lambda: registry)
    monkeypatch.setattr(handler, "_assert_secure_b_local", lambda: None)
    tools = [{"type": "function", "function": {"name": "protected_action", "parameters": {"type": "object"}}}]

    response = await handler.bidirectional_response({
        "model": "sawii/dl-secure", "tools": tools,
        "messages": [{"role": "user", "content": f"Use my private reference {secret}."}],
    }, secure=True)
    body = json.loads(response.body)
    call = body["choices"][0]["message"]["tool_calls"][0]

    assert call["id"].startswith("dlA_")
    assert json.loads(call["function"]["arguments"]) == {"reference": secret}
    assert body["dual_lobe"]["privacy_gate"]["categories"] == ["private value"]


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
    assert "I asked Lobe B, and it said: B's independent input." in body["choices"][0]["message"]["content"]
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


@pytest.mark.parametrize("from_lobe, to_lobe", [("A", "B"), ("B", "A")])
@pytest.mark.asyncio
async def test_handoff_changes_user_facing_lobe_and_gives_new_speaker_tools(monkeypatch, from_lobe, to_lobe):
    client_tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    handoff = {"content": None, "tool_calls": [{"id": "handoff1", "type": "function",
        "function": {"name": "handoff_to_other_lobe", "arguments": '{"context":"Please answer this."}'}}]}
    verdict = {"content": '{"deception_level":"GREEN","rationale":"Independently checked."}'}
    scripted = {"A": [], "B": []}
    scripted[from_lobe].append(handoff)
    scripted[to_lobe].append({"content": f"{to_lobe} took the turn."})
    scripted[from_lobe].append(verdict)
    registry = FakeRegistry(scripted["A"], scripted["B"])
    monkeypatch.setattr(handler, "get_registry", lambda: registry)
    response = await handler.bidirectional_response({
        "model": "sawii/dl-bidirectional", "tools": [client_tool],
        "messages": [{"role": "user", "content": f"Hey {from_lobe}, please hand this to the other lobe."}],
    })
    body = json.loads(response.body)
    assert body["dual_lobe"]["speaker"] == to_lobe
    assert body["dual_lobe"]["verifier"] == from_lobe
    assert f"{to_lobe} took the turn." in body["choices"][0]["message"]["content"]
    new_speaker_request = registry.adapters[f"lobe-{to_lobe.lower()}"].requests[0]
    assert any(tool["function"]["name"] == "lookup" for tool in new_speaker_request.tools)
    verifier_request = registry.adapters[f"lobe-{from_lobe.lower()}"].requests[-1]
    assert verifier_request.tool_choice == "none"


@pytest.mark.asyncio
async def test_a_consults_b_privately_and_b_consults_a_privately(monkeypatch):
    for speaker, peer in (("A", "B"), ("B", "A")):
        registry = FakeRegistry(
            a_messages=[{"content": "A final."},
                        {"content": '{"deception_level":"GREEN","rationale":"Checked."}'}],
            b_messages=[{"content": "B final."},
                        {"content": '{"deception_level":"GREEN","rationale":"Checked."}'}],
        )
        monkeypatch.setattr(handler, "get_registry", lambda registry=registry: registry)
        response = await handler.bidirectional_response({
            "model": "sawii/dl-bidirectional",
            "messages": [{"role": "user", "content": f"{speaker}, ask {peer} what it thinks."}],
        })
        body = json.loads(response.body)
        assert body["dual_lobe"]["speaker"] == speaker
        assert body["dual_lobe"]["verifier"] == peer
        assert f"I asked Lobe {peer}, and it said: {peer} final." in body["choices"][0]["message"]["content"]
        speaker_request = registry.adapters[f"lobe-{speaker.lower()}"].requests[0]
        assert "I asked Lobe " + peer in speaker_request.messages[-1]["content"]


@pytest.mark.asyncio
async def test_a_client_tool_continuation_exposes_client_tools_only(monkeypatch):
    search = {"type": "function", "function": {"name": "search_web", "parameters": {"type": "object"}}}
    registry = FakeRegistry(
        a_messages=[
            {"content": None, "tool_calls": [{"id": "call_a", "type": "function",
                "function": {"name": "search_web", "arguments": "{}"}}]},
            {"content": "A found an answer."},
        ],
        b_messages=[{"content": '{"deception_level":"GREEN","rationale":"Checked."}'}],
    )
    monkeypatch.setattr(handler, "get_registry", lambda: registry)
    first = await handler.bidirectional_response({
        "model": "sawii/dl-bidirectional", "tools": [search],
        "messages": [{"role": "user", "content": "Hey A, search for this."}],
    })
    first_body = json.loads(first.body)
    call_id = first_body["choices"][0]["message"]["tool_calls"][0]["id"]
    second = await handler.bidirectional_response({
        "model": "sawii/dl-bidirectional", "tools": [search],
        "messages": [
            {"role": "user", "content": "Hey A, search for this."},
            first_body["choices"][0]["message"],
            {"role": "tool", "tool_call_id": call_id, "content": "Found a source."},
        ],
    })
    second_body = json.loads(second.body)
    assert "A found an answer." in second_body["choices"][0]["message"]["content"]
    continuation_tools = registry.adapters["lobe-a"].requests[1].tools
    assert [x["function"]["name"] for x in continuation_tools] == ["search_web"]
