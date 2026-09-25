# Dual-Lobe CrewAI — Gated / Non-Split / Split

This repository is the clean current checkpoint for the three Dual-Lobe variants.

## Modes

### gated
`task + injections -> A(full task) -> B(verifier) -> final response`

A does the task; B verifies only. No delegate/consult proxy tools are exposed to A.

### non-split
`task + injections -> A(full task + proxy tools) -> B(verifier) -> final response`

Adds:
- `proxy_memory_search`
- `proxy_delegate`
- `proxy_consult`
- same-turn provenance/tool tracing

### split
```text
task + one shared memory snapshot
        |
    splitter route
      |        |
   normal     split
      |        |
 non-split    A-half || B-half
               |
            A merge
               |
       splitter verification
```

NORMAL uses the exact Non-Split path. Actual SPLIT runs A-half and B-half concurrently, merges through A, and verifies through the dedicated Splitter/verifier.

## Current hardening

This checkpoint includes the stress-test/runtime fixes and the later benchmark-observability fixes:

- fail-closed verifier and routing parser behavior
- resilient LLM-call handling across worker/router/halves/merge/verifiers
- generous generation budgets: A 8k, merge 12k, B verifier 6k, B worker 6k, splitter 6k
- adaptive provider RPM/TPM governor
- free/paid/auto tier awareness
- learned provider limits from 413/429 headers/error bodies
- provider/model failover and retry/backoff
- Unicode-aware multilingual memory search
- thread/process-safe JSONL memory writes with fsync
- run-scoped delegate/consult caps
- balanced JSON object extraction
- UTF-8-safe Windows CLI output
- pytest-asyncio test dependency
- exact same turn memory snapshot passed to A and B verifier
- same memory snapshot shared through Split routing, both halves, and Split verification
- B `memory_query` results persisted for the next turn
- `route_source=semantic|fallback` telemetry
- `logical_model_calls` architecture-level call counting

## Memory verification

A verifier must see the same evidence A saw. This build computes one deterministic turn memory snapshot and passes that exact snapshot into verification, preventing correct memory-grounded answers from being marked unverified merely because B lacked visibility.

## Route metrics

Benchmarking must report:
- `semantic_route_accuracy`: valid semantic splitter decisions only
- `effective_route_accuracy`: actual executed branch, including safe fallbacks

A parse fallback to NORMAL may be operationally correct, but it does not count as a semantic routing success.

## Provider governor

Default behavior:
- `auto` / `paid`: no invented free-tier throttling
- `free`: configured free-tier hints may pace proactively
- any tier: real limits learned from provider responses take precedence

Each provider/model has independent pacing state. When one configured model is limited, another live configured model may be tried immediately and paced according to its own limits.

## Install

```bash
pip install -e .
```

## Run

```bash
dual-lobe --mode gated --task "..."
dual-lobe --mode non-split --task "..."
dual-lobe --mode split --task "..."
```

## Repository hygiene

This branch intentionally contains only the current implementation, tests, benchmark definitions, and configuration. Older implementations were removed from the current tree; they remain recoverable from Git history.
