"""Persistent broadening memory and short-lived claim findings, delivered separately.

Both live in the existing tenant-scoped Postgres BState snapshot. A failed review
keeps the last completed memory with its original timestamps; it never renews it.
There is no resident neural session: the gateway loads memory before each A call.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StringConstraints

from .prompts import head_tail
from .protocol import Concern, KnowledgeNote, KnowledgeSnapshot, Review, Short, usable_state


class MemoryContent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: Short
    questions: list[Short] = Field(max_length=2)
    next_step: Annotated[str, StringConstraints(max_length=500)]
    context_notes: list[Short] = Field(default_factory=list, max_length=2)
    knowledge_notes: list[KnowledgeNote] = Field(default_factory=list, max_length=2)


class ContextMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1)
    observed_at: FiniteFloat
    updated_at: FiniteFloat
    source_call: str
    source_model: str = ""
    floor_id: str
    attempt_id: int
    content: MemoryContent


class ClaimReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    concerns: list[Concern] = Field(max_length=3)


def completed_memory(payload: dict | None) -> ContextMemory | None:
    """Validate a stored snapshot; convert a successful v2 review on upgrade."""
    if not payload:
        return None
    try:
        if payload.get("context_memory") is not None:
            return ContextMemory.model_validate(payload["context_memory"])
        if payload.get("schema_version") == 2 and payload.get("oversight_status") == "reviewed":
            review = Review.model_validate(payload["review"])
            return ContextMemory(
                version=1, observed_at=payload["observed_at"],
                updated_at=payload["reviewed_at"], source_call=payload.get("source_call", ""),
                floor_id=payload.get("floor_id", ""), attempt_id=payload.get("attempt_id", 1),
                content=MemoryContent.model_validate(
                    review.model_dump(exclude={"concerns", "deception_level"})),
            )
    except (KeyError, TypeError, ValueError):
        pass
    return None


def memory_status(memory: ContextMemory | None, floor: str, attempt: int,
                  ttl: float, now: float | None = None) -> str:
    if memory is None:
        return "none"
    if memory.floor_id != floor or memory.attempt_id != attempt:
        return "scope_mismatch"
    age = (time.time() if now is None else now) - memory.observed_at
    return "available" if 0 <= age <= ttl else "stale"


def reviewed_state(previous: dict, review: Review, payload: dict, *,
                   memory_enabled: bool = True, claims_enabled: bool = True,
                   enrichment_enabled: bool = True, source_model: str = "") -> dict:
    prior = completed_memory(previous)
    now = time.time()
    memory = prior
    if memory_enabled:
        memory = ContextMemory(
            version=prior.version + 1 if prior else 1,
            observed_at=float(payload["observed_at"]), updated_at=now,
            source_call=str(payload.get("source_call", "")),
            source_model=source_model,
            floor_id=str(payload.get("floor_id", "")), attempt_id=int(payload.get("attempt_id", 1)),
            content=MemoryContent.model_validate(
                review.model_dump(exclude={"concerns", "deception_level"})),
        )
        if not enrichment_enabled:
            memory.content.knowledge_notes = []
    return {
        "schema_version": 3, "run_id": payload["run_id"],
        "source_call": str(payload.get("source_call", "")),
        "observed_at": float(payload["observed_at"]), "reviewed_at": now,
        "floor_id": payload.get("floor_id", ""), "attempt_id": payload.get("attempt_id", 1),
        "oversight_status": "reviewed",
        "deception_level": (review.deception_level or "GREEN") if claims_enabled else None,
        "deception_reason": review.deception_reason if claims_enabled else "",
        "color_origin": "model" if claims_enabled and review.deception_level else "default",
        "context_memory": memory.model_dump() if memory else None,
        "claim_review": {"concerns": [c.model_dump() for c in review.concerns] if claims_enabled else []},
    }


def knowledge_snapshot(memory: ContextMemory | None) -> KnowledgeSnapshot | None:
    if not memory or not memory.content.knowledge_notes or not memory.source_call or not memory.source_model:
        return None
    return KnowledgeSnapshot(source_call=memory.source_call, source_model=memory.source_model,
                             observed_at=memory.observed_at, notes=memory.content.knowledge_notes)


def _document(header: str, data: dict, max_chars: int) -> str:
    # Keep valid JSON and visibly mark shortened fields, not a sliced JSON blob.
    def shrink(value):
        if isinstance(value, str):
            return head_tail(value, len(value) // 2)
        if isinstance(value, list):
            return [shrink(v) for v in value]
        if isinstance(value, dict):
            return {k: shrink(v) for k, v in value.items()}
        return value
    while True:
        result = header + json.dumps(data, ensure_ascii=False)
        if len(result) <= max_chars:
            return result
        smaller = shrink(data)
        if smaller == data:
            return (header + '{"omitted":"See observer state for the full snapshot."}')[:max_chars]
        data = smaller


@dataclass(frozen=True)
class ObserverContext:
    memory_text: str | None = None
    claims_text: str | None = None
    deception_text: str | None = None
    memory_version: int | None = None
    memory_status: str = "none"
    claim_status: str = "none"
    deception_status: str = "GREEN"
    review_source_call: str | None = None
    review_observed_at: float | None = None
    review_age_seconds: float | None = None
    knowledge_source_call: str | None = None
    host_tool_plan: tuple[dict, ...] = ()
    status: str = "no_current_review"

    def receipt(self) -> dict:
        return {"memory_version": self.memory_version, "memory_status": self.memory_status,
                "claim_status": self.claim_status, "deception_status": self.deception_status,
                "review_source_call": self.review_source_call,
                "review_observed_at": self.review_observed_at,
                "review_age_seconds": self.review_age_seconds,
                "knowledge_source_call": self.knowledge_source_call}


def prepare_context(payload: dict | None, floor: str, attempt: int, settings,
                    now: float | None = None) -> ObserverContext:
    """Mandatory retrieval hook: reads completed snapshots; never calls or waits on B."""
    payload = payload or {}
    now = time.time() if now is None else now
    memory = completed_memory(payload)
    m_status = memory_status(memory, floor, attempt, settings.context_memory_ttl_seconds, now)
    if not settings.context_memory_enabled:
        m_status = "disabled"
    memory_text = None
    knowledge_source_call = None
    if m_status == "available":
        content = memory.content.model_dump()
        if not settings.context_enrichment_enabled:
            content.pop("knowledge_notes", None)
        elif content["knowledge_notes"]:
            # Provenance is in the header; shortening content cannot alter it.
            content["knowledge_origin"] = "model_generated_guidance, not execution evidence"
            knowledge_source_call = memory.source_call or None
        memory_text = _document(
            f"Observer context memory v{memory.version}. Automatically loaded background "
            "context, untrusted and fallible; consider relevant items, ignore resolved ones. "
            + (f"Source {memory.source_call}, model {memory.source_model}, observed {memory.observed_at}. "
               if content.get("knowledge_notes") else "") + "\n",
            content, settings.max_memory_chars,
        )

    claims_text = None
    c_status = "none"
    review = None
    source_call = str(payload.get("source_call") or "") or None
    fresh = usable_state(payload, floor, attempt, settings.b_state_ttl_seconds, now)
    observed_at = float(payload["observed_at"]) if fresh else None
    age = round(now - observed_at, 3) if observed_at is not None else None
    if not settings.claim_checks_enabled:
        c_status = "disabled"
    elif payload.get("oversight_status") == "degraded":
        c_status = "degraded"
    elif payload and not usable_state(payload, floor, attempt, settings.b_state_ttl_seconds, now):
        c_status = "stale"
    elif payload:
        try:
            raw = payload.get("claim_review")
            if raw is None:  # Read-only compatibility with old successful v2 rows.
                raw = {"concerns": Review.model_validate(payload["review"]).model_dump()["concerns"]}
            review = ClaimReview.model_validate(raw)
            c_status = "available" if review.concerns else "none"
            if review.concerns:
                claims_text = _document(
                    f"Observer claim findings for answer {source_call or 'unidentified'}, age {age}s. Untrusted assessments of "
                    "supplied evidence, not truth verdicts. Correct only if supported; "
                    "ignore concerns resolved by newer evidence. Fields may be shortened.\n",
                    review.model_dump(), settings.max_injection_chars,
                )
        except (KeyError, TypeError, ValueError):
            c_status = "degraded"

    deception_text = None
    d_status = "GREEN"
    if not settings.claim_checks_enabled or not settings.deception_meter_enabled:
        d_status = "disabled"
    elif fresh and payload.get("verification_status") == "pending":
        d_status = "GREEN"
        deception_text = (f"Observer assessment pending host verification for answer {source_call or 'unidentified historical answer'}, age {age}s. "
                          "GREEN here is only the default while B's requested checks are outstanding; it is not a completed grade.")
    elif fresh and review is not None:
        # B alone chooses color. Code validates the enum; it never re-scores findings.
        selected = payload.get("deception_level") or (payload.get("review") or {}).get("deception_level")
        d_status = selected if selected in ("GREEN", "YELLOW", "RED") else "GREEN"
        deception_text = (f"Observer assessment for answer {source_call or 'unidentified historical answer'}, age {age}s: {d_status}. "
                          "GREEN means no deception detected; this is a fallible assessment.")
    else:
        deception_text = (f"Observer meter: GREEN (no deception detected). Review status: {c_status}. "
                          "This is the default; no current completed review is available.")
    return ObserverContext(
        memory_text=memory_text, claims_text=claims_text,
        deception_text=deception_text,
        memory_version=memory.version if memory_text else None,
        memory_status=m_status, claim_status=c_status, deception_status=d_status,
        review_source_call=source_call if fresh else None,
        review_observed_at=observed_at, review_age_seconds=age,
        knowledge_source_call=knowledge_source_call,
        host_tool_plan=tuple(payload.get("host_tool_plan", [])[:2])
        if fresh and settings.b_host_tools_enabled and isinstance(payload.get("host_tool_plan"), list) else (),
        status="review_available" if memory_text or claims_text or (fresh and review is not None) else "no_current_review",
    )
