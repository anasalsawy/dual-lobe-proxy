"""Small, provider-independent models used by acceptance gates and benchmarks."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CoverageStatus = Literal["FULL", "PARTIAL", "UNKNOWN", "STALE", "CONFLICTING"]
ReleaseStatus = Literal["RELEASE", "QUALIFY", "HOLD"]
ReceiptStatus = Literal["requested", "started", "succeeded", "failed", "cancelled"]
EvidenceSource = Literal[
    "model_claim",
    "client_reported",
    "provider_result",
    "host_receipt",
    "internal_observed",
]


class AcceptanceCriterion(BaseModel):
    """A requirement whose scope must be satisfied independently."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    subject_scope: str | None = None
    environment_scope: str | None = None
    time_scope: str | None = None
    required_evidence: list[str] = Field(default_factory=list)


class MaterialClaim(BaseModel):
    """A claim that may require evidence before release."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    action: str | None = None
    result: str | None = None
    subject_scope: str | None = None
    environment_scope: str | None = None
    attempt_scope: str | None = None
    evidence_required: bool = True


class EvidenceCoverage(BaseModel):
    """The scoped evidence supporting one acceptance criterion."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    status: CoverageStatus
    evidence_ids: list[str] = Field(default_factory=list)
    explanation: str


class ReleaseDecision(BaseModel):
    """A conservative release recommendation, not a truth verdict."""

    model_config = ConfigDict(extra="forbid")

    decision: ReleaseStatus
    blocking_criteria: list[str] = Field(default_factory=list)
    user_visible_reason: str


class ExecutionReceipt(BaseModel):
    """Host-observed lifecycle for an action; model claims are not receipts."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    action_id: str
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: ReceiptStatus
    scope: dict[str, str] = Field(default_factory=dict)
    result_digest: str | None = None
    source: Literal["host_receipt"] = "host_receipt"


class EvidenceRecord(BaseModel):
    """Normalized evidence input for deterministic scoring."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    source: EvidenceSource
    scope: dict[str, str] = Field(default_factory=dict)
    status: Literal["positive", "negative", "unknown"] = "positive"
    stale: bool = False
    conflicting: bool = False
    supports: list[str] = Field(default_factory=list)
