# Dual-lobe design comparison

Snapshot: **2026-09-15**. This inventory scanned the 44 repositories accessible
to the linked GitHub account and identified 24 dual-lobe-related designs,
variants, host integrations, or supporting assets. It does not count unrelated
repositories merely because they use CrewAI, browser automation, or multiple
agents.

This is a repository-evidence comparison, not a claim that the designs have
equal implementation depth. A README proposal, a runnable flow, a deterministic
plumbing test, and a real-model benchmark are different evidence levels.

## Comparison criteria

Every row is compared on the same criteria:

| Criterion | Meaning |
|---|---|
| Model/provider | Models named by the repository or its example configuration; “configurable” means no fixed model is part of the design |
| Dynamics | Whether A/B are serial, asynchronous, concurrent, alternating, event-driven, or boundary-gated; and the main phases |
| A/B authority | Which lobe owns tools, side effects, judging, permission, or release |
| Context/memory | What crosses the lobe boundary and whether it is durable, bounded, or shared |
| Deception/acceptance | What is checked, and whether it is advisory, a hard gate, or absent; scope-aware acceptance is called out explicitly |
| Tests/evidence | Tests or validation actually documented in the repository; “none located” is not “failed” |
| Performance | Measured results, design-level expectations, and known cost/latency dynamics |
| Maturity | Spec, stub, runnable prototype, or implemented system |

Abbreviations in the table: `A` = primary/executor lobe, `B` = observer,
strategist, verifier, or second lobe; `async` = B is off the A request path;
`serial` = the next phase waits for the previous phase; `hard gate` = output or
action cannot pass until the gate accepts it.

## Full inventory

| Repository / design | Model/provider | Dynamics | A/B authority | Context/memory | Deception/acceptance | Tests/evidence | Performance | Maturity |
|---|---|---|---|---|---|---|---|---|
| [`dual-lobe`](https://github.com/anasalsawy/dual-lobe) — Interdependent core | Framework-agnostic; two independently configurable lobes are recommended | Persistent `proposal → permit → execute → verify`; watchdog/recovery; optional prediction-error/replay/skill modules | Strategist owns judgment, permits, verification, and escalation; Executor owns side effects; exclusive tool ownership | Typed compressed handoffs, shared workspace, append-only ledger, verified checkpoints | Hard planning/action/verification/escalation locks; completion must be reconstructible from evidence; scope-aware acceptance is specified but not implemented | `SPEC.md` lists 12 conformance tests; no runnable implementation test results in this repo | `EXPERIMENTS.md` proposes B0–B4/D0–D4 baselines and latency/tokens/cost metrics; no benchmark result | Research specification |
| [`dual-lobe-proxy`](https://github.com/anasalsawy/dual-lobe-proxy) — normal observer | A/B endpoints and models configurable; may share a provider | A streams immediately; B reviews after delivery; next A call receives the last usable state; no B wait | A owns answer/tool request; B has no executor authority; host runs tools; gateway can perform only bounded evidence sensing | Postgres run-scoped versioned memory, separate claim findings, TTL/floor/attempt scoping | Advisory `UNSUPPORTED`/`CONTRADICTION`/`SUSPICIOUS_SHIFT`, meter, monitoring reminder; no pre-delivery acceptance gate | Recorded CI: 131 scripted cases passed at v0.4; current local unit run: 139 passed, 5 ambient-SOCKS-precondition failures; no semantic benchmark | Normal path adds state I/O/prompt tokens but no B model wait; recorded docs have live soak, not controlled quality/latency comparison | Implemented development proxy |
| [`dual-lobe-proxy`](https://github.com/anasalsawy/dual-lobe-proxy) — director mode | Same configurable A/B provider | `A answer → B decision → A continuation`; serial, visible, bounded by calls/time/tokens/state | B directs conversationally but does not execute tools; host retains tool authority; tool results must match IDs | Durable director transcript plus shared memory; accepted turns and tool handoffs persist | B can question/correct/stop, but stop is not proof of success; no final acceptance verifier | Same proxy CI covers director, history, tool handoff, cancellation, and persistence; replies are scripted | Explicit extra A/B calls and serial latency/cost; no controlled benchmark | Implemented opt-in mode |
| [`IntentGuard`](https://github.com/anasalsawy/IntentGuard) — live supervisor | Host executor plus configured supervisor model; provider not fixed | Event-driven `observe → evaluate → allow/block/intervene → observe`; same-session correction loop | Supervisor receives independent runtime/tool/environment telemetry and can block/intervene; executor is not its own witness | Canonical intent revision, trajectory, events, artifacts, errors, interventions, status | Targets fabrication, hidden failure, scope drift, unauthorized side effects, and deviation; intended hard intervention, not merely final grading | Runnable `mvp_supervisor.py` and deterministic root-delete block; no repository test suite located | Model decision per event can add continuous overhead; 30 s supervisor request timeout in MVP; no measured benchmark | MVP plus architecture/spec |
| [`forge`](https://github.com/anasalsawy/forge) — dual-lobe gateway | Worker defaults to SambaNova Meta-Llama-3.3-70B; B/provider configurable; analysts remain direct | B pre-pass starts concurrently with A; A returns; B post-pass audits and updates state for a later call; floors remain serial | Worker/A executes; B shadows and injects distilled state; analysts/final auditor remain explicit quality gates | `.dual_lobe/state.json` plus cumulative floor handoffs | B checks unsupported completion and evidence integrity, but injected context is advisory; floor/final gates are separate | Launcher and runtime source present; no repository test suite or measured run reported | Concurrency hides the pre-pass from A latency; B post-pass and provider contention still consume resources; no numbers | Runnable integration prototype |
| [`forge2`](https://github.com/anasalsawy/forge2) — adjacent governed pipeline | Worker and Reviewer must use different model connections; exact models not fixed | Six cumulative research floors and six build floors; `Worker → Analyst → correction`; human checkpoint; final auditor; serial | Reviewer cannot edit; exact verdicts and human approval control progression; not labelled as a dual-lobe proxy | Crash-safe JSON state plus append-only JSONL events and cumulative handoffs | Exact PASS/CORRECTION/APPROVE/REJECT/RERUN contracts; final PASS/FAIL; full-scope semantics; not live per-event supervision | Four test files and CI commands documented; no performance result in README | High serial floor/correction latency by design; no benchmark | Runnable adjacent architecture, not a dual-lobe implementation |
| [`dl-vault-proof`](https://github.com/anasalsawy/dl-vault-proof) — proof-gate variant | Base proxy provider configuration | Base async observer; non-stream buffered response can be held when proof is absent | A remains executor/answerer; gateway adds a proof hold; B remains advisory | Base proxy Postgres memory/claims/evidence paths | `_proof_hold()` withholds bare completion/action claims and returns `[proof-required]`; lexical heuristic, not scope-aware semantic proof | Base proxy test tree copied; no separately reported variant benchmark | Can add turns/friction and false holds; no measured result | Runnable variant of base proxy |
| [`dl-fact-verify`](https://github.com/anasalsawy/dl-fact-verify) — verify-before-release | CrewAI flow; model not fixed in flow | Serial `Answerer draft → Verifier → RELEASE/HOLD`; no execution loop | Answerer fetches/cites; Verifier releases or holds | In-flow JSON state: prompt, answer, verdict; no durable memory described | Hard release for current facts; every specific fact is expected to have a source; no general task-scope coverage | Flow and README only; no test directory or execution result located | Two serial model calls; extra verification latency/cost proposed, not measured | Small runnable flow / experiment |
| [`dl-interlocked`](https://github.com/anasalsawy/dl-interlocked) — permission-lock core | CrewAI flow; model not fixed in flow | Serial `plan → permit → execute → verify`; retry on failed verification is implied | Executor proposes/acts; Strategist permits and independently verifies; action and completion are gated | In-flow plan/action/evidence fields; no durable store described | Hard `PERMIT/REVISE` and `SUCCESS/RETRY`; evidence before completion; scope semantics not formalized | Flow and README only; no test directory or execution result located | Four serial agent phases; verifier bottleneck and false blocks possible; no measurement | Small runnable flow / experiment |
| [`dl-broadener`](https://github.com/anasalsawy/dl-broadener) — aggressive broadening | Base proxy provider configuration | Base async observer; B biases its single review toward alternatives and prerequisites | A executes/answers; B advises and grades; no tool or release authority | Base proxy versioned Postgres memory; context notes kept separate from concerns | Broadening plus advisory deception meter; does not hard-block or compute claim coverage | Base proxy test tree copied; variant-specific live accuracy not reported | Same hot-path behavior as base plus broader B prompt; possible context noise; no benchmark | Runnable prompt variant |
| [`dl-consensus`](https://github.com/anasalsawy/dl-consensus) — cross-family agreement | A/judge: Gemini 3.1 Flash Lite; B: Moonshot/Kimi K3 | Serial `A answer → independent B answer → consensus judge`; disagreement surfaced | Both answer; judge releases/reconciles; no tool executor boundary | In-flow answer A/B/final fields; no durable memory described | Agreement is the release condition; disagreement is visible; agreement is not independent truth proof; no scope coverage | Flow/README only; no tests or measured result located | Three serial model calls and likely high latency/cost; proposed decorrelation benefit, unmeasured | Small runnable flow / experiment |
| [`dl-web-verifier`](https://github.com/anasalsawy/dl-web-verifier) — live web verifier | Example config uses `gpt-4o`; provider configurable | B requests `fetch_web`/`search`; gateway fetches; B re-reviews; intended off A path | A answers; gateway executes bounded web lookup; B grades | Base proxy context plus sensed web material | Focused current-fact grounding; claims without evidence can be RED; no full acceptance/scope gate | Source/tool files and Docker setup; no test directory or result located | Network fetch and re-review cost; request path is intended to stay nonblocking; no benchmark | Runnable integration variant |
| [`dl-search-broadener`](https://github.com/anasalsawy/dl-search-broadener) — search broadener | Example config uses `gpt-4o`; provider configurable | B performs primary/alternative/prerequisite/recent searches, then injects notes; background design | B searches/advises; A remains executor/answerer | Base proxy context plus search notes | Broadening is primary; deception secondary; search results are not proof; no scope gate | Source/tool files and Docker setup; no test directory or result located | Multiple searches per review increase network/token cost; intended off-path; no benchmark | Runnable integration variant |
| [`dl-adversarial-check`](https://github.com/anasalsawy/dl-adversarial-check) — adversarial hunter | Example config uses `gpt-4o`; provider configurable | B extracts claims, searches pro/con/edge cases, compiles, then rates | B researches and challenges; no side-effect authority or hard action lock described | Base-style prompt plus pro/con/edge evidence | Adversarial GREEN/YELLOW/RED; better falsification pressure, but still model judgment and no formal scope coverage | Source/tool files and Docker setup; no test directory or result located | Three searches per claim are explicitly configured; high cost/latency risk; no measurement | Runnable integration variant |
| [`dl-multimodal-deep`](https://github.com/anasalsawy/dl-multimodal-deep) — cross-source grounding | Example config uses `gpt-4o`; provider configurable | B checks Wikipedia, web, structured APIs, and docs, then compares sources | B gathers/compares; A answers; no action gate described | Base-style context plus multi-source findings | HIGH/MEDIUM/LOW consistency and RED on fundamental disagreement; source agreement is not proof of task completion or scope | Source/tool files and Docker setup; no test directory or result located | Several network sources and reconciliation steps; high cost/latency risk; no measurement | Runnable integration variant |
| [`dl-loop-fixed`](https://github.com/anasalsawy/dl-loop-fixed) — fixed roles | A/B both Moonshot/Kimi K3 via NVIDIA NIM | A executes; B reviews continuously/selectively in the same CrewAI round; claimed advisory loop | A owns real host tools; B has read/research tools; B never blocks | Shared state/memory described; no durable mechanism detailed | Advisory deception grade and context broadening; no hard gate | `flow.json` and README only; no test directory or result located | “Continuous” is a design description; one CrewAI flow block does not establish real concurrent execution; no measurement | Small runnable flow / experiment |
| [`dl-loop-flip`](https://github.com/anasalsawy/dl-loop-flip) — alternating roles | A/B both Moonshot/Kimi K3 via NVIDIA NIM | Serial turn 1 A executes/B observes, turn 2 B executes/A observes | Tool ownership flips by turn; advisory only; no permanent independent verifier | Shared memory described; no durable mechanism detailed | Advisory deception only; intended to reduce correlated errors by alternation; no hard acceptance gate | `flow.json` and README only; no test directory or result located | Two role-switching rounds add serial latency; decorrelation is a hypothesis, not measured | Small runnable flow / experiment |
| [`dl-loop-state-gated`](https://github.com/anasalsawy/dl-loop-state-gated) — boundary gate | A/B both Moonshot/Kimi K3 via NVIDIA NIM | A executes until deliverable/evidence boundary; B verifies; control returns to A | A executes; B verifies and permits continuation; neither grants tools | Shared dispatcher/registry described; no durable mechanism detailed | Evidence gate at major boundary; stronger than advisory, but no formal claim-scope coverage | `flow.json` and README only; no test directory or result located | Lower review frequency than every-step review, but each gate is serial; no measurement | Small runnable flow / experiment |
| [`dl-core-dual`](https://github.com/anasalsawy/dl-core-dual) — proof-gate copy/variant | Base proxy provider configuration | Base async observer plus proof-gate behavior described in `VARIANT.md` | A is answerer; gateway can hold bare proofless completion; B advises | Base proxy Postgres memory/claims/evidence | Same proof-gate heuristic as `dl-vault-proof`; repository metadata calls it a `dl-vault-proof` variant; no scope-aware gate | Base proxy test tree copied; no separately reported variant benchmark | Same proof-gate friction/false-hold trade-off; no measurement | Runnable variant, naming/lineage needs cleanup |
| [`dl-scout-verify`](https://github.com/anasalsawy/dl-scout-verify) — intent + research + proof | Base proxy provider configuration; optional CrewAI preloader | A never waits for B; background scout/fact-check/evidence sensing; next-call injection | A answers; B scouts/verifies; gateway performs bounded fetch/read; proof rule can hold buffered claims | Base shared memory plus `intent`, `freshness`, evidence snapshot | Broadening, proof-required, fact checking, deception meter; still reports material as non-authoritative and lacks full scope gate | Base proxy test tree copied; `VARIANT.md` describes extensions but no separately reported semantic benchmark | Claims zero added A latency via 25 ms state read; this is an architecture claim, not a controlled performance result; B work lands later | Runnable ambitious variant |
| [`dl-continuous-audit`](https://github.com/anasalsawy/dl-continuous-audit) — audit concept | Not specified | Intended persistent cross-turn audit and goal-drift detection; dynamics not implemented in README | Authority not specified | Durable audit log implied | Contradiction and goal-drift detection named; criteria/protocol not supplied | README only; no tests or implementation evidence located | No performance evidence | Concept stub |
| [`dl-tools-lib`](https://github.com/anasalsawy/dl-tools-lib) — shared B tool substrate | No model; utility library | Async web fetch, search, consistency check, evidence linking, orchestration; batch/concurrency controls | Tools can be called by variants; library itself has no A/B authority or release gate | In-memory cache/TTL plus evidence chains and source metadata | Links evidence to claims, confidence/gaps, cross-source consistency; does not decide task acceptance | Source modules/README; no test directory or benchmark result located | Configurable rate limits, caching, size limits, concurrency; no measured throughput/latency | Supporting library, not a dual-lobe design |
| [`compuse`](https://github.com/anasalsawy/compuse) — Tasker predictive loop | OpenAI-compatible vision model from environment; user session configured Gemini Flash Lite, while repo stays model-neutral | A executes current desktop batch while predicting next; B prepares/checks handoff; predictive and screen-aware modes; separate parallel-split prototype | Coordinator serializes physical input; B can gate/replan; host/desktop adapter owns side effects | SQLite journal, observation anchors, predicted endpoint, B profile memory | Gatekeeper/recovery profiles, proof requirements, screen anchors, replan on mismatch; parallel split requires isolated surfaces; not a general acceptance verifier | Six test files; user-provided deterministic shell proof showed 5 actions, `CONTINUOUS_HANDOFF=PASS`, comparison `PASS` | User-provided proof: predictive 332.1 ms vs screen-aware 315.2 ms; deterministic plumbing only, not real desktop or semantic model performance | Runnable Windows vertical slice |
| [`dialogue-os`](https://github.com/anasalsawy/dialogue-os) — hosted Dual-Lobe research track | Framework-agnostic | Same Strategist/Executor core as `dual-lobe`; Dialogue OS supplies organization/governance around it | Dual-Lobe owns task-unit locks; Dialogue OS owns organization-level governance | Constitutional organization state plus Dual-Lobe typed handoffs/ledger/checkpoints in the research spec | Same hard-lock/evidence-before-success hypothesis; no implemented scope gate | Canonical docs and proposed experiments; no runnable dual-lobe test result in this repo | Research plan calls for success, false-success, safety, recovery, tokens, latency, and ablations; no results | Host/specification relationship, not a separate runtime |

## What the comparison actually shows

### Strongest reliability boundary

The original [`dual-lobe` core](https://github.com/anasalsawy/dual-lobe) and
`dl-interlocked` have the clearest authority separation: side effects, permission,
and verification are owned by different roles. `dl-fact-verify` and
`dl-vault-proof` add narrower release surfaces. These designs pay in serial
latency and can falsely block legitimate work.

### Strongest continuous/nonblocking behavior

[`dual-lobe-proxy`](https://github.com/anasalsawy/dual-lobe-proxy), `forge`, and
`dl-scout-verify` keep B off the normal A response path. That improves
responsiveness but means a bad answer can be delivered before B reacts. Their
receipts and later injections are not equivalent to pre-delivery verification.

### Strongest runtime awareness

`IntentGuard` has the most explicit independent observation-plane design because
it consumes runtime/tool/environment events and can intervene in the same session.
It is still adapter-dependent: without native runtime telemetry, it falls back
to model-level supervision and must not claim ground-truth verification.

### Strongest context broadening

`dl-broadener`, `dl-search-broadener`, and `dl-scout-verify` put the most weight on
finding the goal behind a narrowed tactic. Search increases freshness and breadth,
but also adds source-quality, prompt-injection, cost, and relevance risks.

### Strongest test evidence currently visible

`dual-lobe-proxy` has the most substantial recorded software validation, including
CI coverage against scripted providers and database workflows. `compuse` has a
deterministic shell comparison and a user-observed timing result. Neither result
is a controlled measurement of model honesty, deception detection, or real-world
computer-use success.

### The important missing capability across the family

Most variants check one of these narrower properties:

- the claim has a citation;
- two answers agree;
- a tool result exists;
- a source contradicts a claim;
- a proof token appears;
- a gate returned `PASS` or `SUCCESS`.

The missing common layer is **scope-aware acceptance coverage**:

```text
user acceptance criteria
→ normalized claim scope
→ evidence scope and freshness
→ FULL / PARTIAL / UNKNOWN / STALE / CONFLICTING coverage
→ only then a completion claim
```

Therefore, none of the rows should be described as solving the exact failure mode
where an isolated or narrow demonstration is presented as proof of the user's
broader goal unless it has an explicit acceptance/evidence gate for that scope.
The required regression remains:

> A fixture pass may support “the fixture passed,” but not “the intended
> same-computer command splitter passed.”

## Not counted as dual-lobe designs

The accessible account also contains `browser-use-bridge`, `builder-agent`,
`6forge`, `crewai`, `cua`, `BrowserUse`, `Vapi`, travel apps, and other projects.
They may provide useful host tools or agent infrastructure, but the repository
evidence inspected here does not define a dual-lobe architecture for them.

`dialogue-os-runtime` is an operational runtime for Dialogue OS; its README does
not define a separate Dual-Lobe runtime. `dl-tools-lib` and
`dl-continuous-audit` are included above only so the inventory does not hide
supporting or incomplete pieces.

## Recommended comparison protocol

To turn this inventory into a real ranking, use the same task set and model
budget for at least these control groups:

| Control | Purpose |
|---|---|
| Single A | Baseline generation/execution behavior |
| A + reminder | Measures awareness wording alone |
| Async observer | Measures nonblocking B broadening/claim review |
| Proof gate | Measures hard proof friction |
| Interlocked gate | Measures permit/verify reliability and serial cost |
| Runtime supervisor | Measures independent telemetry and intervention |
| Predictive Tasker | Measures overlap/handoff dynamics on desktop tasks |

For every run record task success, **scope-correct completion rate**,
false-success rate, missed concerns, false positives, evidence coverage, recovery,
duplicate side effects, user interventions, wall-clock latency, model tokens/cost,
tool calls, and observer coverage. Do not call a deterministic plumbing pass a
performance win or a deception reduction.
