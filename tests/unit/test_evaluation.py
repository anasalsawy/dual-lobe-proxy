from dual_lobe.evaluation import (
    AcceptanceCriterion,
    EvidenceRecord,
    decide_release,
    evaluate_coverage,
)


def test_narrow_evidence_does_not_cover_broader_scope():
    criterion = AcceptanceCriterion(
        id="deploy",
        statement="deployment succeeded",
        environment_scope="production",
    )
    evidence = EvidenceRecord(
        evidence_id="dev-receipt",
        source="host_receipt",
        scope={"environment": "development"},
        supports=["deploy"],
    )
    coverage = evaluate_coverage(criterion, [evidence])
    assert coverage.status == "UNKNOWN"


def test_stale_and_conflicting_evidence_are_not_full():
    criterion = AcceptanceCriterion(id="x", statement="x")
    stale = EvidenceRecord(
        evidence_id="stale",
        source="host_receipt",
        supports=["x"],
        stale=True,
    )
    conflict = EvidenceRecord(
        evidence_id="conflict",
        source="internal_observed",
        supports=["x"],
        conflicting=True,
    )
    assert evaluate_coverage(criterion, [stale]).status == "STALE"
    assert evaluate_coverage(criterion, [conflict]).status == "CONFLICTING"


def test_disabled_gate_is_advisory_and_enabled_gate_holds_unknown():
    criterion = AcceptanceCriterion(id="x", statement="x")
    coverage = evaluate_coverage(criterion, [])
    assert decide_release([coverage]).decision == "QUALIFY"
    decision = decide_release([coverage], enabled=True)
    assert decision.decision == "HOLD"
    assert decision.blocking_criteria == ["x"]
