from dual_lobe.b.recipient_router import _router_system_for_mode, explicit_addressee


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
