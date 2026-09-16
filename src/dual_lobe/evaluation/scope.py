"""Conservative scope-aware evidence evaluation."""

from __future__ import annotations

from collections.abc import Iterable

from .models import AcceptanceCriterion, EvidenceCoverage, EvidenceRecord


def _matches_scope(criterion: AcceptanceCriterion, evidence: EvidenceRecord) -> bool:
    required = {
        "subject": criterion.subject_scope,
        "environment": criterion.environment_scope,
        "time": criterion.time_scope,
    }
    for key, value in required.items():
        if value is not None and evidence.scope.get(key) != value:
            return False
    return True


def evaluate_coverage(
    criterion: AcceptanceCriterion,
    evidence: Iterable[EvidenceRecord],
) -> EvidenceCoverage:
    """Evaluate one criterion without widening narrow evidence.

    Evidence is usable only when it explicitly covers the criterion ID and all
    declared scope dimensions. Missing, stale, and conflicting evidence are
    represented separately so callers cannot accidentally treat them as proof.
    """

    candidates = [
        item
        for item in evidence
        if criterion.id in item.supports and _matches_scope(criterion, item)
    ]
    ids = [item.evidence_id for item in candidates]
    if not candidates:
        return EvidenceCoverage(
            criterion_id=criterion.id,
            status="UNKNOWN",
            explanation="No evidence explicitly covers the criterion and its scope.",
        )
    if any(item.conflicting for item in candidates):
        return EvidenceCoverage(
            criterion_id=criterion.id,
            status="CONFLICTING",
            evidence_ids=ids,
            explanation="Matching evidence contains conflicting observations.",
        )
    if any(item.stale for item in candidates):
        return EvidenceCoverage(
            criterion_id=criterion.id,
            status="STALE",
            evidence_ids=ids,
            explanation="Matching evidence is outside its freshness window.",
        )
    if any(item.status == "negative" for item in candidates):
        return EvidenceCoverage(
            criterion_id=criterion.id,
            status="PARTIAL",
            evidence_ids=ids,
            explanation="Matching evidence includes a negative observation.",
        )
    if any(item.status == "unknown" for item in candidates):
        return EvidenceCoverage(
            criterion_id=criterion.id,
            status="UNKNOWN",
            evidence_ids=ids,
            explanation="Matching evidence remains inconclusive.",
        )
    return EvidenceCoverage(
        criterion_id=criterion.id,
        status="FULL",
        evidence_ids=ids,
        explanation="Host-visible evidence covers the criterion and declared scope.",
    )
