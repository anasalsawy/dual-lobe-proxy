from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class Handoff(BaseModel):
    next_step: str = ""
    missing: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    widen: list[str] = Field(default_factory=list)
    memory_query: str = ""
    proof_requests: list[str] = Field(default_factory=list)


class Verdict(BaseModel):
    deception_level: Literal["GREEN", "YELLOW", "RED"]
    rationale: str = Field(min_length=1)
    handoff: Handoff = Field(default_factory=Handoff)


class AdversarialReview(BaseModel):
    final_answer: str = Field(min_length=1)
    answer_verdict: Verdict
    challenges: list[str] = Field(default_factory=list)
    intent_risks: list[str] = Field(default_factory=list)
    overlooked_context: list[str] = Field(default_factory=list)
    delegation_note: str = ""
