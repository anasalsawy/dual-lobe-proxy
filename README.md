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
                     ┌── A keeps half A ───────────────┐
task + memory -> A ──┤                                  ├─ A absorbs B half / authors whole answer -> B verify + split grade
                     └── split_channel -> B half B ────┘
```

A is now defined as a professional splitter-executor. Every turn it first searches for a useful two-way independent decomposition.

If a valid time-saving split exists, A MUST split:
- A defines both independent halves;
- A keeps one half;
- `split_channel` launches B's half in a background lane and returns immediately;
- A executes its own half while B executes concurrently;
- B receives the same task/execution tools as A's worker lane (the split_channel control-plane tool is A-only to prevent recursive fan-out);
- after both halves finish, A ALWAYS absorbs B's half and authors the complete final answer;
- "append" means a light A-authored assembly; "integrate" means deeper synthesis. Both still go through A.

If no useful split exists, A stays single-lane.

The objective is **minimum wall-clock completion time without loss of answer quality**, not maximum split frequency.

## Split feedback loop

Every Self-Split turn ends with B doing two separate jobs:

1. verify the final answer;
2. grade A's split decision.

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
- `a_wait_for_b_ms`
- `b_wait_for_a_ms`
- `merge_ms`
- `parallel_gain_proxy_ms`
- `measured_time_effect`

`parallel_gain_proxy_ms = min(A-half time, B-half time) - merge time`.

That metric is deliberately labeled a **proxy**, not a true counterfactual. A positive value means measured parallel work exceeded measured merge overhead.

## Experience memory

Split feedback is persisted in the same durable JSONL store under a dedicated `[SPLIT_EXPERIENCE]` marker.

Before future Self-Split decisions, A receives relevant past split lessons separately from ordinary task memory. This creates memory-conditioned adaptation without pretending that the base model was retrained.

Ordinary task memory and split-experience memory remain logically separated.

## Model calls

Typical logical inference counts now are:

- Gated: 2 — A + B verify
- Non-Split: 2, plus any optional delegate/consult calls
- Self-Split NORMAL: 2 — A self-routes/works + B review
- Self-Split after a split: 4 — A half + concurrent B half + A absorb/merge + B review

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
