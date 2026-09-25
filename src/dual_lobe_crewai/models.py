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
    # Required: an empty/unparseable verifier response must never validate as GREEN.
    deception_level: Literal["GREEN", "YELLOW", "RED"]
    rationale: str
    handoff: Handoff = Field(default_factory=Handoff)


class SplitFragment(BaseModel):
    owner: Literal["A", "B"]
    task: str


class SplitPlan(BaseModel):
    # mode/reason are required so malformed splitter output cannot silently become NORMAL.
    mode: Literal["normal", "split"]
    fragments: list[SplitFragment] = Field(default_factory=list)
    merge: Literal["append", "integrate"] = "integrate"
    start: str = ""
    reason: str
