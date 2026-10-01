"""Version-controlled tool boundary for the privacy-preserving engine.

The boundary is unconditional: A has no environment tools. B owns all tools,
including browsers, screens, databases, and connected runtimes. Caller-supplied
labels and environment allowlists cannot grant A access.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

def tool_name(tool: dict[str, Any]) -> str:
    return str((tool.get("function") or {}).get("name") or "").strip()


def tools_for_a(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """A cannot inspect or operate on the external environment."""
    return []


def tools_for_b(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """B owns all client tools, regardless of caller-supplied scope labels."""
    return [deepcopy(tool) for tool in (tools or [])
            if isinstance(tool, dict) and tool.get("function")]
