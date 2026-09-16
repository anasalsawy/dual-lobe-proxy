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
    return all(value is None or evidence.scope.get(key) == value for key, value in required.items())


def evaluate_coverage(
    criterion: AcceptanceCriterion,
    evidence: Iterable[EvidenceRecord],
) -> EvidenceCoverage:
    """Evaluate one criterion without widening narrow evidence.

    Evidence must explicitly identify the criterion and match every declared
    scope dimension. Negative evidence is never full coverage; conflicting or
    stale matching evidence takes precedence over positive observations.
    """

    candidates = [
        item for item in evidence
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
        status, explanation = "CONFLICTING", "Matching evidence contains conflicting observations."
    elif any(item.stale for item in candidates):
        status, explanation = "STALE", "Matching evidence is outside its freshness window."
    elif any(item.status == "negative" for item in candidates):
        status, explanation = "PARTIAL", "Matching evidence includes a negative observation."
    elif any(item.status == "unknown" for item in candidates):
        status, explanation = "UNKNOWN", "Matching evidence remains inconclusive."
    else:
        status, explanation = "FULL", "Host-visible evidence covers the criterion and declared scope."
    return EvidenceCoverage(
        criterion_id=criterion.id,
        status=status,
        evidence_ids=ids,
        explanation=explanation,
    )
