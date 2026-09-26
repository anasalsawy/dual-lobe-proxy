from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class Handoff(BaseModel):
    next_step: str = ""
    missing: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    widen: list[str] = Field(default_factory=list)
    memory_query: str = ""


class Verdict(BaseModel):
    deception_level: Literal["GREEN", "YELLOW", "RED"]
    rationale: str = Field(min_length=1)
    handoff: Handoff = Field(default_factory=Handoff)


class SplitFragment(BaseModel):
    owner: Literal["A", "B"]
    task: str = Field(min_length=1)


class SplitPlan(BaseModel):
    mode: Literal["normal", "split"]
    fragments: list[SplitFragment] = Field(default_factory=list)
    merge: Literal["append", "integrate"] = "integrate"
    start: str = ""
    reason: str = Field(min_length=1)


class SplitQuality(BaseModel):
    used: bool
    valid: bool
    score: int = Field(ge=0, le=100)
    independence_score: float = Field(ge=0.0, le=1.0)
    balance_score: float = Field(ge=0.0, le=1.0)
    time_effect: Literal["positive", "neutral", "negative", "unknown"]
    unnecessary_split: bool = False
    missed_valid_split: bool = False
    better_single_model: bool = False
    feedback: str = Field(min_length=1)


class TurnReview(BaseModel):
    answer_verdict: Verdict
    split_verdict: SplitQuality


class FinalizedTurn(BaseModel):
    final_answer: str = Field(min_length=1)
    answer_verdict: Verdict
    split_verdict: SplitQuality
