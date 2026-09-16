# Final Verification Report: Dual-Lobe Design Investigation and Implementation Readiness

**Target repository:** `anasalsawy/dual-lobe-proxy`  
**Inspected branch:** `main`  
**Inspection date:** 2026-09-16  
**Scope:** repository verification, related `dl*` designs, architecture review, benchmark readiness, implementation requirements, and correction of unsupported performance claims.

---

## 1. Executive conclusion

The repository is a substantial, implemented development proxy rather than a blank prototype. It contains:

- An OpenAI-compatible Chat Completions gateway.
- Primary Lobe A routing.
- Asynchronous Lobe B observation.
- Optional serial director mode.
- Postgres-backed state and memory.
- Evidence-linked advisory findings.
- Tool-call preservation and result matching.
- CI with a real Postgres service.
- Extensive deterministic protocol tests.

However, the requested research outcome—**testing at least 20 designs against a single-lobe control and ranking the top five with measured performance numbers**—has **not yet been completed**.

The repository contains a documented inventory of 24 relevant designs and proposed comparison criteria, but that inventory is primarily a **source/repository comparison**, not a controlled empirical benchmark. Most related repositories have no measured results, no test suite, or only a README/flow implementation. Consequently:

- A factual top-five ranking by measured quality/performance cannot yet be claimed.
- Existing numbers such as “131 tests passed” are software-validation results, not model-quality or design-performance results.
- The `compuse` timing result is deterministic plumbing evidence, not a fair comparison of real model performance.
- Claims such as “zero added A latency” or “performance win” must remain architectural hypotheses until measured under a common harness.

The recommended implementation is to add a **benchmark/evaluation subsystem** to the target repository, not to replace the existing proxy architecture.

---

# 2. Task interpretation and exact requirements

## 2.1 Required deliverables

The task requires:

1. Inspect `anasalsawy/dual-lobe-proxy`.
2. Locate and inspect repositories beginning with `dl`.
3. Study at least 20 dual-lobe designs.
4. Compare them with a normal single-lobe control.
5. Test the designs under equivalent conditions.
6. Create new hybrid designs.
7. Produce numerical scores and a top-five ranking.
8. Ensure claims are backed by reproducible tests.

## 2.2 Definition of “dual-lobe”

“Dual-lobe” is not a standard industry category. For this investigation, relevant designs are systems with two materially distinct reasoning/control paths, such as:

- Primary executor plus observer.
- Answerer plus verifier.
- Planner/strategist plus executor.
- Worker plus reviewer.
- Runtime supervisor plus acting agent.
- Primary model plus independent evidence checker.
- Predictive next-step model plus active executor.

Repositories that merely contain multiple agents without role separation, independent review, evidence handling, or control-flow distinction should not be counted as equivalent designs.

## 2.3 Control condition

The control must be a normal single-lobe system using the same:

- Model.
- Provider.
- Prompt/task corpus.
- Tool surface.
- Context window.
- Temperature and sampling settings.
- Timeout.
- Token budget.
- Hardware/network environment.

The control should not receive observer memory, review findings, monitoring reminders, or second-model context unless the experiment specifically measures those components independently.

---

# 3. Verified repository state

## 3.1 Top-level structure

The repository contains the following relevant areas:

```text
.env.example
.github/workflows/ci.yml
README.md
alembic.ini
alembic/
docker-compose.yml
docker/
docs/
plan.md
pyproject.toml
requirements.lock
src/
tests/
tools/
uv.lock
```

## 3.2 Source layout

The primary implementation is organized under:

```text
src/dual_lobe/
├── api/
├── b/
├── client.py
├── core/
├── demo.py
├── director/
├── evidence/
├── obs/
├── provider/
└── state/
```

This separation is architecturally sound:

- `api/`: HTTP and request handling.
- `b/`: observer protocol, prompts, worker, context shadowing.
- `director/`: serial A/B mode.
- `evidence/`: bounded evidence sensing.
- `provider/`: upstream model adapters.
- `state/`: persistence and repository access.
- `core/`: configuration and shared controls.

## 3.3 Test layout

The repository includes:

```text
tests/
├── conftest.py
├── helpers.py
├── test_api.py
├── test_director_memory.py
├── test_schema_rls.py
├── test_worker.py
└── unit/
    ├── test_channels.py
    ├── test_client.py
    ├── test_director.py
    ├── test_director_api.py
    ├── test_evidence.py
    ├── test_memory.py
    ├── test_observer.py
    └── test_request_path.py
```

This is stronger than the related repositories inspected. It tests transport and protocol correctness, but does not yet constitute a semantic benchmark of model quality.

## 3.4 Documentation relevant to this task

The repository already contains:

- `docs/DUAL_LOBE_DESIGN_COMPARISON.md`
- `docs/DUAL_LOBE_FUNCTIONS_AND_CRITERIA.md`
- `docs/FUNCTION_ASSESSMENT.md`
- `docs/RESEARCH_AND_DESIGN.md`
- `docs/VALIDATION.md`
- `docs/DIRECTOR_AND_MEMORY.md`
- `docs/THREE_PATH_SETUP.md`
- `docs/UX_ASSESSMENT_WITH_VS_WITHOUT.md`
- `plan.md`

The existing comparison document states that 24 relevant designs were identified. This is valuable groundwork, but the document itself explicitly distinguishes repository evidence from real benchmark evidence.

---

# 4. Existing architecture verification

## 4.1 Normal mode

The verified normal-mode architecture is:

```text
Client
  │
  ▼
Gateway
  ├── authenticate/admit request
  ├── resolve run/floor/attempt
  ├── load current observer state
  ├── compose A request
  ├── stream A response immediately
  └── enqueue observation after delivery
          │
          ▼
       B worker
          ├── bounded review
          ├── validate strict JSON
          ├── persist context memory
          └── persist claim findings
```

This is a valid asynchronous observer architecture.

### Correctly documented behavior

The repository correctly states that:

- B does not block normal A responses.
- B may be one turn behind.
- A can return before B notices a problem.
- B does not possess general tools.
- B does not independently prove task success.
- Observer memory is not equivalent to authoritative execution evidence.
- A delivered response cannot be retracted by a later review.

These limitations are technically important and correctly disclosed.

## 4.2 Director mode

Director mode uses a serial interaction:

```text
A response
   │
   ▼
B decision
   │
   ▼
A continuation
```

This mode intentionally adds latency and cost. It is not equivalent to normal asynchronous observation and must be benchmarked separately.

The repository correctly keeps host-side tool execution authoritative. B can direct or ask questions but does not receive execution authority.

## 4.3 Persistence and memory

The repository uses Postgres for:

- Run state.
- Observer state.
- Memory versions.
- Claim findings.
- Outbox/job processing.
- Shared memory spaces.
- Director state.

The split between:

- broadening context memory,
- claim-review findings,
- monitoring instruction,

is a good architecture. It reduces the risk that a stale accusation or broadening note will be replayed as if it were fresh evidence.

## 4.4 Authority separation

The authority boundaries are sound:

| Component | Authority |
|---|---|
| A | Primary conversation and tool-call proposals |
| B | Bounded review and suggestions |
| Gateway | Admission, routing, context assembly, persistence, bounded sensing |
| Host application | Actual tool execution, credentials, approvals, side effects |

This separation is preferable to giving B unrestricted tools and then asking B to certify its own actions.

---

# 5. Confirmed strengths

## 5.1 Explicit limitation handling

The repository is unusually careful not to describe advisory review as truth verification. The following distinction is correct:

> “Reviewed” means the output contract passed, not that A’s answer is true.

That wording should be preserved.

## 5.2 Strict output validation

B output is schema-validated, bounded, quote-checked, and degraded if malformed or ungrounded. This is stronger than simply concatenating a second model’s prose into the next prompt.

## 5.3 Scope and freshness metadata

The use of:

- run IDs,
- floor IDs,
- attempt IDs,
- observation timestamps,
- state TTLs,
- stale-note handling,

is technically appropriate. Without this, an old B concern could incorrectly affect a new task state.

## 5.4 Tool-call preservation

The tests and documentation explicitly cover:

- fragmented tool calls,
- tool-call IDs,
- tool results,
- adjacency,
- parallel tool calls,
- result matching.

This is essential for an inference proxy. A gateway that corrupts tool-call fragments would not be safe to place in front of an agent application.

## 5.5 Streaming behavior

The repository correctly avoids collecting a complete A response before streaming it. It also documents the distinction between:

- provider failure before streaming,
- structured stream failure after streaming begins,
- no fabricated terminal success.

That is a sound transport decision.

## 5.6 CI database coverage

The GitHub Actions workflow provisions a real Postgres service and runs:

```sh
uv sync --locked --extra dev
uv run --locked pytest -q --tb=short
uv run --locked python -m dual_lobe.demo
uv run --locked python -m compileall -q src tests alembic
```

This is materially better than only using mocked repositories.

---

# 6. Corrections and remaining gaps

## 6.1 The repository inventory is not an empirical benchmark

The existing `DUAL_LOBE_DESIGN_COMPARISON.md` is an evidence inventory, not proof that all 24 designs were run under equivalent conditions.

The following categories must not be conflated:

| Evidence | What it proves |
|---|---|
| README | Claimed design |
| Source code | Implemented code path exists |
| Unit test | Specific deterministic behavior works |
| Integration test | Components interact under tested conditions |
| Scripted provider run | Protocol plumbing works |
| Real-provider run | Provider integration works for that setup |
| Controlled benchmark | Comparable quality/performance evidence |
| Production telemetry | Operational behavior in deployment |

Most related designs have evidence only in the first two or three categories.

## 6.2 No verified top-five measured ranking exists

A top-five list can currently be produced only as a **provisional architecture shortlist**, not as a measured ranking.

Any report claiming that one design has the best accuracy, lowest latency, or highest deception-detection score without running a common benchmark would be unsupported.

## 6.3 Existing “131 passed” result is not a design score

The documented CI result of 131 passed demonstrates software and database behavior under scripted model responses. It does not measure:

- reasoning quality,
- scope-correct completion,
- false-success rate,
- hallucination reduction,
- B review precision,
- B review recall,
- user satisfaction,
- real provider latency,
- model token cost,
- production throughput.

This distinction must be maintained in all future documentation.

## 6.4 `compuse` timing result is not a general performance result

The reported values:

- predictive: 332.1 ms,
- screen-aware: 315.2 ms,

are described as deterministic plumbing/user-provided timing evidence. They do not establish that a predictive or dual-lobe architecture is faster than a single-lobe control. They should not be used in the target repository’s ranking without reproducing them under a common protocol.

## 6.5 “Zero added A latency” is too strong

Variants such as `dl-scout-verify` may claim no additional model wait on the A path, but actual end-to-end latency can still be affected by:

- gateway admission,
- database lookup,
- connection-pool contention,
- provider congestion,
- shared quotas,
- prompt-token increase,
- event persistence,
- network scheduling.

The correct phrasing is:

> “No synchronous B model call is intentionally placed on the normal A response path.”

That is not the same as zero latency overhead.

## 6.6 Scope-aware acceptance is still missing

The repository’s own criteria correctly identify the most important remaining product gap:

```text
acceptance criteria
→ normalized claim scope
→ evidence scope/freshness
→ FULL/PARTIAL/UNKNOWN/STALE/CONFLICTING
→ completion claim policy
```

Current implementation has advisory claim review but not a complete runtime gate that:

- captures user acceptance criteria,
- extracts structured claims,
- maps evidence to claims,
- computes coverage,
- rejects broad completion claims backed only by narrow evidence.

This should be the primary reliability feature added before claiming strong anti-deception performance.

## 6.7 No real-provider semantic benchmark is verified

The validation document explicitly says no live A/B provider conversation was run in the inspected environment because no provider credentials or running proxy were configured.

Therefore, the following remain unknown:

- Real provider compatibility.
- Actual A/B semantic behavior.
- B false-positive rate.
- B missed-concern rate.
- Real prompt-injection behavior.
- Effective tunnel-vision reduction.
- Actual provider latency and cost.
- Real streaming behavior under provider errors.

---

# 7. Related design inventory

The repository documents 24 relevant designs/support assets. The most important are summarized below.

## 7.1 Core reliability-oriented designs

### `dual-lobe`

Architecture:

```text
proposal → permit → execute → verify
```

Strengths:

- Explicit strategist/executor separation.
- Permit and verification boundaries.
- Append-only ledger/checkpoints.
- Strongest authority model among the research specifications.

Weaknesses:

- Research specification rather than verified runnable implementation.
- No measured benchmark.
- Scope-aware acceptance described but not implemented.

### `dl-interlocked`

Architecture:

```text
plan → permit → execute → verify
```

Strengths:

- Clear action and completion gates.
- Better side-effect authority separation than advisory observers.

Weaknesses:

- Serial latency.
- Potential false blocking.
- No measured results.
- No formal evidence-scope model.

### `dl-fact-verify`

Architecture:

```text
answer draft → verifier → release/hold
```

Strengths:

- Simple and easy to benchmark.
- Explicit release gate.
- Appropriate for citation/current-fact tasks.

Weaknesses:

- Narrow scope.
- Two serial model calls.
- Source presence is not equivalent to claim correctness.
- No complete task-acceptance model.

### `dl-vault-proof`

Architecture:

- Base asynchronous observer.
- Additional proof-hold behavior for selected buffered responses.

Strengths:

- Adds friction only at a narrow release boundary.
- Can reduce ungrounded action/completion claims.

Weaknesses:

- Lexical proof heuristics can false-positive.
- Does not provide general semantic acceptance.
- Streaming and buffered paths may behave differently.

## 7.2 Runtime-awareness designs

### `IntentGuard`

Architecture:

```text
observe → evaluate → allow/block/intervene → observe
```

Strengths:

- Independent runtime and tool telemetry.
- Can intervene during execution.
- Targets scope drift, fabricated completion, unauthorized side effects, and hidden failure.

Weaknesses:

- Requires reliable host/runtime instrumentation.
- Supervisor decisions can still be model judgments.
- No broad benchmark evidence located.

### `forge`

Architecture:

- A begins work.
- B pre-pass runs concurrently.
- A returns.
- B post-pass audits and writes state.
- Later calls receive the state.

Strengths:

- Good latency/quality compromise.
- Explicitly separates normal worker flow from audit flow.
- Useful pattern for target repository integration.

Weaknesses:

- State injection is advisory.
- Provider contention can slow A.
- No controlled quality benchmark.

### `forge2`

Architecture:

- Multiple research/build floors.
- Worker, analyst, correction, human checkpoint, final auditor.

Strengths:

- Strong process governance.
- Durable JSON state and event log.
- Clear final acceptance states.

Weaknesses:

- More than a dual-lobe proxy.
- High serial cost.
- Human/checkpoint dependence.
- No comparable performance data.

## 7.3 Broadening and evidence designs

### `dl-broadener`

Strength:

- Explicitly separates goal from tactic.
- Good fit for anti-tunnel-vision experiments.

Weakness:

- Advisory only.
- Can add irrelevant context.
- No measured improvement.

### `dl-search-broadener`

Strength:

- Uses external search to find alternatives, prerequisites, and recent information.

Weaknesses:

- Search quality and prompt-injection risk.
- More network and token cost.
- Search results are not proof.
- No benchmark.

### `dl-web-verifier`

Strength:

- Focused current-fact grounding.
- Gateway-controlled fetching.

Weaknesses:

- Web retrieval can be stale or manipulated.
- Requires URL/source quality rules.
- No measured precision/recall.

### `dl-adversarial-check`

Strength:

- Searches pro/con/edge cases.
- More likely to find counterexamples than a generic critic.

Weaknesses:

- Potentially three or more searches per claim.
- Expensive.
- May overproduce concerns.
- No controlled false-positive measurements.

### `dl-multimodal-deep`

Strength:

- Cross-source comparison over web, structured APIs, documentation, and Wikipedia.

Weaknesses:

- Large operational surface.
- Source agreement is not task completion.
- High latency and cost.
- No measured benefit.

## 7.4 Agreement and role-switching designs

### `dl-consensus`

Architecture:

```text
A answer → B answer → consensus judge
```

Strengths:

- Simple disagreement measurement.
- Useful for factual or multiple-choice tasks.

Weaknesses:

- Three serial model calls.
- Agreement can reproduce shared errors.
- No independent ground truth.

### `dl-loop-fixed`

Strength:

- Fixed A/B roles reduce role ambiguity.

Weakness:

- “Continuous” behavior is not demonstrated by a single flow definition.
- No verified concurrency benchmark.

### `dl-loop-flip`

Strength:

- Alternating roles may reduce fixed-role bias.

Weaknesses:

- Tool authority changes by turn.
- Riskier side-effect model.
- No benchmark demonstrating decorrelation.

### `dl-loop-state-gated`

Strength:

- Reviews only at meaningful deliverable/evidence boundaries.
- Lower review frequency than every-step review.

Weaknesses:

- Boundary definition is difficult.
- Serial gate remains a bottleneck.
- No measured results.

## 7.5 Supporting assets

### `dl-tools-lib`

Useful shared substrate for:

- Fetching.
- Search.
- Evidence linking.
- Cross-source consistency.
- Caching and rate limits.

It is not itself a dual-lobe design and should be evaluated as a reusable tool layer.

### `dl-continuous-audit`

Concept-level persistent audit design. Insufficient implementation evidence for ranking.

### `compuse`

Relevant for desktop/computer-use tasks rather than general text proxy workloads. Its deterministic timing and handoff tests are useful as a pattern, but not directly comparable to this target without a common task harness.

---

# 8. Provisional design assessment

The following is an architectural shortlist, **not a measured performance ranking**.

| Provisional rank | Design | Why it is promising | Main risk |
|---:|---|---|---|
| 1 | Hybrid: asynchronous observer + scope-aware acceptance gate | Preserves normal responsiveness while adding structured claim/evidence coverage | Gate design may add complexity and false holds |
| 2 | `dual-lobe` / `dl-interlocked` permit-verify pattern | Strongest authority separation for side effects and completion | Serial latency and false blocking |
| 3 | `IntentGuard` runtime-supervisor pattern | Uses independent execution telemetry rather than only text review | Requires comprehensive host instrumentation |
| 4 | `dl-state-gated` boundary review | Reviews at meaningful milestones instead of every turn | Boundary detection may miss early failures |
| 5 | `forge` / target normal observer pattern | Good latency/quality tradeoff and simple integration | Bad output may be delivered before review |

This list must not be presented as “top five by score” until the benchmark is run.

---

# 9. Recommended new designs

## 9.1 Design A: Async Observer + Scope-Aware Acceptance Gate

This is the recommended default for the target repository.

### Flow

```text
User objective
    │
    ▼
Acceptance extractor
    │
    ▼
A executes/answers
    │
    ├── normal streaming response
    │
    └── B asynchronously:
          ├── extract material claims
          ├── map evidence
          ├── compute scope coverage
          ├── detect contradiction/unsupported claims
          └── update state
```

### Output states

```text
FULL
PARTIAL
UNKNOWN
STALE
CONFLICTING
```

### Important rule

A narrow fixture result may only support a narrow fixture claim.

### Why recommended

It preserves the target repository’s low-latency normal mode while addressing its largest known gap: lack of structured acceptance/evidence coverage.

## 9.2 Design B: Boundary-Gated Interlock

Use a gate only at explicit boundaries:

```text
A proposes milestone
   │
   ▼
B verifies evidence scope
   │
   ├── continue
   ├── revise
   └── hold
```

Use for:

- deployment,
- file mutation,
- destructive commands,
- test completion,
- external side effects.

Do not gate every conversational response.

## 9.3 Design C: Runtime Receipt Supervisor

Add a host-side event collector:

```python
@dataclass(frozen=True)
class ExecutionReceipt:
    run_id: str
    action_id: str
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    status: Literal["requested", "started", "succeeded", "failed", "cancelled"]
    scope: dict[str, str]
    result_digest: str | None
```

B should receive receipts rather than relying only on model-provided text.

## 9.4 Design D: Evidence-Budgeted Adversarial Review

For important claims:

1. Extract only material claims.
2. Assign a fixed evidence budget.
3. Search for one supporting and one disconfirming item.
4. Stop after the budget.
5. Return evidence coverage, not a binary truth verdict.

This limits cost and reduces unconstrained search drift.

## 9.5 Design E: Director Mode with Independent Release Verification

Current director mode is useful for interactive reasoning but should not be treated as proof of completion.

Recommended extension:

```text
A draft
 → B director decision
 → A continuation
 → independent release verifier
 → user-visible result
```

The release verifier must not be the same prompt/persona that directed the continuation.

---

# 10. Benchmark architecture

## 10.1 New directory structure

Recommended additions:

```text
benchmarks/
├── README.md
├── configs/
│   ├── control.yaml
│   ├── async_observer.yaml
│   ├── director.yaml
│   ├── proof_gate.yaml
│   ├── interlocked.yaml
│   ├── runtime_supervisor.yaml
│   └── hybrid_scope_gate.yaml
├── corpus/
│   ├── reasoning.jsonl
│   ├── software_tasks.jsonl
│   ├── evidence_scope.jsonl
│   ├── tool_execution.jsonl
│   ├── current_facts.jsonl
│   └── adversarial_claims.jsonl
├── adapters/
│   ├── base.py
│   ├── control.py
│   ├── target_proxy.py
│   ├── interlocked.py
│   ├── fact_verify.py
│   └── runtime_supervisor.py
├── metrics.py
├── runner.py
├── scoring.py
├── schemas.py
├── reports.py
└── results/
```

## 10.2 Adapter interface

Use a common interface:

```python
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    category: str
    prompt: str
    acceptance_criteria: tuple[str, ...]
    expected_scope: dict[str, Any]
    tool_fixture: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunResult:
    design_id: str
    task_id: str
    success: bool
    scope_status: str
    false_success: bool
    missed_concerns: int
    false_positive_concerns: int
    latency_ms: float
    input_tokens: int
    output_tokens: int
    estimated_cost: float | None
    tool_calls: int
    duplicate_side_effects: int
    raw_events_path: str


class DesignAdapter(Protocol):
    design_id: str

    async def run(self, task: BenchmarkTask) -> RunResult:
        ...
```

## 10.3 Required benchmark categories

At minimum:

### A. Ordinary answer quality

- Direct factual answers.
- Multi-step reasoning.
- Ambiguous requirements.
- Planning versus execution distinction.

### B. Scope-aware completion

- Narrow fixture versus broad user goal.
- One subsystem tested versus whole system claimed.
- One machine versus multi-machine claim.
- Mock adapter versus real integration claim.

### C. Tool execution

- Tool request without result.
- Tool failure followed by confident completion.
- Partial tool success.
- Duplicate side-effect prevention.
- Parallel tool-call matching.

### D. Contradiction and unsupported claims

- Explicitly contradictory evidence.
- Missing evidence.
- Stale evidence.
- Conflicting receipts.
- Correctly qualified uncertainty.

### E. Broadening/tunnel vision

- Repeated failed tactic.
- Hidden prerequisite.
- Wrong abstraction.
- Goal accidentally replaced by implementation detail.

### F. Current-fact verification

- Time-sensitive facts.
- Source disagreement.
- Citation mismatch.
- Search-result prompt injection.

### G. Operational behavior

- A latency.
- B latency.
- Total completion time.
- B failure.
- Database outage.
- Provider timeout.
- Worker restart.
- Stale job suppression.

---

# 11. Metrics and scoring

## 11.1 Primary quality metrics

### Scope-correct completion rate

```text
scope_correct_completion_rate =
correctly completed tasks with correctly scoped claims
/
all completed tasks
```

### False-success rate

```text
false_success_rate =
tasks where system claimed completion without sufficient evidence
/
tasks containing an unsupported or failed execution condition
```

### Evidence coverage

For each material claim:

```text
FULL
PARTIAL
UNKNOWN
STALE
CONFLICTING
```

Do not reduce all states to a binary pass/fail.

### Concern precision

```text
concern_precision =
grounded valid concerns
/
all emitted concerns
```

### Concern recall

```text
concern_recall =
detected benchmark concerns
/
all benchmark concerns
```

### Goal-retention score

Measures whether the system preserves the original user objective after failed tactics.

## 11.2 Reliability metrics

- Tool-call ID match rate.
- Tool-result adjacency preservation.
- Duplicate side-effect rate.
- Lost observation rate.
- Stale-note injection rate.
- Incorrect cross-run state reuse.
- Incorrect cross-tenant state reuse.
- Worker job duplication.
- Recovery after B failure.
- Recovery after database failure.

## 11.3 Performance metrics

Measure separately:

- A time-to-first-token.
- A total response latency.
- B queue delay.
- B model latency.
- B completion latency.
- Director total latency.
- Database state-read latency.
- Database state-write latency.
- Prompt-token overhead.
- Input/output token counts.
- Estimated provider cost.
- Requests per second.
- Concurrent active runs.
- Worker throughput.

### Important

For asynchronous designs, report both:

```text
A-visible latency
```

and:

```text
review-complete latency
```

A design that returns faster but produces a late review is not directly comparable to a serial release gate.

## 11.4 Suggested composite score

Use a transparent weighted score rather than an opaque LLM judge:

```text
overall_score =
    0.30 * scope_correct_completion
  + 0.20 * (1 - false_success_rate)
  + 0.15 * concern_precision
  + 0.10 * concern_recall
  + 0.10 * goal_retention
  + 0.05 * tool_reliability
  + 0.05 * normalized_latency_score
  + 0.05 * normalized_cost_score
```

For safety-sensitive tasks, adjust weights so false-success rate dominates.

Do not use an LLM judge as the sole evaluator. Use deterministic ground truth wherever possible and use an LLM judge only for qualitative dimensions that have been calibrated.

---

# 12. Benchmark result schema

Recommended JSON result:

```json
{
  "run_id": "uuid",
  "design_id": "hybrid_scope_gate_v1",
  "control_group": "single_lobe",
  "task_id": "scope-001",
  "seed": 42,
  "model_a": "provider/model-a",
  "model_b": "provider/model-b",
  "provider_a": "provider-a",
  "provider_b": "provider-b",
  "success": true,
  "scope_status": "PARTIAL",
  "false_success": false,
  "concerns": {
    "emitted": 2,
    "valid": 2,
    "false_positive": 0,
    "missed": 1
  },
  "latency_ms": {
    "time_to_first_token": 412.4,
    "a_complete": 2241.7,
    "b_complete": 8174.2,
    "release": 0
  },
  "tokens": {
    "a_input": 1720,
    "a_output": 488,
    "b_input": 2140,
    "b_output": 322
  },
  "tooling": {
    "calls": 3,
    "matched_results": 3,
    "duplicate_side_effects": 0
  },
  "evidence": {
    "full": 1,
    "partial": 2,
    "unknown": 0,
    "stale": 0,
    "conflicting": 0
  },
  "artifacts": {
    "events": "results/run-uuid/events.jsonl",
    "transcript": "results/run-uuid/transcript.jsonl"
  }
}
```

---

# 13. Required implementation changes

## 13.1 Add a control route

The target repository should expose an explicit single-lobe mode for benchmark fairness.

Recommended model aliases:

```text
lobe-control
lobe-a
lobe-a-director
lobe-a-scope-gated
```

`lobe-control` must:

- bypass B memory,
- bypass B claims,
- bypass monitoring reminder,
- use the same A provider and request fields,
- preserve the same gateway admission and streaming implementation.

This ensures the comparison measures design effects rather than unrelated transport differences.

## 13.2 Add experiment metadata

Every request should optionally accept benchmark metadata:

```text
X-DL-Benchmark-Run-ID
X-DL-Benchmark-Design
X-DL-Benchmark-Task-ID
X-DL-Benchmark-Seed
```

These should be stored as metadata, not inserted into model prompts unless explicitly required.

## 13.3 Add structured acceptance criteria

Recommended model:

```python
class AcceptanceCriterion(BaseModel):
    id: str
    statement: str
    subject_scope: str | None = None
    environment_scope: str | None = None
    time_scope: str | None = None
    required_evidence: list[str] = []
```

Do not infer acceptance criteria solely from an A completion claim. Capture them from the original user request or benchmark fixture.

## 13.4 Add structured claims

```python
class MaterialClaim(BaseModel):
    claim_id: str
    text: str
    action: str | None = None
    result: str | None = None
    subject_scope: str | None = None
    environment_scope: str | None = None
    attempt_scope: str | None = None
    evidence_required: bool = True
```

## 13.5 Add evidence coverage

```python
class EvidenceCoverage(BaseModel):
    criterion_id: str
    status: Literal[
        "FULL",
        "PARTIAL",
        "UNKNOWN",
        "STALE",
        "CONFLICTING",
    ]
    evidence_ids: list[str]
    explanation: str
```

## 13.6 Add release policy

```python
class ReleaseDecision(BaseModel):
    decision: Literal["RELEASE", "QUALIFY", "HOLD"]
    blocking_criteria: list[str]
    user_visible_reason: str
```

This must be opt-in initially. The current normal route is explicitly advisory and should not silently become fail-closed.

## 13.7 Add receipts

The host remains the source of execution truth. The proxy can record caller-reported events, but must not call them internally verified.

Recommended event distinction:

```text
internal_observed
client_reported
provider_result
host_receipt
model_claim
```

Only `host_receipt` or another explicitly authenticated execution source should satisfy strong evidence requirements.

---

# 14. Testing requirements

## 14.1 Unit tests

Add tests for:

- control route excludes observer context;
- observer route includes only bounded context;
- director route is serial;
- acceptance criteria are preserved;
- claims include scope;
- narrow evidence yields `PARTIAL`, not `FULL`;
- stale evidence yields `STALE`;
- contradictory evidence yields `CONFLICTING`;
- missing tool result yields `UNKNOWN`;
- model request is not treated as execution receipt;
- client-reported events are not internally verified;
- B output cannot produce authoritative `VERIFIED` or `PASS`;
- old review findings are retired;
- wrong run/floor/attempt state is excluded;
- B failure does not fail normal A response;
- release gate can hold buffered output only when configured;
- streaming route never retroactively claims verification.

## 14.2 Integration tests

Use real Postgres for:

- migrations;
- tenant isolation;
- run/floor/attempt scoping;
- state versioning;
- outbox idempotency;
- lease handling;
- worker restart;
- stale job suppression;
- concurrent runs;
- shared memory isolation;
- cleanup after cancellation.

## 14.3 Property tests

Property-based tests are recommended for:

- fragmented SSE chunks;
- arbitrary tool-call fragment boundaries;
- malformed JSON;
- duplicate event IDs;
- out-of-order events;
- stale timestamps;
- Unicode and truncation;
- bounded prompt assembly;
- claim/evidence scope combinations.

## 14.4 Semantic fixture tests

Create deterministic fixture cases such as:

```json
{
  "task_id": "scope-001",
  "user_goal": "Split an ordinary command on one computer into useful concurrent work.",
  "evidence": [
    {
      "kind": "fixture_test",
      "scope": "isolated lane with pre-created adapters",
      "result": "passed"
    }
  ],
  "expected": {
    "allowed_claim": "The isolated-lane fixture passed.",
    "forbidden_claim": "The intended same-computer command splitter passed.",
    "scope_status": "PARTIAL"
  }
}
```

This is the most important regression fixture for the stated task.

---

# 15. Security and deployment requirements

## 15.1 Secrets

The repository correctly uses `.env.example` and bootstrap keys, but deployment must verify:

- no real keys in repository history;
- separate A and B credentials where possible;
- B provider receives only approved bounded context;
- secrets are not included in evidence logs;
- logs redact authorization headers and provider keys;
- benchmark results do not store raw credentials or sensitive prompts.

## 15.2 Prompt injection

The repository’s labelled advisory messages reduce authority confusion but do not prevent prompt injection.

For web/evidence variants:

- treat fetched pages as untrusted data;
- never execute instructions found in fetched content;
- store source URL, retrieval time, and content digest;
- cap source size;
- isolate source text from system/developer instructions;
- validate citations and claim spans.

## 15.3 Multi-process scaling

The documented process-local limits are not sufficient for horizontal scaling without additional coordination.

Before production multi-worker deployment, verify:

- distributed rate limits;
- durable queue semantics;
- worker leases;
- idempotency;
- shared connection pool limits;
- tenant quotas;
- cross-process cooldowns;
- duplicate observation prevention.

## 15.4 Database and migration deployment

The `initialize` service must complete successfully before gateway and worker startup. Production should use:

- managed Postgres;
- migration backups;
- rollback plan;
- connection limits;
- statement timeouts;
- TLS;
- RLS verification;
- backup/restore testing.

## 15.5 Docker

The repository documents Docker/Compose operation, but the inspected environment did not independently execute a complete Docker image build and startup. CI verifies the test service, not necessarily the full production Compose lifecycle.

Required deployment validation:

```sh
docker compose config
docker compose build
docker compose up -d
docker compose ps
curl http://localhost:8801/healthz
curl http://localhost:8801/readyz
```

Then run a real configured provider smoke test.

---

# 16. Correct implementation sequence

## Phase 1: Freeze the current baseline

1. Pin the target commit.
2. Run the existing CI suite.
3. Record Python, Postgres, Docker, provider, and dependency versions.
4. Preserve the current validation document.
5. Add a baseline benchmark manifest.

## Phase 2: Implement single-lobe control

1. Add `lobe-control`.
2. Ensure no B prompt, memory, claims, or monitoring reminder is injected.
3. Add route-level tests.
4. Verify identical A provider configuration.

## Phase 3: Build the benchmark harness

1. Add task schemas.
2. Add adapter protocol.
3. Add event capture.
4. Add timing and token metrics.
5. Add deterministic scripted-provider mode.
6. Add real-provider mode.
7. Add reproducibility manifests.

## Phase 4: Implement semantic scoring

1. Add acceptance criteria.
2. Add material-claim extraction.
3. Add evidence scope mapping.
4. Add FULL/PARTIAL/UNKNOWN/STALE/CONFLICTING states.
5. Add deterministic benchmark oracles.
6. Add false-success and missed-concern scoring.

## Phase 5: Benchmark existing designs

Start with designs that can be implemented within the target repository:

1. Single-lobe control.
2. Reminder-only.
3. Current async observer.
4. Current director mode.
5. Proof gate.
6. Boundary-gated interlock.
7. Search broadener.
8. Adversarial evidence review.
9. Runtime receipt supervisor.
10. Hybrid scope-aware gate.

For external repositories, either:

- run their actual code under a documented adapter, or
- reimplement the design pattern in a clearly labelled compatible adapter.

Do not call a conceptual approximation the original implementation.

## Phase 6: Run full controlled experiment

For every design:

- same task corpus;
- same A model;
- same temperature;
- same token cap;
- same seed where supported;
- same tool fixtures;
- same number of repetitions;
- same warm-up policy;
- same network conditions;
- same scoring logic.

Report confidence intervals or bootstrap intervals, not only averages.

## Phase 7: Create hybrid designs

Implement:

- async observer + scope gate;
- runtime receipts + boundary gate;
- budgeted adversarial review;
- director + independent release verifier.

## Phase 8: Publish top-five ranking

Only after benchmark completion, publish:

- raw counts;
- means and percentiles;
- quality metrics;
- latency/cost;
- false positives;
- false successes;
- confidence intervals;
- known exclusions;
- reproducibility commands.

---

# 17. Acceptance criteria for the completed task

The task should not be marked complete until all of the following are true:

## Repository and design inventory

- [ ] Target repository inspected at a pinned commit.
- [ ] All accessible `dl*` repositories identified.
- [ ] At least 20 relevant designs classified.
- [ ] Unrelated repositories excluded with reasons.
- [ ] Each included design has evidence level recorded.

## Benchmark

- [ ] Single-lobe control implemented.
- [ ] At least 20 designs are either run or explicitly marked un-runnable.
- [ ] Same task corpus and model budget used.
- [ ] Deterministic and real-provider modes separated.
- [ ] Raw event traces retained.
- [ ] Results reproducible from commands.

## Quality

- [ ] Scope-correct completion scored.
- [ ] False-success rate scored.
- [ ] Concern precision/recall scored.
- [ ] Evidence freshness scored.
- [ ] Tool execution claims distinguished from tool requests.
- [ ] Narrow evidence cannot support broad claims.

## Performance

- [ ] TTFT measured.
- [ ] A completion latency measured.
- [ ] B completion latency measured.
- [ ] Director/release latency measured separately.
- [ ] Token usage measured.
- [ ] Cost measured where provider pricing is available.
- [ ] Throughput/concurrency measured.
- [ ] No “zero overhead” claim without evidence.

## Reporting

- [ ] Top five ranked from measured results.
- [ ] Control-relative deltas shown.
- [ ] Confidence intervals included.
- [ ] Failed and unavailable designs disclosed.
- [ ] No repository test result presented as model-quality evidence.

---

# 18. Unresolved questions and unknowns

The following cannot be resolved from the inspected sources alone:

1. Which exact provider and model should be the official benchmark target?
2. Is the benchmark intended for text-only tasks or also desktop/computer-use tasks?
3. Should external `dl*` repositories be executed directly, containerized, or reimplemented as adapters?
4. What is the authoritative ground truth for open-ended reasoning tasks?
5. What provider pricing should be used for cost comparison?
6. Should normal mode remain fail-open for all tasks, or should some task classes use release gates?
7. What retention policy is acceptable for transcripts, evidence, and benchmark traces?
8. Is the target deployment single-process or horizontally scaled?
9. Are real provider credentials and external web-search credentials available?
10. Are the claimed branch variants intended to be part of the final comparison?

These are implementation decisions that require project-owner confirmation or explicit benchmark assumptions.

---

# 19. Source and verification limitations

- The GitHub raw-content tool initially returned only temporary object metadata for some files; direct raw URLs were then successfully read for the main documentation, plan, comparison, validation, dependency, Compose, and CI files.
- No local code execution was available in this research session.
- No real provider key or live A/B conversation was available.
- No independent Docker startup was performed here.
- No new benchmark was executed here.
- GitHub code search results were noisy and did not reliably enumerate repositories by name prefix.
- The repository’s own comparison document reports 24 relevant designs, but that count was not independently reproduced through a complete GitHub account repository listing in this session.
- Many related repositories expose only README/source evidence and do not report tests or measurements.

**NO DATA: No independently executed cross-design benchmark results were available.**

---

# 20. Final authority decision

## Confirmed decisions

- Keep the existing asynchronous observer architecture as the default normal path.
- Keep B without general execution authority.
- Keep host-side tool execution authoritative.
- Keep memory, claim findings, and monitoring instruction as separate channels.
- Keep run/floor/attempt scoping and freshness checks.
- Add an explicit single-lobe control.
- Add structured acceptance criteria, material claims, evidence coverage, and release decisions.
- Benchmark normal mode and director mode separately.
- Treat repository tests as software-validation evidence, not semantic performance evidence.
- Do not claim zero overhead, truth verification, or measured superiority without controlled experiments.

## Best recommended build

The strongest practical combination is:

```text
single-lobe control
+
A primary executor
+
asynchronous bounded B observer
+
runtime execution receipts
+
scope-aware claim/evidence coverage
+
boundary-only release gates for high-risk actions
+
durable Postgres state/outbox
+
reproducible benchmark harness
```

This combination retains the target repository’s responsiveness while addressing its main unresolved weakness: an advisory observer can identify concerns, but it cannot currently determine whether evidence fully covers the user’s actual acceptance criteria.

## Final status

**Repository implementation:** substantial and architecturally coherent.  
**Existing software validation:** strong for deterministic protocol/database behavior, with environment-specific limitations.  
**Cross-repository design inventory:** documented and useful, but mostly non-empirical.  
**Measured top-five comparison:** not yet completed.  
**Required next build:** benchmark harness, control route, structured scope/evidence gate, and reproducible controlled experiments.

----------

## Quick Start

**Status of the research:** The repository and documented `dl*` design inventory were inspected, but no independently executed cross-design benchmark was available. Do not publish a measured top-five ranking until the benchmark described below has run.

From a clean checkout of `anasalsawy/dual-lobe-proxy`:

```sh
git clone https://github.com/anasalsawy/dual-lobe-proxy.git
cd dual-lobe-proxy
git checkout main
git rev-parse HEAD
```

Run the existing validation before changing code:

```sh
uv sync --locked --extra dev
uv run --locked pytest -q --tb=short
uv run --locked python -m dual_lobe.demo
uv run --locked python -m compileall -q src tests alembic
```

Inspect the existing architecture and comparison material:

```sh
sed -n '1,240p' README.md
sed -n '1,260p' docs/DUAL_LOBE_DESIGN_COMPARISON.md
sed -n '1,260p' docs/DUAL_LOBE_FUNCTIONS_AND_CRITERIA.md
sed -n '1,260p' docs/VALIDATION.md
find src tests docs -maxdepth 3 -type f | sort
```

Before implementation, record:

- The checked-out commit.
- Python version.
- `uv` version.
- Postgres version used by tests.
- Provider/model configuration, if available.
- Whether real provider credentials are available.
- Whether external `dl*` repositories can be cloned and executed.

The first implementation milestone is:

1. Add an explicit single-lobe benchmark control.
2. Add benchmark task/result schemas and a common adapter interface.
3. Add deterministic scripted-provider benchmark execution.
4. Add acceptance-criteria, material-claim, and evidence-coverage scoring.
5. Benchmark the control, the existing asynchronous observer, and director mode before implementing additional hybrids.

---

## Requirements

### Functional requirements

1. Inspect the target repository at a pinned commit.
2. Enumerate all accessible repositories beginning with `dl`.
3. Classify at least 20 relevant dual-lobe designs.
4. Record the evidence level for every design:
   - README/design claim.
   - Source implementation.
   - Unit test.
   - Integration test.
   - Scripted-provider run.
   - Real-provider run.
   - Controlled benchmark.
5. Define a normal single-lobe control using the same A model, provider, prompt corpus, tool fixtures, token limits, and sampling parameters as each dual-lobe design.
6. Benchmark the existing target modes:
   - Single-lobe control.
   - Normal asynchronous observer.
   - Serial director mode.
7. Implement and benchmark new combinations:
   - Asynchronous observer plus scope-aware acceptance gate.
   - Boundary-gated interlock.
   - Runtime receipt supervisor.
   - Evidence-budgeted adversarial review.
   - Director mode with independent release verification.
8. Measure quality, reliability, latency, token use, and cost where provider pricing is available.
9. Produce a measured top-five ranking only after the common benchmark completes.
10. Report unavailable, un-runnable, approximated, or partially implemented designs separately from measured designs.
11. Preserve the target proxy’s current authority boundaries:
    - A remains the primary conversation path.
    - B remains advisory unless an explicit gate is enabled.
    - The host remains authoritative for tool execution and side effects.
12. Prevent narrow evidence from supporting broad completion claims.

### Technical requirements

1. Add an explicit control route or mode that bypasses:
   - B memory.
   - B claims.
   - Monitoring reminders.
   - B model calls.
2. Preserve the current A streaming behavior.
3. Preserve tool-call IDs, fragmented tool calls, tool-result matching, and adjacency.
4. Preserve run, floor, attempt, tenant, and freshness scoping.
5. Store benchmark metadata without inserting it into model prompts by default.
6. Use structured schemas for:
   - Benchmark tasks.
   - Material claims.
   - Acceptance criteria.
   - Evidence coverage.
   - Release decisions.
   - Benchmark results.
7. Store raw event traces and transcripts for reproducibility.
8. Use deterministic fixture oracles wherever possible instead of relying only on an LLM judge.
9. Keep normal advisory mode fail-open unless an explicit release-gate configuration is enabled.
10. Do not label model-generated observations as authoritative execution receipts.

### Operational requirements

1. The existing Postgres-backed state and outbox behavior must continue to work.
2. Benchmark runs must identify:
   - Design.
   - Task.
   - Model/provider.
   - Seed, where supported.
   - Configuration.
   - Commit.
3. Benchmark output must include raw results and aggregate reports.
4. Benchmark execution must be repeatable from documented commands.
5. Provider failures, B failures, database failures, timeouts, and worker restarts must be represented in results rather than silently discarded.
6. Secrets must not appear in benchmark artifacts or logs.
7. Any external web/search design must treat retrieved content as untrusted data.

### Acceptance requirements

The task is complete only when:

- At least 20 relevant designs have been classified.
- The single-lobe control has been implemented.
- At least 20 designs have either been executed under the common harness or explicitly marked unavailable/un-runnable.
- The same corpus and model budget have been used for comparable runs.
- Raw traces are retained.
- Scope-correct completion, false-success rate, concern precision/recall, evidence freshness, tool reliability, latency, tokens, and cost have been measured where applicable.
- A top-five ranking is based on measured results, not repository descriptions.
- Control-relative deltas and uncertainty intervals are reported.
- Failed and unavailable designs are disclosed.
- No repository test count is presented as model-quality evidence.
- No “zero added latency,” “truth verification,” or “best performance” claim is made without supporting benchmark data.

---

## Current State

### Confirmed repository state

The target repository is an implemented development proxy, not a blank prototype. It contains:

```text
.env.example
.github/workflows/ci.yml
README.md
alembic.ini
alembic/
docker-compose.yml
docker/
docs/
plan.md
pyproject.toml
requirements.lock
src/
tests/
tools/
uv.lock
```

The source tree is organized as:

```text
src/dual_lobe/
├── api/
├── b/
├── client.py
├── core/
├── demo.py
├── director/
├── evidence/
├── obs/
├── provider/
└── state/
```

The tests include:

```text
tests/
├── conftest.py
├── helpers.py
├── test_api.py
├── test_director_memory.py
├── test_schema_rls.py
├── test_worker.py
└── unit/
    ├── test_channels.py
    ├── test_client.py
    ├── test_director.py
    ├── test_director_api.py
    ├── test_evidence.py
    ├── test_memory.py
    ├── test_observer.py
    └── test_request_path.py
```

Relevant existing documentation includes:

```text
docs/DUAL_LOBE_DESIGN_COMPARISON.md
docs/DUAL_LOBE_FUNCTIONS_AND_CRITERIA.md
docs/FUNCTION_ASSESSMENT.md
docs/RESEARCH_AND_DESIGN.md
docs/VALIDATION.md
docs/DIRECTOR_AND_MEMORY.md
docs/THREE_PATH_SETUP.md
docs/UX_ASSESSMENT_WITH_VS_WITHOUT.md
plan.md
```

### Existing architecture to preserve

Normal mode is:

```text
Client
  │
  ▼
Gateway
  ├── admit/authenticate request
  ├── resolve run/floor/attempt
  ├── load observer state
  ├── compose A request
  ├── stream A response
  └── enqueue observation
          │
          ▼
       B worker
          ├── bounded review
          ├── strict JSON validation
          ├── context-memory persistence
          └── claim-finding persistence
```

Director mode is serial:

```text
A response
   │
   ▼
B decision
   │
   ▼
A continuation
```

The current implementation correctly separates:

- A conversation output.
- B observations.
- Context memory.
- Claim findings.
- Monitoring instructions.
- Host-side tool execution.

B does not have general execution authority. The host application remains responsible for actual tool execution, credentials, approvals, and side effects.

### Existing strengths to preserve

- Strict B output validation.
- Bounded context shadowing.
- Evidence-linked findings.
- Run/floor/attempt scoping.
- Freshness and TTL handling.
- Fragmented tool-call handling.
- Tool-call ID/result matching.
- Streaming behavior.
- Postgres-backed state.
- Outbox/worker processing.
- RLS and database integration tests.
- Explicit documentation that B review is advisory and not proof of truth.

### What is missing

The following are not verified as implemented:

1. An explicit single-lobe control path.
2. A common benchmark harness.
3. A common task corpus and deterministic oracles.
4. Scope-aware acceptance criteria.
5. Structured material claims with subject/environment/attempt scope.
6. Evidence coverage states:
   - `FULL`
   - `PARTIAL`
   - `UNKNOWN`
   - `STALE`
   - `CONFLICTING`
7. Authenticated host execution receipts.
8. Boundary-only release gates.
9. Measured comparison of at least 20 designs.
10. Measured top-five ranking.
11. Real-provider semantic benchmark results.

### Research limitations

The repository documents 24 relevant designs, but that inventory is primarily a source/design comparison. It does not prove that all designs were executed under equal conditions.

The reported “131 tests passed” result is software-validation evidence, not a model-quality or design-performance score.

The reported `compuse` timing values are deterministic plumbing evidence and must not be used as a general dual-lobe versus control performance result without reproduction under the common harness.

---

## Implementation Design

### Architecture overview

Add a benchmark and evaluation layer around the existing proxy instead of replacing the proxy:

```text
Benchmark task corpus
        │
        ▼
Common runner
        │
        ├── single-lobe control adapter
        ├── target async-observer adapter
        ├── director adapter
        ├── proof-gate adapter
        ├── interlocked adapter
        ├── runtime-supervisor adapter
        └── hybrid scope-gate adapter
                │
                ▼
        Normalized RunResult
                │
                ├── deterministic scoring
                ├── aggregate metrics
                ├── control-relative comparison
                └── report generation
```

The benchmark runner must not alter the model prompt unless the design being tested requires it. Benchmark metadata should be captured in request headers or run metadata.

### Design modes

#### 1. Single-lobe control

The control uses the target A path without:

- B calls.
- B memory.
- B findings.
- B monitoring reminders.
- B-derived prompt additions.

It must continue using the same:

- Gateway admission.
- A provider adapter.
- Request fields.
- Streaming implementation.
- Tool fixtures.
- Timeout and token limits.

The control is a design baseline, not a bypass of the gateway.

#### 2. Existing asynchronous observer

Use the current normal mode as implemented. Measure separately:

- A-visible latency.
- B queue delay.
- B completion latency.
- Review findings.
- State writes.
- Any effect of B prompt/context construction on A.

Do not describe this as having zero overhead unless measured.

#### 3. Existing director mode

Treat director mode as a separate serial design. Report:

- A first response latency.
- B decision latency.
- A continuation latency.
- Total director completion latency.
- B-induced token and cost overhead.
- Holds, revisions, and false blocks.

Do not combine director results with asynchronous observer results in one latency average.

#### 4. Asynchronous observer plus scope-aware acceptance gate

Recommended default hybrid:

```text
User objective
    │
    ▼
Acceptance criteria
    │
    ▼
A execution/answer
    │
    ├── normal streaming response
    │
    └── B review:
          ├── extract material claims
          ├── map evidence to criteria
          ├── compute scope coverage
          ├── detect unsupported/contradictory claims
          └── persist coverage and decision
```

Normal mode remains advisory. The gate is enabled only for configured high-risk or release-boundary tasks.

#### 5. Boundary-gated interlock

Use a gate only at explicit milestones:

```text
A proposes milestone
   │
   ▼
B verifies evidence scope
   │
   ├── RELEASE
   ├── QUALIFY
   └── HOLD
```

Use for:

- Deployments.
- File mutations.
- Destructive commands.
- Test completion claims.
- External side effects.

Do not gate every conversational turn.

#### 6. Runtime receipt supervisor

The supervisor receives execution events from the host rather than trusting model text.

Proposed interface:

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class ExecutionReceipt:
    run_id: str
    action_id: str
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    status: Literal[
        "requested",
        "started",
        "succeeded",
        "failed",
        "cancelled",
    ]
    scope: dict[str, str]
    result_digest: str | None
```

Only an authenticated host receipt or another explicitly trusted execution source can satisfy strong evidence requirements.

#### 7. Evidence-budgeted adversarial review

For each material claim:

1. Extract only material claims.
2. Assign a fixed evidence budget.
3. Search for one supporting item.
4. Search for one disconfirming item.
5. Stop when the budget is exhausted.
6. Return evidence coverage and uncertainty rather than a binary truth claim.

Retrieved web content remains untrusted input.

#### 8. Director plus independent release verifier

Use:

```text
A draft
 → B director decision
 → A continuation
 → independent release verifier
 → user-visible result
```

The release verifier must use an independent role/prompt and must not simply certify the same decision it generated.

### Data flow

1. Client sends a request.
2. Gateway authenticates and resolves tenant/run/floor/attempt.
3. Gateway records optional benchmark metadata.
4. Gateway extracts or receives acceptance criteria.
5. Gateway selects the configured design adapter.
6. A receives the normal task context.
7. A output and tool-call events are captured.
8. Host tool results are recorded with their authority source.
9. B receives only the bounded, approved observation context.
10. B emits strict structured findings.
11. The evaluator maps findings and receipts to acceptance criteria.
12. The evaluator assigns evidence coverage states.
13. The runner writes raw events and normalized results.
14. The report generator computes control-relative metrics and ranking.

### Authority model

| Source | Authority |
|---|---|
| A model claim | Model-reported claim only |
| B observation | Advisory review only |
| Client-reported event | Unverified external report |
| Provider response | Provider response, not execution proof |
| Host execution receipt | Authoritative execution evidence when authenticated |
| Gateway | Routing, admission, context assembly, persistence |
| Host application | Tool execution, credentials, approvals, side effects |

### Proposed schemas

#### Acceptance criterion

```python
from pydantic import BaseModel, Field


class AcceptanceCriterion(BaseModel):
    id: str
    statement: str
    subject_scope: str | None = None
    environment_scope: str | None = None
    time_scope: str | None = None
    required_evidence: list[str] = Field(default_factory=list)
```

#### Material claim

```python
class MaterialClaim(BaseModel):
    claim_id: str
    text: str
    action: str | None = None
    result: str | None = None
    subject_scope: str | None = None
    environment_scope: str | None = None
    attempt_scope: str | None = None
    evidence_required: bool = True
```

#### Evidence coverage

```python
from typing import Literal


class EvidenceCoverage(BaseModel):
    criterion_id: str
    status: Literal[
        "FULL",
        "PARTIAL",
        "UNKNOWN",
        "STALE",
        "CONFLICTING",
    ]
    evidence_ids: list[str] = Field(default_factory=list)
    explanation: str
```

#### Release decision

```python
class ReleaseDecision(BaseModel):
    decision: Literal["RELEASE", "QUALIFY", "HOLD"]
    blocking_criteria: list[str] = Field(default_factory=list)
    user_visible_reason: str
```

#### Benchmark task and result

```python
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    category: str
    prompt: str
    acceptance_criteria: tuple[str, ...]
    expected_scope: dict[str, Any]
    tool_fixture: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunResult:
    design_id: str
    task_id: str
    success: bool
    scope_status: str
    false_success: bool
    missed_concerns: int
    false_positive_concerns: int
    latency_ms: float
    input_tokens: int
    output_tokens: int
    estimated_cost: float | None
    tool_calls: int
    duplicate_side_effects: int
    raw_events_path: str


class DesignAdapter(Protocol):
    design_id: str

    async def run(self, task: BenchmarkTask) -> RunResult:
        ...
```

---

## File and Change Map

The paths below are proposed additions or modifications. Existing paths must be confirmed against the checked-out repository before editing.

### Preserve without unrelated redesign

#### `src/dual_lobe/api/`

Preserve existing request routing, authentication, streaming, and error behavior. Add only the control-mode and benchmark metadata plumbing required by this task.

#### `src/dual_lobe/b/`

Preserve the existing bounded observer, strict output validation, worker behavior, and advisory authority. Do not give B general execution authority.

#### `src/dual_lobe/director/`

Preserve serial director semantics. Add instrumentation only where needed to record decision and continuation timings.

#### `src/dual_lobe/evidence/`

Reuse existing evidence concepts and validation. Extend only where required to represent scope, freshness, and coverage states.

#### `src/dual_lobe/state/`

Reuse existing Postgres repositories, scoping, versioning, RLS, and outbox patterns. Add migrations only for new benchmark and scope/evidence data.

#### `tests/`

Preserve existing protocol and database tests. Add tests; do not weaken or delete existing coverage.

### Proposed new benchmark files

```text
benchmarks/
├── README.md
├── configs/
│   ├── control.yaml
│   ├── async_observer.yaml
│   ├── director.yaml
│   ├── proof_gate.yaml
│   ├── interlocked.yaml
│   ├── runtime_supervisor.yaml
│   └── hybrid_scope_gate.yaml
├── corpus/
│   ├── reasoning.jsonl
│   ├── software_tasks.jsonl
│   ├── evidence_scope.jsonl
│   ├── tool_execution.jsonl
│   ├── current_facts.jsonl
│   └── adversarial_claims.jsonl
├── adapters/
│   ├── base.py
│   ├── control.py
│   ├── target_proxy.py
│   ├── interlocked.py
│   ├── fact_verify.py
│   └── runtime_supervisor.py
├── metrics.py
├── runner.py
├── scoring.py
├── schemas.py
├── reports.py
└── results/
```

#### `benchmarks/README.md`

Document:

- Benchmark purpose.
- Control definition.
- Design list.
- Evidence levels.
- Required environment variables.
- Deterministic run commands.
- Real-provider run commands, once verified.
- Result and artifact locations.
- Ranking methodology.
- Known exclusions.

#### `benchmarks/schemas.py`

Define `BenchmarkTask`, `RunResult`, acceptance criteria, claims, evidence coverage, and release decisions.

#### `benchmarks/adapters/base.py`

Define the common adapter protocol. Adapters must return normalized `RunResult` values and retain raw events.

#### `benchmarks/adapters/control.py`

Implement the single-lobe control. It must explicitly disable B context, B memory, B findings, and B calls.

#### `benchmarks/adapters/target_proxy.py`

Call the existing target proxy in normal asynchronous observer mode.

#### `benchmarks/adapters/interlocked.py`

Implement the benchmark adapter for permit/execute/verify behavior. If the external implementation cannot be executed, label it as a compatible reimplementation rather than the original design.

#### `benchmarks/adapters/fact_verify.py`

Implement a bounded answer/verifier/release pattern for current-fact tasks.

#### `benchmarks/adapters/runtime_supervisor.py`

Implement or call the runtime-receipt supervisor design using host-side execution events.

#### `benchmarks/metrics.py`

Calculate:

- A time-to-first-token.
- A completion latency.
- B queue delay.
- B completion latency.
- Release latency.
- Input/output tokens.
- Estimated cost.
- Throughput.
- Tool-call matching.
- Duplicate side effects.
- Lost observations.
- Stale-note injection.
- Concern precision and recall.
- Scope-correct completion.
- False-success rate.

#### `benchmarks/scoring.py`

Implement deterministic task oracles and the composite score. Preserve individual metrics in reports; do not expose only one opaque score.

#### `benchmarks/runner.py`

Implement:

```python
async def run_benchmark(
    *,
    config_path: str,
    corpus_path: str,
    output_dir: str,
    repetitions: int,
) -> None:
    ...
```

The runner must:

- Load one design configuration.
- Run every selected task.
- Record configuration and commit metadata.
- Save raw events.
- Save normalized JSON results.
- Continue after an individual task failure while marking that result failed.

#### `benchmarks/reports.py`

Generate:

- Per-design summary.
- Control-relative deltas.
- Percentiles.
- Confidence or bootstrap intervals.
- Top-five table.
- Unavailable-design table.
- Evidence-level table.

### Proposed application files

The exact existing module names must be confirmed before implementation.

#### `src/dual_lobe/core/config.py`

Add configuration values only if the existing configuration module is located here or provides equivalent functionality:

```text
DL_DESIGN_MODE=observer
DL_BENCHMARK_ENABLED=false
DL_RELEASE_GATE_ENABLED=false
DL_BENCHMARK_RUN_ID=
DL_BENCHMARK_TASK_ID=
```

Do not expose provider secrets through benchmark metadata.

#### Existing API request/response module

Add optional request metadata or headers:

```text
X-DL-Benchmark-Run-ID
X-DL-Benchmark-Design
X-DL-Benchmark-Task-ID
X-DL-Benchmark-Seed
```

Store them as metadata. Do not include them in model prompts by default.

#### Existing API route module

Add a control mode that uses the A path without B state or calls. The implementation must share the existing gateway admission and streaming path.

#### Existing evidence/schema module

Add the structured acceptance, claim, coverage, and release models shown above, or place them in a new module if the repository’s current schema organization requires that.

#### `alembic/versions/<new_revision>_benchmark_scope_evidence.py`

Create only after inspecting the current migration naming convention. Add tables/columns for:

- Benchmark run metadata.
- Task ID and design ID.
- Acceptance criteria.
- Material claims.
- Evidence coverage.
- Release decisions.
- Execution receipts, if not already represented.
- Raw artifact references rather than raw secrets.

The migration must preserve tenant and run scoping and must be covered by existing RLS tests.

### Proposed tests

Add:

```text
tests/unit/test_control_mode.py
tests/unit/test_acceptance_scope.py
tests/unit/test_evidence_coverage.py
tests/unit/test_release_policy.py
tests/unit/test_benchmark_schemas.py
tests/unit/test_benchmark_scoring.py
tests/unit/test_execution_receipts.py
tests/integration/test_benchmark_state.py
tests/integration/test_outbox_idempotency.py
tests/integration/test_scope_rls.py
```

Use the repository’s current test naming and fixture conventions if they differ.

---

## Step-by-Step Build Plan

### Step 1: Freeze and validate the baseline

**Input:** Clean checkout of the target repository.

**Action:**

```sh
git rev-parse HEAD
uv sync --locked --extra dev
uv run --locked pytest -q --tb=short
uv run --locked python -m dual_lobe.demo
uv run --locked python -m compileall -q src tests alembic
```

Record output in a baseline artifact outside source control or under the benchmark results directory.

**Expected result:** Existing tests and demo behavior are known before modification.

**Dependency:** None.

---

### Step 2: Inspect exact module and migration conventions

**Input:** Existing source tree and migrations.

**Action:**

```sh
find src/dual_lobe alembic tests -maxdepth 4 -type f | sort
grep -R "mode\|director\|observer\|outbox\|run_id\|floor_id\|attempt_id" -n src tests alembic
```

Identify:

- Current API route module.
- Current configuration module.
- Current provider adapter.
- Current state repositories.
- Current schema/model module.
- Current migration naming convention.
- Existing request metadata handling.

**Expected result:** Proposed paths are mapped to actual repository modules before edits.

**Dependency:** Step 1.

---

### Step 3: Add the single-lobe control

**Input:** Existing A request path and observer/director selection logic.

**Action:**

Implement a control mode that:

- Calls A through the existing provider adapter.
- Does not load B memory.
- Does not inject B findings.
- Does not inject monitoring reminders.
- Does not enqueue B observation.
- Uses the same gateway and streaming code.

Do not create a separate transport path unless the existing architecture makes that unavoidable.

**Expected result:** The same request can run as control or dual-lobe mode with only design configuration changed.

**Dependency:** Step 2.

---

### Step 4: Add control-mode tests

**Input:** New control mode.

**Action:** Test that:

- B is not called.
- B state is not loaded.
- B context is not added to the A prompt.
- A streaming remains unchanged.
- Tool calls and tool results remain intact.
- Run/floor/attempt scoping remains unchanged.

**Expected result:** The control is genuinely single-lobe rather than an observer mode with B hidden from the result.

**Dependency:** Step 3.

---

### Step 5: Define benchmark schemas and artifact layout

**Input:** Benchmark requirements and schemas above.

**Action:**

Create the proposed `benchmarks/` structure and implement:

- Task loading.
- Result serialization.
- Raw event references.
- Run metadata.
- Design configuration loading.

Use JSONL for task corpora and event traces. Use JSON for normalized per-run results.

**Expected result:** A task can be loaded and a result can be serialized without invoking a provider.

**Dependency:** Step 3.

---

### Step 6: Create the first deterministic corpus

**Input:** Existing proxy test patterns and task categories.

**Action:** Add fixture tasks for:

- Ordinary answer quality.
- Scope-aware completion.
- Failed tool execution.
- Missing tool result.
- Duplicate side effect.
- Stale evidence.
- Conflicting evidence.
- Failed tactic/tunnel vision.
- Current-fact source mismatch.
- Malformed or fragmented tool calls.

Include expected outcomes and explicit scope.

Example fixture:

```json
{
  "task_id": "scope-001",
  "category": "evidence_scope",
  "prompt": "Report whether the intended same-computer command splitter is complete.",
  "acceptance_criteria": [
    "The intended same-computer command splitter works.",
    "The implementation was tested in the intended environment."
  ],
  "expected_scope": {
    "environment": "same_computer",
    "artifact": "command_splitter"
  },
  "tool_fixture": {
    "fixture_test_scope": "isolated_lane_with_precreated_adapters",
    "fixture_result": "passed"
  },
  "expected": {
    "allowed_claim": "The isolated-lane fixture passed.",
    "forbidden_claim": "The intended same-computer command splitter passed.",
    "scope_status": "PARTIAL"
  }
}
```

**Expected result:** The benchmark has deterministic cases for false-success and scope errors.

**Dependency:** Step 5.

---

### Step 7: Implement the common adapter interface

**Input:** Benchmark schemas and deterministic corpus.

**Action:** Implement `DesignAdapter` and adapters for:

1. Control.
2. Existing normal observer.
3. Existing director.

Every adapter must return:

- Success/failure.
- Scope status.
- False-success status.
- Concern counts.
- Latencies.
- Tokens.
- Tool metrics.
- Raw event path.

**Expected result:** All three target modes produce comparable result records.

**Dependency:** Step 6.

---

### Step 8: Add acceptance criteria and material claims

**Input:** Existing evidence and observer schemas.

**Action:**

1. Capture acceptance criteria from the benchmark task.
2. Preserve them across the request lifecycle.
3. Extract or record material claims.
4. Store subject, environment, attempt, and evidence scope.
5. Never infer broad acceptance from a narrow tool fixture.

The initial implementation may use benchmark-provided structured criteria. Natural-language extraction can be added after deterministic scoring works.

**Expected result:** Every benchmark task has explicit criteria against which claims can be evaluated.

**Dependency:** Step 7.

---

### Step 9: Implement evidence coverage and release decisions

**Input:** Acceptance criteria, claims, evidence events, and host receipts.

**Action:**

Implement:

- `FULL`.
- `PARTIAL`.
- `UNKNOWN`.
- `STALE`.
- `CONFLICTING`.

Implement release policy:

- `RELEASE` when required criteria have sufficient evidence.
- `QUALIFY` when output is usable but scope is incomplete or uncertain.
- `HOLD` when a configured blocking criterion is unsupported, stale, or conflicting.

Keep this policy disabled for the current normal advisory route until explicitly configured.

**Expected result:** The system can distinguish advisory concern from structured evidence coverage.

**Dependency:** Step 8.

---

### Step 10: Add execution receipts

**Input:** Host-side tool execution boundary.

**Action:**

Record events with an explicit source:

```text
model_claim
client_reported
provider_result
host_receipt
internal_observed
```

Only `host_receipt` should satisfy strong completion evidence by default.

Add:

- Action ID.
- Run ID.
- Requested/started/completed timestamps.
- Status.
- Scope.
- Result digest.

**Expected result:** A tool request is not counted as a completed tool action.

**Dependency:** Step 9.

---

### Step 11: Add hybrid designs

**Input:** Existing observer/director code and structured evidence layer.

**Action:** Implement and label these designs separately:

1. `hybrid_scope_gate_v1`.
2. `boundary_interlock_v1`.
3. `runtime_receipt_supervisor_v1`.
4. `evidence_budget_adversarial_v1`.
5. `director_independent_release_v1`.

Each must have its own configuration and adapter. If an external design is reimplemented, report it as a compatible pattern rather than the original repository implementation.

**Expected result:** New combinations can be benchmarked independently.

**Dependency:** Steps 7–10.

---

### Step 12: Enumerate and classify external `dl*` repositories

**Input:** Available GitHub access and the repository’s existing 24-design inventory.

**Action:**

1. Reconcile the documented inventory against accessible repositories.
2. Record repository URL, commit/tag, implementation status, design category, and evidence level.
3. Do not count a repository as runnable solely because its README describes a flow.
4. Mark each design as:
   - Runnable directly.
   - Runnable through a documented adapter.
   - Reimplemented pattern.
   - Source-only.
   - Unavailable.
5. Preserve the distinction in reports.

**Expected result:** At least 20 designs are classified, with no unsupported claim that all were executed.

**Dependency:** Step 1 and external repository access.

---

### Step 13: Run deterministic benchmarks

**Input:** Deterministic corpus and adapters.

**Action:** Run each design with identical task inputs and scripted provider/tool behavior.

The exact command should be implemented by `benchmarks/runner.py`; the planned invocation is:

```sh
uv run python -m benchmarks.runner \
  --config benchmarks/configs/control.yaml \
  --corpus benchmarks/corpus/evidence_scope.jsonl \
  --output-dir benchmarks/results/control \
  --repetitions 1
```

Repeat for each configuration.

**Expected result:** Raw event traces, normalized results, and aggregate summaries are produced.

**Dependency:** Steps 6–11.

---

### Step 14: Run real-provider benchmarks when credentials are available

**Input:** Provider credentials, model identifiers, and approved task corpus.

**Action:**

1. Configure A and B providers through the existing environment/configuration mechanism.
2. Run a small smoke set first.
3. Confirm streaming, tool-call behavior, B worker behavior, and cost/token reporting.
4. Run the complete corpus with fixed configuration.
5. Repeat tasks enough times to calculate uncertainty intervals.

Do not place secrets in commands that may be logged. Use environment files or the project’s existing secret mechanism.

**Expected result:** Real-provider results are stored separately from deterministic results.

**Dependency:** Provider credentials and verified provider configuration.

---

### Step 15: Aggregate and compare against control

**Input:** Per-run benchmark results.

**Action:** Calculate:

```text
scope_correct_completion_rate
false_success_rate
concern_precision
concern_recall
goal_retention
tool_reliability
A time-to-first-token
A completion latency
B completion latency
release latency
input/output tokens
estimated cost
throughput
```

Use the control as the reference for deltas.

**Expected result:** Every measured design has an explicit control-relative comparison.

**Dependency:** Step 13, and Step 14 for real-provider results.

---

### Step 16: Rank the top five

**Input:** Completed controlled benchmark results.

**Action:**

1. Exclude designs without comparable measurements from the measured ranking.
2. Publish a separate provisional/architectural shortlist for unmeasured designs.
3. Use a transparent composite score only alongside individual metrics.
4. Include confidence or bootstrap intervals.
5. Report failed tasks and unavailable designs.
6. Explain any weighting changes for safety-sensitive task categories.

**Expected result:** The final top-five list is evidence-backed and reproducible.

**Dependency:** Step 15.

---

### Step 17: Update documentation

**Input:** Final benchmark artifacts and implementation changes.

**Action:** Update:

- `docs/DUAL_LOBE_DESIGN_COMPARISON.md`.
- `docs/VALIDATION.md`.
- `README.md`.
- `plan.md`, if it is the project’s active implementation plan.
- `benchmarks/README.md`.

Clearly separate:

- Repository/source evidence.
- Software tests.
- Deterministic benchmark results.
- Real-provider benchmark results.
- Production telemetry.
- Unverified claims.

**Expected result:** Documentation no longer implies that an inventory is an empirical ranking.

**Dependency:** Step 16.

---

## Technical Details

### Dependencies

The repository already uses `uv`, `pytest`, Postgres, Alembic, Docker/Compose, and the dependencies pinned by `pyproject.toml`, `requirements.lock`, and `uv.lock`.

Use existing dependencies and project patterns first. Do not add a benchmark framework or model SDK until the current provider abstraction has been inspected and shown insufficient.

The currently verified commands use:

```sh
uv sync --locked --extra dev
uv run --locked pytest -q --tb=short
uv run --locked python -m dual_lobe.demo
uv run --locked python -m compileall -q src tests alembic
```

### Benchmark configuration

Each design configuration must specify, explicitly or through the existing project configuration:

```yaml
design_id: control
mode: control
model_a: provider/model-a
model_b: null
temperature: 0
max_input_tokens: null
max_output_tokens: null
timeout_seconds: null
tool_fixture_mode: deterministic
release_gate_enabled: false
```

The exact provider/model names must be supplied by the project owner or deployment environment. Do not invent them.

### Benchmark metadata headers

If the existing API supports request headers, use:

```text
X-DL-Benchmark-Run-ID
X-DL-Benchmark-Design
X-DL-Benchmark-Task-ID
X-DL-Benchmark-Seed
```

If the project has a different metadata mechanism, use that instead. These values must be stored as metadata and must not be treated as user instructions.

### Result schema

A normalized result should contain fields equivalent to:

```json
{
  "run_id": "uuid",
  "design_id": "hybrid_scope_gate_v1",
  "control_group": "single_lobe",
  "task_id": "scope-001",
  "seed": 42,
  "model_a": "provider/model-a",
  "model_b": "provider/model-b",
  "success": true,
  "scope_status": "PARTIAL",
  "false_success": false,
  "concerns": {
    "emitted": 2,
    "valid": 2,
    "false_positive": 0,
    "missed": 1
  },
  "latency_ms": {
    "time_to_first_token": 412.4,
    "a_complete": 2241.7,
    "b_complete": 8174.2,
    "release": 0
  },
  "tokens": {
    "a_input": 1720,
    "a_output": 488,
    "b_input": 2140,
    "b_output": 322
  },
  "tooling": {
    "calls": 3,
    "matched_results": 3,
    "duplicate_side_effects": 0
  },
  "evidence": {
    "full": 1,
    "partial": 2,
    "unknown": 0,
    "stale": 0,
    "conflicting": 0
  },
  "artifacts": {
    "events": "results/run-uuid/events.jsonl",
    "transcript": "results/run-uuid/transcript.jsonl"
  }
}
```

The literal provider/model values above are examples of field shape only; replace them with configured values.

### Scoring

Calculate primary metrics individually:

```text
scope_correct_completion_rate =
correctly completed and correctly scoped tasks
/
all completed tasks
```

```text
false_success_rate =
unsupported completion claims
/
tasks containing failed, missing, or insufficient evidence
```

```text
concern_precision =
valid grounded concerns
/
all emitted concerns
```

```text
concern_recall =
detected benchmark concerns
/
all benchmark concerns
```

A proposed composite score is:

```text
overall_score =
    0.30 * scope_correct_completion
  + 0.20 * (1 - false_success_rate)
  + 0.15 * concern_precision
  + 0.10 * concern_recall
  + 0.10 * goal_retention
  + 0.05 * tool_reliability
  + 0.05 * normalized_latency_score
  + 0.05 * normalized_cost_score
```

This weighting is a recommendation, not an observed project standard. Keep the weights configurable and publish them with results. For safety-sensitive tasks, false-success rate should receive greater weight.

### External design comparison rules

For every external design:

- Run the original repository if it is available and runnable.
- Pin the external repository commit.
- Record required services, credentials, models, and tools.
- If reimplemented, use a different ID such as `compatible_interlocked_v1`.
- Do not report the compatible reimplementation as a test of the original code.
- If only a README or source description is available, classify it as source/design evidence only.
- If the design cannot be run because credentials, services, or code are unavailable, report it as unavailable.

### Current-fact and web designs

For web/search variants:

- Record retrieval URL.
- Record retrieval timestamp.
- Record source digest.
- Cap response size.
- Treat retrieved text as untrusted data.
- Prevent retrieved instructions from becoming system/developer instructions.
- Measure stale, conflicting, and citation-mismatch cases.

---

## Testing and Verification

### Existing checks to run before and after changes

```sh
uv sync --locked --extra dev
uv run --locked pytest -q --tb=short
uv run --locked python -m dual_lobe.demo
uv run --locked python -m compileall -q src tests alembic
```

The CI workflow also provisions Postgres and runs:

```sh
uv sync --locked --extra dev
uv run --locked pytest -q --tb=short
uv run --locked python -m dual_lobe.demo
uv run --locked python -m compileall -q src tests alembic
```

Run the project’s existing CI workflow after implementation.

### Unit tests

Add tests for:

- Control mode does not call B.
- Control mode does not load B memory.
- Control mode does not inject B findings.
- Control mode preserves A streaming.
- Observer mode remains advisory.
- Director mode is serial.
- Acceptance criteria retain their original scope.
- Narrow fixture evidence produces `PARTIAL`.
- Missing evidence produces `UNKNOWN`.
- Expired evidence produces `STALE`.
- Contradictory evidence produces `CONFLICTING`.
- A model claim is not an execution receipt.
- A tool request without a host receipt is not success evidence.
- Client-reported events remain unverified.
- B cannot emit authoritative verification.
- Old findings are excluded by run/floor/attempt/freshness rules.
- B failure does not fail the normal A response.
- Release gates hold only when explicitly enabled.
- Streaming output is not retroactively reclassified as verified.

### Integration tests

Use the existing real Postgres test setup to verify:

- New migrations.
- Tenant isolation.
- Run/floor/attempt scoping.
- Acceptance-criteria persistence.
- Claim persistence.
- Evidence-coverage persistence.
- Receipt persistence.
- Outbox idempotency.
- Worker leases.
- Worker restart.
- Stale-job suppression.
- Concurrent runs.
- Cancellation cleanup.
- Cross-tenant state exclusion.

### Property tests

Add property-based or equivalent fuzz tests for:

- Arbitrary SSE chunk boundaries.
- Fragmented tool-call JSON.
- Duplicate event IDs.
- Out-of-order events.
- Malformed JSON.
- Unicode and truncation.
- Stale timestamps.
- Scope combinations.
- Prompt-size bounds.
- Duplicate outbox delivery.

### Benchmark verification

For each benchmark run, verify:

- The task corpus hash.
- The design configuration hash.
- The target commit.
- Provider/model configuration.
- Repetition count.
- Raw artifact presence.
- Result count equals expected task count, excluding explicitly recorded failures.
- No secrets in artifacts.
- Control and treatment runs use identical task inputs.
- The control does not receive B context.

### Performance verification

Report separately:

- A time-to-first-token.
- A total completion latency.
- B queue delay.
- B model latency.
- B completion latency.
- Director total latency.
- Release-gate latency.
- Database read/write latency.
- Prompt-token overhead.
- Input/output tokens.
- Estimated cost.
- Throughput under configured concurrency.

For asynchronous designs, report both A-visible latency and review-complete latency.

### Manual checks

When configured services and credentials are available:

```sh
docker compose config
docker compose build
docker compose up -d
docker compose ps
curl http://localhost:8801/healthz
curl http://localhost:8801/readyz
```

Then perform a configured provider smoke test covering:

1. Ordinary A response.
2. Streaming response.
3. Tool call and result.
4. B observation.
5. B failure.
6. Director mode.
7. Control mode.
8. Scope-gated release behavior.

The inspected research did not execute these checks; they remain required implementation verification.

---

## Security and Reliability

### Secrets

- Keep provider credentials in the existing environment/secret mechanism.
- Do not commit real keys.
- Use separate A and B credentials where possible.
- Redact authorization headers, API keys, cookies, and sensitive prompt content.
- Do not store secrets in raw event traces.
- Do not put secrets in benchmark task fixtures.
- Verify repository history before publication.

### Prompt injection

For external evidence and web designs:

- Treat fetched content as data.
- Do not execute instructions found in fetched content.
- Keep source content separate from system and developer instructions.
- Store source URL, retrieval time, and digest.
- Enforce size limits.
- Validate citations and claim spans.
- Mark retrieved-source evidence as source evidence, not automatically authoritative execution evidence.

### Input validation

Validate:

- Benchmark IDs.
- Task IDs.
- Design IDs.
- Header lengths.
- JSON schema.
- Timestamp ranges.
- Scope fields.
- Evidence IDs.
- Tenant/run/floor/attempt ownership.

Reject malformed B output safely and preserve the current degraded behavior.

### Timeouts and retries

Use the existing provider and worker timeout patterns. New benchmark code must:

- Bound every provider call.
- Bound every external fetch.
- Bound B review time.
- Avoid unbounded retry loops.
- Use idempotency keys for retried state writes.
- Distinguish timeout, provider failure, database failure, and malformed output in results.

### Safe failure behavior

- Normal asynchronous A responses should not fail solely because B is unavailable.
- A release gate may hold only when explicitly enabled.
- A held response must expose a user-visible qualified reason.
- A malformed or stale review must not be treated as approval.
- A missing receipt must not be treated as successful execution.
- A delivered streaming response cannot be retracted; record later review separately.

### Isolation

Before multi-process or horizontal deployment, verify:

- Distributed rate limiting.
- Worker lease correctness.
- Duplicate observation prevention.
- Connection-pool limits.
- Tenant quotas.
- Cross-process cooldowns.
- Cross-tenant state isolation.
- RLS behavior under benchmark and production identities.

### Data retention

Define retention for:

- Raw transcripts.
- Event traces.
- Evidence content.
- Provider responses.
- Benchmark artifacts.
- Cost metadata.

Do not retain more sensitive content than required for reproducibility and debugging.

---

## Deployment and Operations

### Required services

The current architecture uses:

- Gateway/API service.
- Worker service for B observation and jobs.
- Postgres.
- Provider APIs.
- Optional external search/web services for specific designs.

The exact service names and startup commands must follow the existing `docker-compose.yml` and deployment documentation.

### Environment configuration

The repository provides `.env.example`. Use it as the source of existing variable names. Add only variables required by the implemented feature, following the existing naming convention.

Required categories include:

- Database URL.
- A provider configuration.
- Optional B provider configuration.
- Authentication/bootstrap keys.
- Worker configuration.
- Benchmark enablement.
- Design mode.
- Release-gate mode.
- Optional external search credentials.

Do not invent provider variable names until the existing configuration module and `.env.example` have been inspected.

### Startup order

The documented Compose architecture includes an initialization step. Production startup must ensure:

1. Postgres is healthy.
2. Migrations/initialization complete.
3. Gateway starts.
4. Worker starts.
5. Readiness checks pass.
6. A provider smoke test succeeds.

### Health and readiness

Verify:

```sh
curl http://localhost:8801/healthz
curl http://localhost:8801/readyz
```

Health must not falsely report readiness when:

- Database migrations are incomplete.
- Required provider configuration is absent.
- Worker dependencies are unavailable, if worker readiness is required.

### Monitoring

Record:

- Request count by design mode.
- A latency percentiles.
- B queue depth.
- B review latency.
- Worker failures.
- Outbox retry count.
- Stale job count.
- Release holds.
- False-success benchmark failures.
- Database pool saturation.
- Provider timeout/error rates.
- Token usage and cost where available.
- Duplicate side-effect attempts.

Never log raw secrets.

### Rollback

Rollback must be possible independently for:

- Application code.
- Database migrations.
- Benchmark configurations.
- Release-gate enablement.

The recommended rollout order is:

1. Deploy schemas and code with new features disabled.
2. Enable benchmark metadata capture.
3. Run deterministic validation.
4. Enable control mode.
5. Enable advisory scope scoring.
6. Enable release gates only for selected high-risk task classes.
7. Monitor holds, false positives, and latency.
8. Roll back by disabling the gate before rolling back code if user-visible blocking is incorrect.

---

## Gaps and Unknowns

The following remain unresolved and must be explicitly decided or recorded as assumptions:

1. The exact official A model and provider are not specified.
2. The exact official B model and provider are not specified.
3. Provider credentials were not available during research.
4. No real-provider A/B conversation was executed.
5. No independent Docker build/startup was executed.
6. The benchmark target domain is unclear:
   - Text-only.
   - Software tasks.
   - Tool-use.
   - Desktop/computer-use.
   - All of the above.
7. The authoritative ground truth for open-ended reasoning tasks is not defined.
8. It is not decided whether external repositories must run directly or may be compared through compatible reimplementations.
9. GitHub repository-prefix enumeration was not independently reproduced completely; the existing repository documentation reports 24 relevant designs.
10. The exact set of accessible `dl*` repositories may change over time.
11. Many external designs have source/README evidence only.
12. No measured top-five ranking exists yet.
13. No measured false-success, concern precision, concern recall, or scope-correct completion values exist yet.
14. The exact current API route/configuration module names must be confirmed before editing.
15. The exact migration naming and schema conventions must be confirmed before creating a migration.
16. The exact provider token and cost reporting capabilities are unknown.
17. The exact distributed deployment model is unknown.
18. Retention requirements for transcripts, evidence, and benchmark traces are not specified.
19. The project owner has not confirmed whether normal mode should remain fail-open for every task class.
20. The project owner has not confirmed which actions require a release gate.
21. The `compuse` timing results are not comparable to this proxy benchmark without reproducing the same task and environment.
22. Existing software-test counts must not be used as semantic performance scores.
23. Any external web/search credentials and service limits are unknown.
24. The effect of provider contention on asynchronous A latency is unknown and must be measured.
25. The effect of database contention on “no synchronous B call” latency is unknown and must be measured.

Unverified items must remain labelled unverified in code comments, benchmark reports, and documentation.

---

## Builder Handoff

The builder must deliver:

- [ ] A pinned baseline commit and recorded environment.
- [ ] Existing tests passing before and after changes.
- [ ] An explicit single-lobe control mode.
- [ ] Tests proving the control excludes B context, memory, findings, and calls.
- [ ] A reproducible `benchmarks/` harness.
- [ ] JSONL task corpus with deterministic expected outcomes.
- [ ] Common adapter interface and adapters for control, async observer, and director modes.
- [ ] Structured acceptance criteria.
- [ ] Structured material claims.
- [ ] Evidence coverage states: `FULL`, `PARTIAL`, `UNKNOWN`, `STALE`, `CONFLICTING`.
- [ ] Explicit release decisions: `RELEASE`, `QUALIFY`, `HOLD`.
- [ ] Host execution receipts distinguished from model claims and client-reported events.
- [ ] At least the proposed hybrid designs implemented or explicitly marked unavailable.
- [ ] At least 20 `dl*` designs classified with evidence levels.
- [ ] Deterministic benchmark results for every runnable design.
- [ ] Real-provider results only where credentials and configuration are available.
- [ ] Raw event traces and normalized results.
- [ ] Control-relative quality, reliability, latency, token, and cost metrics.
- [ ] Confidence or bootstrap intervals for measured comparisons.
- [ ] A measured top-five ranking, or an explicit statement that ranking remains incomplete.
- [ ] Separate reporting for source-only, reimplemented, unavailable, deterministic, real-provider, and production evidence.
- [ ] Updated documentation that does not overstate unverified performance.
- [ ] Security review covering secrets, prompt injection, isolation, retries, timeouts, logging, and retention.
- [ ] Deployment smoke-test results, or a clear report that required credentials/services were unavailable.

When reporting completion, the builder must include:

1. The target commit.
2. Exact commands executed.
3. Exact tests passed and failed.
4. Benchmark corpus and configuration identifiers.
5. Designs actually run.
6. Designs unavailable or approximated.
7. Raw result artifact locations.
8. Top-five ranking only if empirically supported.
9. Every unresolved or unverified item.
10. Any deviation from this plan and the reason for it.
