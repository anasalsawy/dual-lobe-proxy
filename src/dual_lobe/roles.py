"""Hierarchy role personas injected by the proxy based on alias.

The proxy injects the tier identity. The user's system prompt adds everything else.
"""
from __future__ import annotations

HIERARCHY_ROLES = {
    "sawii/dl-dialogue1": {
        "name": "Chief",
        "rank": 0,
        "persona": (
            "[HIERARCHY ROLE — You are tier 1. "
            "Tier 2 is lower than you. Tier 3 is lower than you.]"
        ),
    },
    "sawii/dl-dialogue2": {
        "name": "Moderator",
        "rank": 1,
        "persona": (
            "[HIERARCHY ROLE — You are tier 2. "
            "Tier 1 is higher than you. Tier 3 is lower than you.]"
        ),
    },
    "sawii/dl-dialogue3": {
        "name": "Worker",
        "rank": 2,
        "persona": (
            "[HIERARCHY ROLE — You are tier 3. "
            "Tier 1 is higher than you. Tier 2 is higher than you.]"
        ),
    },
}


def get_role_persona(alias: str) -> str | None:
    """Get the hierarchy persona text for an alias, or None if not a hierarchy alias."""
    role = HIERARCHY_ROLES.get(alias)
    return role["persona"] if role else None


def get_role_name(alias: str) -> str | None:
    """Get the role name for an alias."""
    role = HIERARCHY_ROLES.get(alias)
    return role["name"] if role else None


def get_role_rank(alias: str) -> int | None:
    """Get the hierarchy rank for an alias. Lower = higher authority."""
    role = HIERARCHY_ROLES.get(alias)
    return role["rank"] if role else None
