# Deterministic benchmark toolkit

The provider-independent primitives live in `src/dual_lobe/evaluation/`.
They validate acceptance scope, evidence provenance, release decisions, and
normalized benchmark records without requiring credentials or a model.

This is **not** yet a complete cross-design runner or a measured ranking. No
claim about model quality, latency, cost, or a top-five design is supported by
these primitives alone.

## Current guarantees

- Criteria and evidence are explicitly scoped; narrow evidence cannot satisfy a broader criterion.
- `host_receipt` is distinguishable from model, client, provider, and internal evidence.
- Stale, conflicting, negative, and unknown evidence are not promoted to `FULL`.
- Disabled release gates remain advisory.
- Benchmark records validate non-negative metrics and aggregate deterministic summaries.

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

## Not yet implemented here

The repository still needs a common adapter protocol, task corpus, raw-event
runner, control route, real-provider execution, uncertainty intervals, and a
measured comparison of twenty or more designs. External designs must be
labelled source-only, unavailable, compatible reimplementation, deterministic,
or real-provider; do not conflate those evidence levels.
