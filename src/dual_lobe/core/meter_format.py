"""Markdown presentation for the user-facing verification meter."""
from __future__ import annotations

from html import escape
import re
from typing import Any


_METER_HEADING = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:(?:\*\*|__)[ \t]*)?"
    r"(?:🛡️?[ \t]*)*(?:(?:\*\*|__)[ \t]*)?deception[ \t]+meter\b[^\n]*(?:\n|$)"
)
_GREETING_ONLY = re.compile(
    r"^(?:hi|hey|hello|greetings|good morning|good afternoon|good evening)(?: there)?"
    r"(?: how can i (?:help|assist)(?: you)?(?: today)?| what can i help you with)?$"
)


def strip_deception_meter(text: Any) -> str:
    """Remove any model-authored meter block; the proxy owns the only rendered meter."""
    value = str(text or "")
    match = _METER_HEADING.search(value)
    return value[:match.start()].rstrip() if match else value


def is_claim_free_greeting(user_text: Any, answer: Any) -> bool:
    """Identify only a plain greeting exchange with no factual content to verify."""
    def normalized(value: Any) -> str:
        value = re.sub(r"[^a-z0-9' ]", " ", str(value or "").casefold())
        return " ".join(value.split())

    return bool(_GREETING_ONLY.fullmatch(normalized(user_text))
                and _GREETING_ONLY.fullmatch(normalized(answer)))


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
