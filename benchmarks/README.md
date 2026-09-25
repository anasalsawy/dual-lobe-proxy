# Controlled 3V benchmark

Compare GATED, NON-SPLIT, and SPLIT on matched tasks with the same models, memory, tools, fixtures, retry policy, and scoring rubric.

Report medians and p90s.

## Route metrics

Report two distinct metrics:

- **semantic_route_accuracy** — valid splitter JSON decisions only. A fallback is not semantic success.
- **effective_route_accuracy** — whether the branch actually executed matches the expected route, including safe fallbacks.

## Memory evidence

B Verifier now receives the exact shared-memory slice A received on the same turn.

## Model-call instrumentation

Use `RunResult.logical_model_calls`. Do not count CrewAI event-bus start/end events as separate inference calls.

Ordinary architecture-level counts:
- GATED = 2
- NON-SPLIT = 2 plus delegate/consult calls actually used
- SPLIT→NORMAL = splitter + NON-SPLIT calls
- actual SPLIT = 5 plus delegate/consult calls actually used

Provider retries/failovers should be reported separately from logical calls.
