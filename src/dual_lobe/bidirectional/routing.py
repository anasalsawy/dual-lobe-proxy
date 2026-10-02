"""Lightweight, deterministic opt-in detection for the bidirectional path."""
from __future__ import annotations

import re
from typing import Any

# A vocative address to a lobe. Matches, case-insensitively and anywhere in the
# text:
#   hey/hi/hello/yo [lobe] A|B      ("Hey B", "hello lobe A", "yo B")
#   lobe A|B                        ("lobe A", "Lobe-B", "LobeB")
#   @lobe-a / @lobe_b / @A / @B     ("@A", "@lobe-b")
#   A|B followed by a separator     ("A:", "A,", "A!", "A -", "A —")
#   A|B followed by a request verb  ("B ask", "A please", "B what", ...)
# Leading markdown/quotes are tolerated so "**A:**" and '"B, ..."' are seen.
_LETTER = r"[ab]"
_SEP = r"[,!:]|[-—]"
_VERB = (
    r"(?:ask|please|consult|check|look|help|review|answer|respond|what|why|how|"
    r"tell|explain|handle|can|could|would|will|do)"
)

# Optional wrappers around a token: markdown emphasis, quotes, backticks.
_WRAP = r"[\s*_`\"'>\[\]().]*"

# (a) Vocative/greeting forms — unambiguous, may appear anywhere in the message.
_VOCATIVE = (
    rf"(?:hey|hi|hello|yo)\s+(?:lobe[\s_-]*)?{_LETTER}\b"   # hey [lobe] B
    rf"|lobe[\s_-]*{_LETTER}\b"                              # lobe B / lobe-b / LobeB
    rf"|@{_LETTER}\b"                                        # @a / @b
    rf"|@{_LETTER}{_WRAP}{_SEP}"                             # @a, / @a:
    rf"|{_LETTER}{_WRAP}{_SEP}"                              # A, / B: / A -
)

# (b) Bare "X verb" form ("B ask ...") — an address anywhere, UNLESS it is the
# target of a consultation ("ask B ..."), which names the consultee, not the
# addressee. Also suppress the vocative reading of a letter that directly
# follows a consultation phrase ("check with A, ...").
_LEAD_VERB = rf"{_LETTER}{_WRAP}\s+(?={_VERB}\b)"
_CONSULT_BEFORE = r"(?<!ask\s)(?<!consult\s)(?<!with\s)(?<!view\s)(?<!of\s)"

# Scan for vocatives anywhere; the captured letter is the addressee.
_ADDRESS_RE = re.compile(
    rf"(?<![A-Za-z0-9]){_CONSULT_BEFORE}{_WRAP}({_VOCATIVE})",
    re.IGNORECASE,
)
_VERB_ADDRESS_RE = re.compile(
    rf"(?<![A-Za-z0-9]){_CONSULT_BEFORE}{_WRAP}({_LETTER})(?:{_WRAP})\s+(?={_VERB}\b)",
    re.IGNORECASE,
)

# Pull the single letter out of a matched address fragment.
_LETTER_FROM_MATCH = re.compile(rf"(?i)(?<![A-Za-z0-9]){_LETTER}\b")


def lobe_address(text: str) -> str | None:
    """Return the last explicit lobe address ("A"/"B") in ``text``, if any.

    A vocative address names the lobe the user is talking to: "Hey B", "lobe A",
    "@b", "A:", "B -". When a message re-addresses the other lobe
    ("actually B, ..."), the *last* vocative wins because it is the most recent
    instruction.

    A bare "X ask ..." only counts as an address at the very start of the
    message. This deliberately does NOT treat "ask B" / "consult A" as an
    address to that lobe: those are peer-consultation requests aimed at the
    *speaker*, handled separately by ``requested_consultee``.
    """
    if not text:
        return None
    last: tuple[int, str] | None = None
    for pattern in (_ADDRESS_RE, _VERB_ADDRESS_RE):
        for match in pattern.finditer(text):
            letter = _LETTER_FROM_MATCH.search(match.group(0))
            if letter:
                candidate = (match.start(), letter.group(0).upper())
                if last is None or candidate[0] >= last[0]:
                    last = candidate
    return last[1] if last else None


def unaddressed_consultee(text: str) -> str | None:
    """True when the text only *consults* a lobe ("ask B ...") without addressing.

    Used so a bare "Ask B what he thinks." stays routed to the default speaker A
    while still opting into the bidirectional path.
    """
    if lobe_address(text) is not None:
        return None
    match = re.search(
        rf"(?i)\b(?:ask|consult|check with|get the view of)\s+(?:lobe[\s_-]*)?{_LETTER}\b",
        text,
    )
    if not match:
        return None
    return match.group(0)


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
    if lobe_address(text) is not None:
        return True
    return unaddressed_consultee(text) is not None
