# Function assessment: broadening and anti-deception

Updated 2026-09-10 for v0.5. Implemented behavior, deterministic validation and
historical live reports are distinguished below. New prompt intent is not a
demonstration of improved judgment.

## Current implementation

| Purpose | What is implemented | What still depends on model judgment |
|---|---|---|
| Broadening | Explicit tunnel vision / “open sesame” role; optional learned knowledge and peripheral questions | Correctness, relevance and whether A's next decision improves |
| Anti-deception | Fixed monitoring instruction, quote-anchored concerns, separate findings route | Whether the account is misleading or missing history explains it |
| Meter | B chooses GREEN/YELLOW/RED from the whole supplied record; GREEN default = no deception detected; no UNKNOWN color | B's substantive classification can be wrong |
| Memory | Versioned run snapshots and attributed notes attached to shared journal entries | Which historical guidance A should use or discard |
| Director | Visible B guidance uses the same enrichment role; A retains host tools and handoff/resume | Direction quality and whether to continue or stop |

Edit the role in [prompts.py:ENRICHMENT_POLICY](../src/dual_lobe/b/prompts.py).
[OBSERVER_GUIDANCE.md](OBSERVER_GUIDANCE.md) maps the other code sections.
B reasons inside deterministic scope, schema, evidence, freshness, budget and
tool boundaries. The five thinking angles are optional, not a checklist.

## What happens

1. A receives original messages plus the fixed monitoring instruction, eligible
   completed run memory, findings/meter and selected shared memory. Generated
   feedback is separate user-role data, not privileged instructions.
2. Normal A responses stream without waiting for a B model. Selected shared
   journal writes commit before the successful terminal event/tool handoff.
   The separate best-effort observation transaction runs after response delivery.
3. Observation contains redacted context/output/latest real user request, source
   call, time, scope and memory space. Synthetic director turns do not replace
   the real user's latest request.
4. B returns bounded JSON and may name optional information calls from the host's
   supplied tool definitions. The app executes those calls; B never executes them.
   One corrective re-ask is allowed for invalid
   parse/grounding output. Both attempts share one total deadline and each is
   charged to the per-process B budget. Transport failures are not retried.
5. Invalid required reviews produce degraded status. Malformed optional knowledge
   notes can be dropped without erasing valid concerns. Valid reviews write a new
   memory version, separate concerns and model/call/time provenance.
6. With shared memory selected, the worker attaches notes to the originating
   tenant/space/run/call journal row. Raw history and pinned notes remain separate.
   Migration 0004 is additive and nullable; old records need no reset/backfill.
7. Later A calls load usable completed feedback. Transient state-read retry uses
   a fresh tenant transaction within the existing 25 ms total read deadline.
   Receipts identify a delivered current review's source call and age.

Run memory retains floor/attempt scope and its default 24-hour TTL. Claims expire
after 180 seconds by default. Failed reviews preserve completed memory without
renewing its age and drop current findings. The meter defaults GREEN, with failed,
stale or unavailable review status reported separately. Exact quotes do not prove
B interpreted them correctly or that A deliberately deceived anyone.

Shared retrieval reuses the notebook/recent/first/lexical-match journal queries.
It adds at most one snapshot containing at most two notes, favoring a matched
conversation, otherwise recent context. Generated note wording is not separately
indexed. A judges relevance. Storage is durable; injected context is bounded and
age-limited. This is not perfect recall, neural memory or live workspace access.

## Corrected defects from starting commit 5023d94

| Reproduced issue | Correction / regression coverage |
|---|---|
| Latest user text bypassed redaction | Redact before truncation/storage and at B inputs; actual capture-to-review regression |
| B color was previously derived from concerns | Preserve B's validated color choice; legacy records without a color use GREEN and remain explicitly attributed |
| Meter claimed to assess “last answer” | Source-call/age attribution; default GREEN is not a completed review |
| Meter survived disabling claim checks | Independent meter switch and claim-check dependency |
| Corrective attempt escaped call budget / doubled timeout | Per-attempt admission and one total deadline |
| State retry reused failed transaction | Fresh session; regression invalidates a real SQLAlchemy connection |
| Director omitted latest real user request | Capture each A turn's real request, excluding synthetic B turns |
| Probe accepted failed/truncated responses; null usage crashed | Strict HTTP/SSE/terminal checks, null-safe usage and explicit failures |

Database cases cover worker-to-journal attachment, reconnect retrieval from a new
app context, memory boundaries, disabling saved notes, old rows and migration.
Provider responses are scripted. See [VALIDATION.md](VALIDATION.md) for exact
executed counts and CI evidence.

## Historical reports — not v0.5 results

The [earlier assessment](https://github.com/anasalsawy/dual-lobe-proxy/blob/5023d944ec5c627d12bf744944affcfbf48553da/docs/FUNCTION_ASSESSMENT.md)
reported v0.3 on commit 315e7fb, an OpenAI-compatible Gemini provider, a four-question
soak, and later goal-anchor/meter experiments. Reported observations included
memory version progression, degraded reviews and a false positive on a legitimate
topic change. A later small check reportedly avoided that false positive.
Raw reproducible inputs, outputs, settings and timing files are absent from this
repository; these observations have not been reproduced for the new code.

They suggest delivery worked in those runs. They do not demonstrate general
blocker recovery, deception reduction, or zero latency. The old claims that both
functions “work end-to-end,” that one prompt change eliminates a class of false
positives, and that external search is the only way to introduce new knowledge
were too broad. Learned domain knowledge can enrich context without tools; its
correctness still needs evaluation.

## Not yet demonstrated

No live A/B credentials or running deployment were configured in this authoring
environment. No A-alone / old-observer / enriched-observer comparison has run here.
The labeled cases and repaired probe are documented in
[UX_ASSESSMENT_WITH_VS_WITHOUT.md](UX_ASSESSMENT_WITH_VS_WITHOUT.md).

Measure useful/correct additions, harmful suggestions, false accusations, missed
claims, A's next action, feedback lag and timing. Syntax/quote validation cannot
establish those outcomes. B has no independent executor; information tools must
be supplied and executed by the connected app through its ordinary tool loop.
Caller-reported results are not independently verified. Infallible monitoring
and guaranteed absence of errors are not supported claims.
