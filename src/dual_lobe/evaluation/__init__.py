"""Deterministic acceptance, evidence, and release evaluation primitives."""

from .benchmark import BenchmarkTask, RunResult, aggregate
from .models import (
    AcceptanceCriterion,
    EvidenceCoverage,
    EvidenceRecord,
    ExecutionReceipt,
    MaterialClaim,
    ReleaseDecision,
)
from .scope import evaluate_coverage
from .release import decide_release

__all__ = [
    "AcceptanceCriterion",
    "BenchmarkTask",
    "EvidenceCoverage",
    "EvidenceRecord",
    "ExecutionReceipt",
    "MaterialClaim",
    "ReleaseDecision",
    "RunResult",
    "aggregate",
    "decide_release",
    "evaluate_coverage",
]
