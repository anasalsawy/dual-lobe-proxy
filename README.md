# Dual-Lobe CrewAI — Gated / Non-Split / Self-Split

This repository is the clean current checkpoint for the three Dual-Lobe variants.

## 1. Gated

`task + memory -> A(full task) -> B(verifier) -> final`

A does the task. B verifies. No parallel channel.

## 2. Non-Split control

`task + memory -> A(full task + optional legacy proxy tools) -> B(verifier) -> final`

This remains the comparison control. A may use:
- `proxy_memory_search`
- `proxy_delegate`
- `proxy_consult`

## 3. Self-Split

There is **no dedicated splitter model anymore**.

```text
task + memory -> A
                  ├─ A works half A ───────────────────────┐
                  └─ split_channel -> B works half B ──────┤  concurrent
                                                          ↓
                                            runtime collects both halves
                                                          ↓
                                   B merge + repair + verify + split grade
                                                          ↓
                                             one canonical final answer
```

A is now defined as a professional splitter-executor. Every turn it first searches for a useful two-way independent decomposition.

If a valid time-saving split exists, A MUST split:
- A defines both independent halves;
- A keeps one half;
- `split_channel` starts B's half asynchronously while A works its own half;
- A and B execute independently at the same time;
- B's worker lane receives the same task/execution tool plane as A's worker lane (the split control tool is A-only to prevent recursive fan-out);
- A returns only its completed half; it does not wait, collect, or merge;
- the runtime waits for both completed halves and sends both to B's finalizer/verifier;
- B preserves useful work from both halves, removes duplication, resolves contradictions, repairs obvious deficiencies, verifies the completed answer, and grades the split;
- B emits one canonical final answer.

If no useful split exists, A stays single-lane.

The objective is **minimum wall-clock completion time without loss of answer quality**, not maximum split frequency.

## Split feedback loop

Every successful Self-Split turn reconverges at B. In one final call B:

1. collects and merges A's half and B's half;
2. repairs obvious deficiencies without inventing unsupported facts;
3. verifies the completed answer;
4. grades A's split decision;
5. emits one canonical final answer.

The split grade includes:
- whether a split was used;
- semantic validity;
- 0–100 split score;
- independence score;
- balance score;
- measured time effect;
- unnecessary split flag;
- missed-valid-split flag;
- whether a single model likely would have been better;
- a short reusable lesson.

The runtime also records actual timing:

- `route_decision_ms`
- `a_half_ms`
- `b_half_ms`
- `parallel_window_ms`
- `overlap_ms`
- `overlap_ratio`
- `balance_ratio`
- `join_wait_ms`
- `b_finalize_verify_ms`
- `parallel_gain_proxy_ms`
- `measured_time_effect`

`parallel_gain_proxy_ms = overlap_ms`.

That metric is deliberately labeled a **proxy**, not a true counterfactual. It measures A/B work that genuinely overlapped. Rigorous speed claims still require matched control runs.


## Loop / canonical-state mode

Self-Split can run as a reconverging loop. Each cycle ends in exactly one canonical state:

```text
canonical state N
   -> A decides split or normal
   -> A-half || B-half
   -> B merge + repair + verify
   -> canonical state N+1
```

The next cycle receives the previous B-finalized answer as `CURRENT CANONICAL STATE`, so A and B do not persist as divergent branches.

CLI:

```bash
dual-lobe --mode split --task "..." --loop-cycles 3 --show-meta
```

This is cycle-level streaming/loop support: intermediate worker halves are internal; the authoritative state is only the B-finalized canonical result of each cycle.

## Experience memory

Split feedback is persisted in the same durable JSONL store under a dedicated `[SPLIT_EXPERIENCE]` marker.

Before future Self-Split decisions, A receives relevant past split lessons separately from ordinary task memory. This creates memory-conditioned adaptation without pretending that the base model was retrained.

Ordinary task memory and split-experience memory remain logically separated.

## Model calls

Typical logical inference counts now are:

- Gated: 2 — A + B verify
- Non-Split: 2, plus any optional delegate/consult calls
- Self-Split NORMAL: 2 — A self-routes/works + B review
- Self-Split after a split: 3 logical executions — A route/half + concurrent B worker half + B merge/repair/verify/finalize

The old dedicated splitter route call has been removed.

## Existing hardening retained

This architecture keeps the previous fixes:

- fail-closed verification parsing
- safe LLM-call handling
- adaptive provider RPM/TPM governor
- provider/model failover and retry/backoff
- Unicode memory retrieval
- thread/process-safe JSONL writes
- run-scoped legacy delegate/consult caps
- balanced JSON extraction
- UTF-8 Windows CLI
- exact memory evidence visible to verification
- generous model budgets

## Install

```bash
pip install -e .
```

## Run

```bash
dual-lobe --mode gated --task "..."
dual-lobe --mode non-split --task "..."
dual-lobe --mode split --task "..." --show-meta
```

The prior dedicated-Splitter implementation remains recoverable from Git history, but it is no longer part of the active runtime.
