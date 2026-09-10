"""Canonical context shared by both lobe peers.

The proxy creates one redacted snapshot for a turn after it has assembled the
messages, tool definitions, artifacts, and tool results that it will expose to
the user-facing peer.  B reviews that same snapshot; it does not receive a
second, smaller view assembled from the original request.  This prevents an
application-only message or tool definition from becoming an accidental A-only
privilege.
"""
from __future__ import annotations

from typing import Any

from ..core.redact import redact_payload
from .artifacts import bounded_artifacts


def make_peer_snapshot(*, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None,
                       artifacts: list[dict[str, Any]] | None,
                       tool_results: list[dict[str, Any]] | None,
                       run_id: str = "", floor_id: str = "", attempt_id: int = 1,
                       memory_space: str | None = None,
                       artifact_budget: int = 12000) -> dict[str, Any]:
    """Build the single data-plane view delivered to A and B.

    The caller has already applied provider-compatible prompt bounds to
    ``messages`` and ``tools``.  We still redact the snapshot before persistence
    so it cannot create a second secret-bearing audit path.
    """
    return redact_payload({
        "run_id": run_id,
        "floor_id": floor_id,
        "attempt_id": attempt_id,
        "memory_space": memory_space,
        "messages": messages,
        "tools": list(tools or []),
        "artifacts": bounded_artifacts(artifacts, artifact_budget),
        "tool_results": list(tool_results or []),
    })


def snapshot_messages(snapshot: Any) -> list[dict[str, Any]]:
    """Return a safe message list from a persisted snapshot."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("messages"), list):
        return []
    return [m for m in snapshot["messages"] if isinstance(m, dict)]


def snapshot_tools(snapshot: Any) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tools"), list):
        return []
    return [t for t in snapshot["tools"] if isinstance(t, dict)]


def snapshot_artifacts(snapshot: Any) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("artifacts"), list):
        return []
    return [a for a in snapshot["artifacts"] if isinstance(a, dict)]


def snapshot_tool_results(snapshot: Any) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tool_results"), list):
        return []
    return [r for r in snapshot["tool_results"] if isinstance(r, dict)]
