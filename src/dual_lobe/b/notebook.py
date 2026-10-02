"""Episodic-memory notes: a parallel, additive contract B emits at turn end.

This is deliberately separate from :mod:`dual_lobe.b.prompts` and the meter
contract. B already runs at turn end to verify A; here we ask it a *second*,
independent question: did the user say anything small and personal that no one
followed up on? The answer is a bounded list of verbatim-quoted notes.

Honesty rule: every note must carry the user's own words (``quote``). A note
without a quote is dropped, so B can never manufacture a memory. Storage and
rendering are deterministic — no LLM is involved after B emits the JSON.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from typing_extensions import Annotated

NOTE_KINDS = ("affect", "preference", "callback", "plan", "detail")
MAX_NOTES = 5
MAX_QUOTE = 400
MAX_ANCHOR = 200

_Snippet = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUOTE)]
_Anchor = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_ANCHOR)]


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: str = Field(default="detail")
    anchor: _Anchor = ""
    quote: _Snippet
    feeling: str = Field(default="neutral")
    salience: int = Field(default=1, ge=1, le=3)


class NoteList(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    notes: list[Note] = Field(default_factory=list, max_length=MAX_NOTES)


NOTES_SYSTEM = f"""You are Lobe B, the quiet observer. You do not speak to the user.
While Lobe A worked, you were watching the conversation. Your one job here is to
notice small, *personal* things the user said that nobody followed up on, so they
can be remembered and perhaps raised later.

Return JSON only, exactly:
{{"notes": [{{"kind": "...", "anchor": "...", "quote": "...", "feeling": "...", "salience": 1}}]}}

Rules:
- kind is one of: {", ".join(NOTE_KINDS)}.
- quote MUST be the user's own words, verbatim, copied from the transcript. If you
  cannot quote it, do not include the note.
- anchor is the topic/event the note attaches to (what we were doing), so it can
  be recalled when that topic returns.
- feeling is one of: warm, amused, concerned, proud, tired, neutral.
- salience is 1 (small aside) to 3 (clearly important to the user).
- Only personal, human remarks (a preference, a life detail, a mood, a plan, a
  joke, a callback). Never ordinary task content, never a factual claim to verify.
- Return at most {MAX_NOTES} notes. Return {{"notes": []}} if there are none."""

NOTES_MARKER = "TRANSCRIPT FOR NOTES:"


def build_notes_prompt(*, transcript: str) -> str:
    return NOTES_SYSTEM + "\n\n" + NOTES_MARKER + "\n" + transcript


def _extract_json_object(text: str) -> str | None:
    """Pull the first balanced JSON object out of a model response."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        start = text.find("{", start + 1)
    return None


def parse_notes(content: str, *, known_texts: list[str] | None = None) -> list[dict[str, Any]]:
    """Parse B's notes JSON into clean dicts, dropping anything unquotable.

    ``known_texts`` (the supplied transcript bodies) is used to *ground* each
    quote: a note whose quote cannot be found in the transcript is dropped, so a
    hallucinated memory never reaches storage.
    """
    raw = _extract_json_object(str(content or ""))
    if raw is None:
        return []
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return []
        parsed = NoteList(**payload) if "notes" in payload else NoteList(notes=[])
    except (ValidationError, TypeError, ValueError):
        return []

    haystack = " ".join(known_texts or []).lower()
    haystack = re.sub(r"\s+", " ", haystack)
    clean: list[dict[str, Any]] = []
    for note in parsed.notes:
        quote = note.quote.strip()
        if not quote:
            continue
        if haystack:
            needle = re.sub(r"\s+", " ", quote.lower())
            if needle not in haystack:
                continue
        kind = note.kind if note.kind in NOTE_KINDS else "detail"
        clean.append({
            "kind": kind,
            "anchor": note.anchor.strip(),
            "quote": quote,
            "feeling": note.feeling.strip() or "neutral",
            "salience": int(note.salience),
        })
    return clean
