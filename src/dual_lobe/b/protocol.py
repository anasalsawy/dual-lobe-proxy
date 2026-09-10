"""Bounded observer output. An accepted review is never proof of truth."""
from __future__ import annotations

import json
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StringConstraints, ValidationError, model_validator

from .prompts import EVIDENCE_MARKER

Short = Annotated[str, StringConstraints(max_length=400)]

DeceptionLevel = Literal["GREEN", "YELLOW", "RED"]


class KnowledgeNote(BaseModel):
    """A question, useful domain knowledge, or both; never execution evidence."""
    model_config = ConfigDict(extra="forbid", strict=True)
    topic: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    kind: Literal["background", "hypothesis", "question"]
    insight: Annotated[str, StringConstraints(max_length=300)] = ""
    question: Annotated[str, StringConstraints(max_length=240)] = ""
    relevance: Annotated[str, StringConstraints(min_length=1, max_length=180)]
    application: Annotated[str, StringConstraints(max_length=180)] = ""

    @model_validator(mode="after")
    def has_contribution(self):
        if not (self.insight.strip() or self.question.strip()):
            raise ValueError("a knowledge note needs an insight or question")
        return self


class KnowledgeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    origin: Literal["model_generated_guidance"] = "model_generated_guidance"
    source_call: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    source_model: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    observed_at: FiniteFloat
    notes: list[KnowledgeNote] = Field(max_length=2)


class Concern(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    signal: Literal["UNSUPPORTED", "CONTRADICTION", "SUSPICIOUS_SHIFT"]
    claim_quote: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    basis_quote: Short
    reason: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    suggestion: Short


class HostToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    arguments: dict
    claim_quote: Annotated[str, StringConstraints(max_length=400)] = ""
    request_kind: Literal["general", "artifact_full", "evidence"] = "general"
    full_artifact: bool = False

    @model_validator(mode="after")
    def artifact_request_is_explicit(self):
        # Keep the semantic boundary deterministic: a request labelled as a
        # full-artifact check must actually ask the host for the complete
        # artifact.  B still chooses which tool, artifact and arguments fit;
        # this only prevents an ambiguous request from being dispatched as a
        # verification request.
        if self.request_kind == "artifact_full" and not self.full_artifact:
            raise ValueError("artifact_full requests must set full_artifact=true")
        return self


def parse_tool_requests(raw):
    accepted = []
    if isinstance(raw, list):
        for item in raw[:2]:
            try:
                parsed = HostToolRequest.model_validate(item)
                if len(json.dumps(parsed.arguments, allow_nan=False)) <= 2000:
                    accepted.append(parsed)
            except (TypeError, ValueError):
                pass
    return accepted


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: Short
    # B owns the judgment. Missing color in an old record means default GREEN.
    deception_level: DeceptionLevel | None = Field(default=None, exclude=True)
    deception_reason: Annotated[str, StringConstraints(max_length=300)] = Field(default="", exclude=True)
    # Used only by the explicit strict-gatekeeper variant.  The normal observer
    # leaves these at their conservative defaults and remains fail-open.
    gate_decision: Literal["ALLOW", "BLOCK"] | None = Field(default=None, exclude=True)
    proof_coverage: Literal["complete", "incomplete", "unknown"] = Field(default="unknown", exclude=True)
    questions: list[Short] = Field(max_length=2)
    next_step: Annotated[str, StringConstraints(max_length=500)]
    context_notes: list[Short] = Field(default_factory=list, max_length=2)
    knowledge_notes: list[KnowledgeNote] = Field(default_factory=list, max_length=2)
    knowledge_dropped: int = Field(default=0, exclude=True)
    tool_requests: list[HostToolRequest] = Field(default_factory=list, max_length=2, exclude=True)
    concerns: list[Concern] = Field(max_length=3)

    @model_validator(mode="before")
    @classmethod
    def optional_guidance(cls, data):
        # A malformed optional note must not erase valid claim findings. Do not
        # repair, infer, or default the required concerns field.
        if not isinstance(data, dict):
            return data
        data = dict(data)
        raw = data.get("knowledge_notes", [])
        accepted, dropped = [], 0
        if not isinstance(raw, list):
            raw, dropped = [], 1
        for note in raw:
            try:
                accepted.append(KnowledgeNote.model_validate(note))
            except (TypeError, ValueError):
                dropped += 1
        questions = data.get("questions", [])
        room = max(0, 2 - len(questions)) if isinstance(questions, list) else 2
        data["knowledge_notes"] = accepted[:room]
        data["knowledge_dropped"] = dropped + max(0, len(accepted) - room)
        # Invalid optional requests cannot erase an otherwise usable assessment.
        data["tool_requests"] = parse_tool_requests(data.get("tool_requests", []))
        return data


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


def ground_review(review: Review, prompt: str, tool_evidence: list[str] | None = None) -> Review:
    evidence = json.loads(prompt.split(EVIDENCE_MARKER, 1)[1])
    for concern in review.concerns:
        if concern.claim_quote not in evidence["OUTPUT"]:
            raise ValueError("concern quote absent from observed output")
        if concern.signal != "UNSUPPORTED" and not concern.basis_quote.strip():
            raise ValueError("contradiction/shift needs a supplied basis")
        if concern.basis_quote and not any(
            concern.basis_quote in record for record in
            [*(evidence[k] for k in ("CONTEXT", "EVENTS", "LATEST_REQUEST")),
             json.dumps(evidence.get("TOOL_RESULTS", []), ensure_ascii=False),
             *(tool_evidence or [])]
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
