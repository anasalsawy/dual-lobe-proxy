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

## Phase 3-4: the dream (`state/ndsmc.py: consolidate`)

The dock controller calls `POST /v1/dual-lobe/memory/{space}/consolidate`
(state:debug scope) when the charge-state condition is met; the proxy is
stateless about docks and never schedules anything itself. Buffer occupancy
for the dock's decision: `GET .../buffer` - utilization hits 0.0 after a
successful purge, the spec's "boot at 0%".

Order is the safety property:

1. **Export verbatim** - unlocked rows append to the space's JSONL
   (stable name, so nights accumulate into one corpus). Each line is the
   spec schema: `timestamp`, `raw_transcript`, `speaker_style_vectors`,
   `emotional_variance_score`. No rewriting, no filtering, no dedup.
2. **Lock** exported ids - the DB now records what provably left.
3. **Train** - optional `DUAL_LOBE_NDSMC_TRAIN_COMMAND` receives the JSONL
   path as its last argument. This is where the operator runs PEFT/LoRA
   with the 80/20 generative-replay mix; replay data is synthesized by the
   runtime's own LLM, so it belongs to that pipeline, not the proxy.
   Non-zero exit aborts the purge and the rows stay for tomorrow night.
4. **Purge** only locked rows. A crash anywhere before step 4 loses
   nothing; there is no code path that deletes unexported data.

Being unplugged mid-dream is therefore answered by construction, not by a
grogginess flag: interruption before the purge means the buffer still holds
everything and the next night's run repeats the cycle. (An interrupted
*weight merge* is the operator pipeline's atomicity problem - train into a
staging adapter, swap atomically - which is why the training step is an
opaque command with an exit code rather than proxy-internal logic.)

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