# Circadian Subconscious Memory Cycle (CSMC)

The proxy's slice of the nightly loop: capture the raw juice during waking
hours, export it verbatim at night, and forget it after the weights have
absorbed it. The proxy owns Phases 1-2 completely and Phase 3-4 up to the
edge of its perimeter: it produces the training file and enforces the purge
discipline, but the fine-tuning itself (LoRA/QLoRA, generative-replay mix,
weight merge) belongs to the runtime on the robot, behind one operator
command.

## The invariant this exists to protect

Raw data is the asset. No code path may summarize, normalize, correct, or
otherwise rewrite a stored transcript - the stutters, slang, casing and
typos are exactly what the weights are supposed to absorb. Storage is
therefore binary in practice: a turn is kept byte-exact or not kept at all.
The low-variance turns the filter drops are gone because the policy is not
to keep them, never because anything rewrote them into something else.
The one sanctioned rewrite is post-bake lossy compression, structured so
it can only touch text the weights already consumed, never overwrite the
verbatim column, and never leave the machine's perimeter.

## The two memory systems, and why they coexist

The episodic notes (C's curated, verbatim-quoted notes; see
EPISODIC_MEMORY.md) are **what the agent can recall and say**. The ND-SMC
buffer is **what the agent becomes** and can never recall. They share a
source (the turn) and share nothing else: different tables, different
lifetimes, different consumers. The purge of one cannot touch the other -
notes are not raw turns, and buffer rows are not notes. A user who asks
what the agent remembers gets notes; the buffer is invisible even to A.

## Phase 1-2: waking ingestion (`state/ndsmc.py: ingest`)

Runs at the end of every turn beside C's note capture, on the human's own
message only (never the conversation JSON, which would re-buffer history
every turn). On the secure model the turn is vault-tokenized before it
touches the buffer - the privacy gate is inherited, not bypassed.

The juice filter scores each turn 0-1 (`b/subconscious.py:
emotional_variance`) from three measured ingredients:

- **Novelty (0.45)** - 1 - cosine against the space's *subconscious
  baseline*: an EMA of ordinary interaction stored in
  `memory_spaces.subconscious`. "Boring" is this user's ordinary, not a
  universal prior. Kept peaks update the baseline at alpha/4 so one
  dramatic day does not redefine normal.
- **Style (0.35)** - deterministic surface markers (`style_markers`):
  hesitant/stutter patterns, halting ellipses, shouting caps, filler-heavy
  informality, guarded hedging, absolutes, coarse register, unusual
  vocabulary. Markers label the text; they never replace it.
- **Affect prior (0.20)** - C's own read for the turn (kind/salience/chord
  from the note pass), so the filter is C's judgment plus arithmetic -
  no extra LLM call, no extra latency.

Turns under `ndsmc_variance_floor` (0.60) are never written. With the default
hash embedder novelty saturates high for any rephrased small talk, so the floor
sits where a style marker or a C note is required to pass - purity over yield,
because the buffer trains weights and junk training data costs more than it
adds. The buffer is
hard-capped per space (`ndsmc_buffer_rows`, default 2000); over-cap, oldest
unlocked rows fall first. `ingest` swallows every failure: the buffer is an
optimization over forgetting, never a request-path dependency.

## Phase 3-4: the dream (retain), the squeeze, and the rim

The dock controller calls `POST /v1/dual-lobe/memory/{space}/consolidate`
(state:debug scope) when the charge-state condition is met; the proxy is
stateless about docks and never schedules anything itself. Occupancy for
the dock's decisions: `GET .../buffer` returns rows, utilization, per-tier
counts (`raw`, `baked_raw`, `compressed`) and `bytes_held`.

**Consolidation is non-destructive.** Order: export verbatim -> lock ->
optional train -> bake. On a successful (or absent) training step every
exported row is marked baked: the weights have eaten it, the export file
holds the verbatim copy forever, and the DB row persists. A failed training
run aborts the bake, and the rows stay raw-and-unexported for tomorrow.
Nothing in `consolidate` deletes anything.

**Tiered compression** (`compress_space`, run at the tail of consolidation
or standalone): a baked row's transcript is distilled to minimum text by an
optional *local* model behind `DUAL_LOBE_NDSMC_COMPRESSOR_COMMAND` (JSONL
`{"id","text"}` in, smaller JSONL out - e.g. a small quantized local LLM).
The lossy text goes to `compressed_transcript`, never back into
`raw_transcript`, and the raw payload is released (`raw_transcript=""`,
`raw_chars=0`) so a compressed row can never masquerade as verbatim or
re-enter a training export as training data. Compression requires baked;
baked requires exported; exported is verbatim. The distillation chain can
therefore only ever shorten what the weights already consumed. No
compressor configured is a valid state: the bowl simply stays heavier,
longer. (Sending user text to an external API for distillation is out of
policy - same reason the vault exists.)

**The overflowing bowl** (`evict_overflow`, also callable standalone as
`POST .../evict`, and checked on every ingest): the bowl has a capacity
(`DUAL_LOBE_NDSMC_BUFFER_ROWS`) and a steady line (`_BOWL_RATIO` of it,
default 0.9). While occupancy sits under capacity, nothing is ever deleted.
Past capacity, the oldest *compressed* rows are deleted FIFO back to the
steady line - hysteresis, so eviction happens in rare passes, not every
turn. Raw rows are never evicted for space, however full the bowl gets:
the only door to amnesia is compression first. True amnesia happens when
the bowl literally overflows, oldest-lossy-fragment-first - the verbatim
tier is never deleted for space at all, and the export files hold every
word regardless.

Being unplugged mid-dream is answered by construction: interruption before
the bake means nothing was compressed or lost, and the next night re-runs
the cycle over still-unexported rows. (An interrupted *weight merge*
remains the operator pipeline's atomicity problem - train into a staging
adapter, swap atomically - which is why training is an opaque command with
an exit code rather than proxy-internal logic.)

## The export, at a glance

Every line is one verbatim row the model trains on - compressed rows never
appear here:

```json
{"schema": "ndsmc-raw/2", "timestamp": "2026-10-03T21:58:04.112345+00:00",
 "raw_transcript": "D-dude I can't... I can't stop thinking about it",
 "speaker_style_vectors": ["hesitant", "halting", "informal"],
 "emotional_variance_score": 0.78}
```

`schema` is versioned because the bowl changes what the file guarantees:
v1 lines were "everything ever exported, then deleted"; v2 lines are the
verbatim tier only, appended nightly, never rewritten - permanent either
way, since the file was always the durable copy.

## What deliberately stays out of the proxy

- **Embedding-as-prompt-injection.** The spec's `[Context Embedding:
  Vector_A]` prefix assumes the model was fine-tuned to read vectors, and
  the proxy is an OpenAI-compatible endpoint fronting arbitrary upstreams -
  it cannot inject foreign tokens into models it does not own. The
  runtime that does the nightly LoRA owns this; the proxy stores the
  per-turn sensory vector (`sensory_vector`, the same in-perimeter embedder
  as the paraphrase lane) beside every buffer row so the training pipeline
  gets `(transcript, vector)` pairs for free.
- **Confabulation, drift, ghost habits.** Emergent behavior of trained
  weights, not features; no proxy code asserts them, and A's persona is the
  only place anything about them could ever be worded.
- **Variance by semantic distance.** Same honest limit as the paraphrase
  lane: the default hash embedder's novelty term is lexical. The baseline
  math is embedder-agnostic - swapping in a local semantic model through
  `set_embedder()` makes novelty genuinely semantic, no schema change.

## Verbatim rule vs. the ledger

Separate decision, same document because they interact: with
`DUAL_LOBE_C_SOLE_MEMORY_CHANNEL=true` (default) A's context receives only
C's notes/callbacks - no journal excerpts. The mechanical journal keeps
recording every turn byte-for-byte as C's raw material and the operator's
audit; it is simply no longer A's memory. Flip the flag to restore the old
dual-channel injection. B is unaffected either way: B's evidence always
comes from the live turn and pinned notebook, and C's notes are opinion to
B, never evidence.