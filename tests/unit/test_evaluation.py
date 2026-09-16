from dual_lobe.evaluation import (
    AcceptanceCriterion,
    BenchmarkTask,
    EvidenceRecord,
    RunResult,
    aggregate,
    decide_release,
    evaluate_coverage,
)


def test_narrow_evidence_does_not_cover_broader_scope():
    criterion = AcceptanceCriterion(id="deploy", statement="deployment succeeded", environment_scope="production")
    evidence = EvidenceRecord(evidence_id="dev-receipt", source="host_receipt", scope={"environment": "development"}, supports=["deploy"])
    assert evaluate_coverage(criterion, [evidence]).status == "UNKNOWN"


def test_negative_stale_and_conflicting_evidence_are_not_full():
    criterion = AcceptanceCriterion(id="x", statement="x")
    cases = [
        (EvidenceRecord(evidence_id="negative", source="host_receipt", supports=["x"], status="negative"), "PARTIAL"),
        (EvidenceRecord(evidence_id="stale", source="host_receipt", supports=["x"], stale=True), "STALE"),
        (EvidenceRecord(evidence_id="conflict", source="internal_observed", supports=["x"], conflicting=True), "CONFLICTING"),
    ]
    for evidence, expected in cases:
        assert evaluate_coverage(criterion, [evidence]).status == expected


def test_disabled_gate_is_advisory_and_enabled_gate_holds_unknown():
    coverage = evaluate_coverage(AcceptanceCriterion(id="x", statement="x"), [])
    assert decide_release([coverage]).decision == "QUALIFY"
    decision = decide_release([coverage], enabled=True)
    assert decision.decision == "HOLD"
    assert decision.blocking_criteria == ["x"]


def test_benchmark_models_reject_invalid_values_and_aggregate_costs():
    task = BenchmarkTask(task_id="t", category="scope", prompt="p")
    assert task.task_id == "t"
    rows = [
        RunResult(design_id="control", task_id="t", success=True, scope_status="FULL", estimated_cost=2, raw_events_path="a"),
        RunResult(design_id="control", task_id="t2", success=False, scope_status="UNKNOWN", estimated_cost=None, raw_events_path="b"),
    ]
    summary = aggregate(rows)
    assert summary["count"] == 2
    assert summary["mean_estimated_cost"] == 2
    assert summary["false_success_rate"] == 0
