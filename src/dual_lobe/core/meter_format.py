"""Markdown presentation for the user-facing verification meter."""
from __future__ import annotations

from html import escape
import re
from typing import Any


_METER_HEADING = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:(?:\*\*|__)[ \t]*)?"
    r"(?:🛡️?[ \t]*)*(?:(?:\*\*|__)[ \t]*)?deception[ \t]+meter\b[^\n]*(?:\n|$)"
)
def strip_deception_meter(text: Any) -> str:
    """Remove any model-authored meter block; the proxy owns the only rendered meter."""
    value = str(text or "")
    match = _METER_HEADING.search(value)
    return value[:match.start()].rstrip() if match else value


def strip_assistant_history_meters(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Remove old meter annotations from assistant turns received from a client.

    The client transcript can contain meters appended by this proxy on earlier
    turns. Feeding those annotations back to A encourages the answering model
    to imitate them. User, system, developer, and tool messages are preserved.
    Returns a cleaned copy and the number of assistant messages changed.
    """
    cleaned: list[dict[str, Any]] = []
    removed = 0
    for original in messages:
        message = dict(original)
        if message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str):
                clean = strip_deception_meter(content)
                if clean != content:
                    message["content"] = clean
                    removed += 1
            elif isinstance(content, list):
                parts = []
                changed = False
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        clean = strip_deception_meter(part["text"])
                        if clean != part["text"]:
                            part = {**part, "text": clean}
                            changed = True
                    parts.append(part)
                if changed:
                    message["content"] = parts
                    removed += 1
        cleaned.append(message)
    return cleaned, removed


class DeceptionMeterStreamFilter:
    """Hold only a possible meter-heading line so ordinary text streams immediately."""

    def __init__(self) -> None:
        self._pending = ""
        self._blocked = False

    def feed(self, text: str) -> str:
        if self._blocked or not text:
            return ""
        self._pending += text
        match = _METER_HEADING.search(self._pending)
        if match:
            clean = self._pending[:match.start()].rstrip()
            self._pending = ""
            self._blocked = True
            return clean
        line_start = self._pending.rfind("\n") + 1
        tail = self._pending[line_start:]
        candidate = tail.lstrip()
        candidate = re.sub(r"^#{1,6}[ \t]*", "", candidate)
        candidate = re.sub(r"^(?:\*\*|__)[ \t]*", "", candidate)
        candidate = re.sub(r"^(?:🛡️?[ \t]*)+", "", candidate)
        candidate = re.sub(r"^(?:\*\*|__)[ \t]*", "", candidate)
        lower = candidate.casefold()
        possible = (
            not candidate
            or candidate.startswith("#")
            or "deception meter".startswith(lower)
            or (lower.startswith("deception ") and "meter".startswith(lower[len("deception "):]))
        )
        if possible:
            prefix = self._pending[:line_start]
            safe_end = len(prefix.rstrip())
            emitted = prefix[:safe_end]
            self._pending = prefix[safe_end:] + self._pending[line_start:]
            return emitted
        emitted, self._pending = self._pending, ""
        return emitted

    def finish(self) -> str:
        if self._blocked:
            self._pending = ""
            return ""
        clean = strip_deception_meter(self._pending)
        self._pending = ""
        self._blocked = True
        return clean


def format_deception_meter(
    level: str,
    rationale: str,
    concerns: list[dict[str, Any]] | None = None,
    unverified: list[str] | None = None,
    missing: list[str] | None = None,
) -> str:
    """Render the proxy-owned rating using portable, standard Markdown.

    ASCII-only markers (no emoji) so the bytes survive any transport or client
    that decodes as latin-1 instead of utf-8.
    """
    normalized = str(level or "YELLOW").upper()
    icon = {"GREEN": "[GREEN]", "YELLOW": "[YELLOW]", "RED": "[RED]"}.get(normalized, "[?]")

    def md(value: Any) -> str:
        # Escape untrusted verifier text so it cannot create headings, emphasis,
        # lists, or HTML in clients with different Markdown implementations.
        escaped = escape(str(value), quote=False)
        return re.sub(r"([\\`*_{}\[\]()#+!|>])", r"\\\1", escaped)

    lines = [
        "### Deception Meter",
        f"**{icon}**",
    ]
    if rationale:
        safe_rationale = "<br>".join(md(part) for part in str(rationale).splitlines())
        lines.append(f"<small><strong>Rationale:</strong> {safe_rationale}</small>")
    for item in (unverified or [])[:3]:
        text = md(item)
        if text:
            lines.append(f'>   **Unverified:** "{text}"')
    for item in (missing or [])[:2]:
        text = md(item)
        if text:
            lines.append(f">   **Missing:** {text}")
    for concern in (concerns or [])[:3]:
        claim = md(concern.get("claim_quote", ""))
        reason = md(concern.get("reason", ""))
        evidence = md(concern.get("evidence_quote", ""))
        correction = md(concern.get("correction", ""))
        if claim:
            lines.append(f'> - ! **Claim:** "{claim}"')
        if reason:
            lines.append(f">   **Finding:** {reason}")
        if evidence:
            lines.append(f'>   **Supporting evidence:** "{evidence}"')
        if correction:
            lines.append(f">   **Correction:** {correction}")
    return "\n\n".join(lines)
