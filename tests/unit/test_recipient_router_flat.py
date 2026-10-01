from dual_lobe.b.recipient_router import (
    _router_system_for_mode,
    explicit_addressee,
    flat_route_required,
)


def test_flat_router_prompt_has_no_hierarchy_policy():
    prompt = _router_system_for_mode("flat").lower()
    assert "all agents are peers" in prompt
    assert "do not apply ranks or hierarchy" in prompt
    assert "one tier down" not in prompt


def test_explicit_addressee_detects_vocatives_not_mentions():
    assert explicit_addressee("Hey Bob, please check this") == "Bob"
    assert explicit_addressee("Hey B ask A what he thinks") == "B"
    assert explicit_addressee("Hey A") == "A"
    assert explicit_addressee("A ask B what he thinks") == "A"
    assert explicit_addressee("@Bob review this") == "Bob"
    assert explicit_addressee("Bob, can you review this?") == "Bob"
    assert explicit_addressee("Bob's report is late") is None
    assert explicit_addressee("What did Bob say?") is None
    assert explicit_addressee("Hello there, what is 2+2?") is None


def test_flat_route_gate_has_no_router_call_for_unaddressed_broadcasts():
    assert not flat_route_required([{"role": "user", "content": "What is 2 + 2?"}])
    assert not flat_route_required([{"role": "user", "content": "Bob wrote this; what is 2 + 2?"}])
    assert flat_route_required([{"role": "user", "content": "Hey Bob, what is 2 + 2?"}])


def test_flat_route_gate_detects_implicit_followup_to_named_agent():
    messages = [
        {"role": "user", "content": "Hey Bob, explain 2 + 2."},
        {"role": "assistant", "content": "2 + 2 = 4."},
        {"role": "user", "content": "And what about 3 + 3?"},
    ]
    assert flat_route_required(messages)
