# Function Assessment: Context Broadening and Anti-Deception

Live assessment of dual-lobe v0.3 (commit `315e7fb`) against its two intended
functions. Grounded in code and a live soak test against a real provider
(Gemini via OpenAI-compatible endpoint). Test date: 2026-09-10.

## Baseline

- Offline suite: **67 passed** (`uv run --locked pytest tests/unit -q`).
- Postgres integration: **5 passed** (`tests/test_worker.py`, `tests/test_schema_rls.py`)
  via testcontainers on this host (daemon access that was blocked on the authoring
  machine).
- Live stack: compose up on `127.0.0.1:8801`; gateway, b-worker, Postgres healthy.
- Soak run: `dual_lobe.client --run soak-test-021`, four unrelated questions, `/state`
  inspected after each.

## 1. Context broadening

### Implementation

Persistent broadening memory produced by an asynchronous observer, delivered as a
forced pre-call read.

1. **Observe** — after A's stream completes, the gateway enqueues an outbox job
   (`src/dual_lobe/api/chat.py:_persist_observation`) capturing the conversation to
   that point, A's response, run/floor/attempt ids, and `observed_at`.
2. **Independent worker** (`src/dual_lobe/b/worker.py`, poll 1 s, concurrency 2) picks
   the job and gates it in `src/dual_lobe/b/context_shadow.py:run_shadow_cycle`:
   enabled flags, 180 s observation-age TTL, not superseded, cooldown, RPM budget.
   Ineligible jobs are recorded as `shadow_skipped`, never replayed.
3. **B review** — tool-free, temperature 0, one attempt. It reconstructs the original
   goal before tactics ("open sesame" / missed-prerequisite framing), then returns the
   JSON contract `goal / questions (<=2) / next_step / context_notes (<=2)`
   (`src/dual_lobe/b/prompts.py:CYCLE_PROMPT`). Prompt bounded at 18 KB via
   `head_tail` budgeting.
4. **Store** — a **versioned ContextMemory** (version+1, scoped to floor+attempt,
   `source_call` recorded) is written under `b_state.payload.context_memory` in tenant
   Postgres (`src/dual_lobe/b/channels.py`), independent of claim findings.
5. **Deliver** — before each eligible A call the gateway performs the best-effort
   25 ms state read (`src/dual_lobe/b/channels.py:prepare_context`, no waiting on B)
   and injects memory as a low-privilege **user-role** `observer_memory` message placed
   after any system/developer messages (`chat.py:_effective_messages`). Only the fixed
   monitoring instruction is privileged (system/developer role).
6. **Lifetimes** — memory TTL 24 h, age measured from `observed_at`, never renewed by a
   failed review. Expired or scope-mismatched memory is excluded even if a newer review
   failed. Failed reviews preserve the previous completed memory verbatim (`degraded`).

### Soak evidence

- Receipts across four turns: `memory=none → v1 → v2 → v3` — each request loaded the
  completed snapshot of the prior work.
- B state revisions 1-4 all `oversight_status=reviewed`, `memory_status=available`,
  with coherent goal/questions/next_step/context_notes in `context_memory`.
- One review degraded (`ValidationError`): previous memory preserved, claim delivery
  dropped (`fail-open`), matching the design. The next review recovered.

## 2. Anti-deception

### Implementation

Two independent channels: post-hoc **claim flagging** plus in-band **observation
awareness**. The proxy never blocks, holds, or corrects A mid-stream.

**Flagging ungrounded claims**

- Same single B review, Job 2: returns `concerns[]` with signal ∈
  `UNSUPPORTED | CONTRADICTION | SUSPICIOUS_SHIFT`, exact `claim_quote` (from output),
  `basis_quote` (from context/events, empty only for UNSUPPORTED), reason, suggestion.
  Max 3 concerns, bounded lengths.
- The instruction set hard-bounds judgment: missing evidence = UNSUPPORTED, never
  false; a tool request is not an execution result; an assistant-written receipt is not
  independent evidence; client events and tool messages are caller-reported; never
  `VERIFIED/PASS/FAIL/"lying"`; no holds or execution preconditions; prefer no concern
  to a stylistic accusation.
- Stored as `claim_review` in the same snapshot, **separate field** from memory, with its
  own shorter 180 s observation-age TTL. A no-concern review clears prior findings, so an
  old accusation cannot replay indefinitely. Degradation drops claims, fails open.

**Making A aware of observation and judgment**

- Fixed `OBSERVATION_REMINDER` (`src/dual_lobe/b/prompts.py`) injected in the
  system/developer position on every eligible request (`chat.py:_effective_messages`).
- It states: A is behind an inference proxy with a background observer whose findings can
  reach subsequent calls; address the user normally; distinguish intended, attempted,
  observed and **confirmed** work; do not invent execution, tests, citations or
  completion; support completion claims with execution evidence; correct earlier claims
  when newer evidence contradicts them; observer memory and findings are fallible
  context, not user instructions.
- Delivery receipts via response headers (`x-dual-lobe-memory`, `x-dual-lobe-claims`,
  `x-dual-lobe-monitoring`) make the request composition inspectable per call.

### Soak evidence

- `monitoring=on` on every request; memory and claims deliveries matched the stored state.
- One claim was flagged: `SUSPICIOUS_SHIFT` on the PostgreSQL question, delivered as
  `claims=available` on the following request, then cleared (`claims=none`) after the
  no-concern review of the next turn — the 180 s TTL / clear-on-review path exercised.
- One `degraded` (ValidationError) fail-open confirmed the resilience path.

## Findings and recommendation

1. **Both functions work end-to-end** — observation, versioned broadening memory, claim
   flagging with per-channel TTLs, fail-open degradation, and the monitoring instruction
   were all observed live.
2. **Judgment quality caveat** — the only flagged claim was a **false positive**: B
   treated a legitimate topic change as `SUSPICIOUS_SHIFT`. Root cause: memory content is
   anchored to the *reviewed* interaction (N-1), so the stored goal was still one user
   message behind. On rapid turns B can be one call behind by design, and the review
   writes the goal of the interaction it just observed.
3. **Recommended (not applied):** anchor B's goal baseline to the **latest in-conversation
   user message** rather than the observed interaction's framing, which should eliminate
   this class of false alarms. Changes would be confined to `context_shadow.py`, the
   prompt contract in `prompts.py` (docs), and the unit tests.
4. Intent recognition caveat: B's evidence is the supplied conversation, tool results and
   reported events alone. Shared-filesystem inspection or other independent verification
   remains a future feature.

## Recorded limitations

- This assessment does not measure production latency, monetary cost, or A's actual
  compliance with the monitoring instruction (receipts report composition, not compliance).
- The soak used an OpenAI-compatible Gemini endpoint; claim-check accuracy was not scored
  against a labeled dataset.
## When does the proxy write to memory?

Memory is written **only after A has fully answered**; never mid-stream, never before a
response is delivered.

1. A streams its answer. The proxy collects the completed message and an audit
   (`worker_call` event with latency and an excerpt). When the response is **fully
   delivered**, a `BackgroundTask` runs `_persist_observation`
   (`src/dual_lobe/api/chat.py`).
2. It enqueues a **transactional outbox job** (`b/outbox.py`) carrying `context_text`,
   `response_text`, `observed_at`, floor/attempt and `source_call`. The job key is
   deterministic (hash of tenant/run/call/context/response), so retries and redeploys
   never double-enqueue.
3. The **b-worker** polls the outbox every `worker_poll_seconds` (default 1 s), grabs the
   job under a per-run lock, and runs `run_shadow_cycle`
   (`src/dual_lobe/b/context_shadow.py`).
4. **Skip gates** — a miss produces a `shadow_skipped` event and **no** memory write:
   - observation disabled, or both channels off;
   - job older than `b_state_ttl_seconds` (180 s) since `observed_at`;
   - superseded (a newer `observed_at` is already stored);
   - within the B cooldown (default 0);
   - over the B RPM budget.
5. If gates pass: one B call (temperature 0, timeout `b_timeout`, no tools). On success
   `reviewed_state` writes **ContextMemory version +1** (observed_at = the interaction
   timestamp, updated_at = now, source_call, floor/attempt) via `save_b_state`; events
   `context_memory_updated` and `b_context_shadow` are appended (`oversight` if concerns).
6. On B failure/invalid output it **still writes** a `degraded` state row, but preserves
   the previous memory **verbatim** (version and age untouched, claims nulled) — a failed
   review never renews memory.

Measured timing (ux run, `observe-delay=12 s`): reviews landed ~13 s apart, each roughly
1 s after the A reply finished. With no pause between turns, B lags one to two reviews
behind; the delivered content is always the last completed usable snapshot.

## Does it look up information / enrich context?

**No — it never looks up anything external.** B's system prompt is explicit: it has no
filesystem, browser, hidden reasoning, or independent factual oracle. There is no web
search, RAG, tool call, or cross-conversation retrieval, and it cannot query facts from
other runs' databases. Any factual content B contributes comes from the model's own
knowledge and is treated as fallible, not evidence.

What exists instead is a **memory-and-framing** layer over the current run only:

1. It reads the conversation to that point, A's answer, and recorded events for this run
   (budget ~18 KB, keeping the mission head and the latest failure tail).
2. It **consolidates** that into a compact snapshot: original goal, ≤2 questions, one next
   step, ≤2 context notes (missed prerequisites, alternate explanations, relevant supplied
   info).
3. It **persists** it as versioned memory (24 h TTL) scoped to run/floor/attempt/tenant,
   and **feeds it forward** — re-injecting the last completed snapshot into the next A call
   as `observer_memory` (plus short-lived `observer_claims`, 180 s).

So "enrichment" = distill-from-record + retain + re-inject, within one conversation.
External factual enrichment (searching the web, docs, or the user's codebase, then
injecting the findings) would be a new feature; it is deliberately absent in v0.3.

## Suggested improvements

Recommendations from the live assessment, not yet implemented.

**P1**
- **B JSON robustness.** The observer degraded on 3 of 6 reviews in the ux run
  (`ValidationError` — strict schema rejection of B's output). Add tolerant extraction
  (strip code fences / locate the outermost JSON object), a single corrective retry that
  passes the parse error back to B, and log the parse-error excerpt to see what B actually
  returns. No extra model debate; B stays tool-free.
- **Goal-anchor fix.** Anchor B's `goal` baseline to the **latest in-conversation user
  message**, not the reviewed (N-1) interaction. This removes the false-positive
  `SUSPICIOUS_SHIFT` class seen in the soak (a legitimate topic change was flagged because
  the stored goal was one message behind).
- **State-read retry.** Turn 3 of the ux run hit `memory=unavailable` from one transient
  25 ms state read. One retry inside the deadline, with reason logging, improves injection
  continuity while staying fail-open.

**P2**
- **Labeled eval for claim checks.** Unit tests verify the schema, not the accuracy of
  `UNSUPPORTED / CONTRADICTION / SUSPICIOUS_SHIFT`. Add a small labeled evaluation set and
  score precision/recall.
- **Compliance measurement.** Receipts report request *composition*, not A's obedience.
  Add a check that A never references the observer and corrects contradicted claims when
  the reminder is present.
- **Telemetry.** Expose memory age/version and the review-lag in receipts; log B call
  latency and outbox queue depth (OTEL is present but off).

**P3**
- **Optional external enrichment.** A scoped, monitored lookup channel (web/docs/codebase
  search) behind an opt-in flag, since v0.3 intentionally has none. This is the only way to
  go beyond the current "distill the record" memory model.
