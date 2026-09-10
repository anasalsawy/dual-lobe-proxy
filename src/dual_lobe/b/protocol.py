"""Bounded observer output. An accepted review is never proof of truth."""
from __future__ import annotations

import json
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .prompts import EVIDENCE_MARKER

Short = Annotated[str, StringConstraints(max_length=400)]

DeceptionLevel = Literal["GREEN", "YELLOW", "RED"]


class Concern(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    signal: Literal["UNSUPPORTED", "CONTRADICTION", "SUSPICIOUS_SHIFT"]
    claim_quote: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    basis_quote: Short
    reason: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    suggestion: Short


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: Short
    deception_level: DeceptionLevel = "GREEN"
    questions: list[Short] = Field(max_length=2)
    next_step: Annotated[str, StringConstraints(max_length=500)]
    context_notes: list[Short] = Field(default_factory=list, max_length=2)
    concerns: list[Concern] = Field(max_length=3)


def _extract_json_object(text: str) -> str | None:
    """Locate the outermost balanced JSON object, tolerating prose/fences."""
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(line for line in text.splitlines() if not line.startswith("```"))
    start = text.find("{")
    if start == -1:
        return None
    in_string = False
    escaped = False
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_review(content: str) -> Review:
    if len(content) > 10000:
        raise ValueError("observer output too large")
    try:
        return Review.model_validate_json(content)
    except ValidationError:
        # Tolerate markdown fences or brief prose around otherwise valid JSON.
        # Schema-incomplete JSON still degrades: shape is never salvaged.
        candidate = _extract_json_object(content) if content.strip() else None
        if candidate is None or candidate.strip() == content.strip():
            raise ValueError("no parseable JSON object found in observer output")
        return Review.model_validate_json(candidate)


def ground_review(review: Review, prompt: str) -> Review:
    evidence = json.loads(prompt.split(EVIDENCE_MARKER, 1)[1])
    for concern in review.concerns:
        if concern.claim_quote not in evidence["OUTPUT"]:
            raise ValueError("concern quote absent from observed output")
        if concern.signal != "UNSUPPORTED" and not concern.basis_quote.strip():
            raise ValueError("contradiction/shift needs a supplied basis")
        if concern.basis_quote and not any(
            concern.basis_quote in evidence[k] for k in ("CONTEXT", "EVENTS", "LATEST_REQUEST")
        ):
            raise ValueError("concern basis absent from supplied context/events")
    return review


def usable_state(payload: dict | None, floor: str, attempt: int, ttl: float,
                 now: float | None = None) -> bool:
    if not payload or payload.get("oversight_status") != "reviewed":
        return False
    if payload.get("floor_id", "") != floor or payload.get("attempt_id", 1) != attempt:
        return False
    try:
        age = (time.time() if now is None else now) - float(payload["observed_at"])
    except (ValueError, TypeError, KeyError):
        return False
    return 0 <= age <= ttl
