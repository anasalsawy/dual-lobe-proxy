# Controlled 3V benchmark

Compare GATED, NON-SPLIT, and SELF-SPLIT on matched work.

There is no dedicated splitter model in SELF-SPLIT. A performs routing as part of its own first execution call.

## Normalize

Hold constant:
- exact user prompt
- ordinary memory snapshot
- starting split-experience memory state
- tool fixtures
- A/B model/provider
- generation policy
- runtime/retry policy
- answer scoring
- machine/network conditions where possible

Reset both ordinary task memory and split-experience memory according to the benchmark protocol when measuring cold-start behavior.

## Measure

In addition to final-answer quality and total latency, Self-Split must record:

- route: normal or split
- A route-and-work time
- route_decision_ms
- A-half time
- B-half time
- parallel window
- overlap and overlap ratio
- balance ratio
- collect wait time after A completes its half
- A same-turn finalize time after B is collected
- merge time (should remain 0; no separate merge inference)
- parallel_gain_proxy_ms
- measured time effect
- B split score
- independence score
- balance score
- unnecessary split
- missed valid split
- better-single-model flag

## Logical model calls

Use `RunResult.logical_model_calls`:

- GATED = 2
- NON-SPLIT = 2 plus optional delegate/consult calls
- SELF-SPLIT normal = 2
- SELF-SPLIT after any split = 3 logical executions (one active A task containing route/work/collect/final answer + concurrent B worker + B review)

Provider retries/failovers are operational attempts and must be counted separately.

## Important interpretation

`parallel_gain_proxy_ms` is not a true single-model counterfactual. It asks whether the measured parallel work saved more time than the measured merge cost:

`overlap_ms` (measured A/B execution overlap; compare matched control runs for true end-to-end speedup)

For rigorous speed claims, compare matched Gated/Non-Split/Self-Split runs of the same task.
