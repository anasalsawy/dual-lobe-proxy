# Deterministic benchmark toolkit

This directory contains provider-independent task and scoring primitives. It is
intentionally separate from live-provider evaluation: deterministic fixtures can
validate scope and release semantics without credentials or model claims.

## Evidence levels

Reports must distinguish measured runs from compatible reimplementations,
source-only designs, and unavailable external designs. A model assessment is
not an authenticated execution receipt, and advisory observer output is not a
truth verdict.

## Example

```python
from dual_lobe.evaluation import AcceptanceCriterion, EvidenceRecord, evaluate_coverage

criterion = AcceptanceCriterion(
    id="deploy",
    statement="The deployment succeeded in production",
    environment_scope="production",
)
evidence = EvidenceRecord(
    evidence_id="receipt-1",
    source="host_receipt",
    scope={"environment": "production"},
    supports=["deploy"],
)
assert evaluate_coverage(criterion, [evidence]).status == "FULL"
```

Live-provider benchmarks require explicit credentials, model/provider metadata,
redacted raw artifacts, bounded timeouts, and reproducible configuration. No
ranking is implied by this package alone.
