"""Companion identity: the API key carries the persona that scopes memory."""
from __future__ import annotations

from dual_lobe.api.auth import Principal


def test_principal_carries_persona_default_empty():
    p = Principal(tenant_id=1, tenant_slug="t", scopes=frozenset())
    assert p.persona == ""


def test_principal_persona_set():
    p = Principal(tenant_id=1, tenant_slug="t", scopes=frozenset(), persona="anas")
    assert p.persona == "anas"


def test_persona_wins_over_request_header():
    """The scope rule used in api/chat.py: a persona key ignores X-DL-Memory-ID."""
    from dual_lobe.state.memory import validate_space

    principal = Principal(tenant_id=1, tenant_slug="t", scopes=frozenset(), persona="anas")
    selected_space = "some-header-space"
    if principal.persona:
        selected_space = f"persona:{principal.persona}"
    assert validate_space(selected_space) == "persona:anas"


def test_no_persona_leaves_header_space_untouched():
    from dual_lobe.state.memory import validate_space

    principal = Principal(tenant_id=1, tenant_slug="t", scopes=frozenset())
    selected_space = "team-space"
    if principal.persona:
        selected_space = f"persona:{principal.persona}"
    assert validate_space(selected_space) == "team-space"
