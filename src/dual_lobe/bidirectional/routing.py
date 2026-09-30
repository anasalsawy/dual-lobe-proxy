"""Lightweight, deterministic opt-in detection for the bidirectional path."""
from __future__ import annotations

import re
from typing import Any


def routing_requested(messages: list[dict[str, Any]]) -> bool:
    """Return whether a turn explicitly requests a lobe route.

    This module has no provider, database, or model dependencies so API startup
    loads the check before serving requests. Unaddressed turns perform only
    these local string/regex operations.
    """
    for message in reversed(messages):
        if message.get("role") == "tool":
            return str(message.get("tool_call_id") or "").startswith(("dlA_", "dlB_"))
        if message.get("role") not in {"assistant", "tool"}:
            break

    text = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                text = "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict)).strip()
            break
    if not text:
        return False
    addressed = re.match(
        r"(?i)^(?:(?:hey|hi|hello|yo)\s+(?:lobe\s+)?[ab]\b|"
        r"(?:lobe\s+)?[ab]\s*(?:[,!:]|\b(?:ask|please|what|help|answer|respond)\b))",
        text,
    )
    if addressed:
        return True
    return bool(re.search(
        r"(?i)\b(?:ask|consult|check with|get the view of)\s+(?:lobe\s+)?[ab]\b", text
    ))
