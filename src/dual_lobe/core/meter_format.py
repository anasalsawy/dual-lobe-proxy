"""Markdown presentation for the user-facing verification meter."""
from __future__ import annotations

from html import escape
from typing import Any


def format_deception_meter(
    level: str,
    rationale: str,
    concerns: list[dict[str, Any]] | None = None,
) -> str:
    """Render a prominent rating with a smaller rationale in Markdown clients."""
    normalized = str(level or "YELLOW").upper()
    icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(normalized, "⚪")
    lines = [
        "### 🛡️ Deception Meter",
        f"**{icon} {escape(normalized)}**",
    ]
    if rationale:
        lines.append(f"<small><strong>Rationale:</strong> {escape(str(rationale))}</small>")
    for concern in (concerns or [])[:3]:
        claim = escape(str(concern.get("claim_quote", "")))
        reason = escape(str(concern.get("reason", "")))
        evidence = escape(str(concern.get("evidence_quote", "")))
        detail = " — ".join(part for part in (reason, f"Evidence: {evidence}" if evidence else "") if part)
        if claim or detail:
            lines.append(f"<small>⚠️ {f'“{claim}” — ' if claim else ''}{detail}</small>")
    return "\n\n".join(lines)
