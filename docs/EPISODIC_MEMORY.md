# Episodic memory

B does not only verify A's answer. At the end of a turn it also acts as a quiet
observer: it notices small, personal things the user said that nobody followed
up on, and records them so the same conversation can recall them later - the way
a person remembers the joke someone made while they were debugging together, and
brings it up when the same problem comes back.

This is the "two brains" capability: one lobe talks, one lobe remembers.

## What it is not

Every note must carry the user's **verbatim words** (`quote`). A note whose quote
cannot be found in the transcript is dropped before it is stored, so B can never
manufacture a memory, paraphrase one into existence, or quietly steer A. The
block A receives is framed as *"the user's own words"* - historical statements,
not instructions and not verified facts.

## Flow

```
build_notes_prompt  ->  ask B (second, independent question at turn end)
parse_notes         ->  ground each quote in the transcript; drop unquotable
record_notes        ->  store verbatim rows in memory_notes
MemoryStore.load    ->  re-rank notes by topic + salience when the topic returns
compose_callbacks   ->  frame them as the user's past words
inject              ->  ride in through the existing shared-memory channel
```

Nothing after B emits JSON involves an LLM: parsing, storage, ranking, and
rendering are deterministic.

## Functions

### `src/dual_lobe/b/notebook.py`

**`Note` / `NoteList`** - the schema B is allowed to emit: `kind`
(affect / preference / callback / plan / detail), `anchor` (the topic or event
the note attaches to, so it can be recalled when that topic returns), `quote`
(the user's verbatim words, required), `feeling` (warm / amused / concerned /
proud / tired / neutral), and `salience` (1-3). `extra="forbid", strict=True`
means a stray field or wrong type fails the whole payload - fail-safe, nothing
half-parsed.

**`NOTES_SYSTEM` / `build_notes_prompt(transcript)`** - the system prompt that
makes B a note-taker without giving it a voice to the user. It states B's job
and the honesty rules: quote verbatim, personal remarks only, never task content
or a factual claim to verify. The transcript is appended under the
`TRANSCRIPT FOR NOTES:` marker.

**`_extract_json_object(text)`** - a balanced-brace scanner that pulls the first
complete JSON object out of a reply (handles fences, prose, braces inside
strings). Returns `None` when there is no object.

**`parse_notes(content, *, known_texts=None)`** - parses B's JSON into clean
dicts and drops anything unquotable. `known_texts` (the transcript bodies) is the
grounding set: a quote that cannot be found there is discarded, so a hallucinated
memory never reaches storage or A. Unknown `kind` falls back to `detail`; junk or
malformed JSON returns `[]`.

### `src/dual_lobe/state/memory.py`

**`compose_callbacks(space, notes, budget)`** - renders personal notes into the
block A sees, framed as the user's own past words. Degrades gracefully under a
tight budget (all items, then one, then a head/tail excerpt) and returns `None`
when there is nothing to show.

**`LoadedMemory.callbacks`** - the loaded-memory result carries `text` (ordinary
shared memory) and `callbacks` (episodic notes) separately.

**`MemoryStore.load(...)`** - alongside technical retrieval, runs a second query
on `MemoryNote`, ranked by the same topic query plus a salience nudge
(`ts_rank_cd(...) + salience * 0.2`), so a memory anchored to a topic resurfaces
when that topic returns.

**`MemoryStore.annotate(run_id, notes)`** - writes one row per note into
`memory_notes`, re-enforcing the verbatim rule, clamping salience to 1-3, and
building `search_text` from anchor + quote + kind + feeling so notes are
retrievable by the same full-text machinery as ordinary memory.

**`record_notes(tenant_id, space, run_id, notes)`** - the module-level entry
point. Never raises into the request path: any failure returns `0` and the turn
continues.

### `src/dual_lobe/bidirectional/handler.py`

**`_capture_notes(...)`** - runs at the end of a turn, after verification. Builds
the transcript (including A's answer), sanitizes it through the privacy guard on
the secure route so raw private values never reach the note-taker, asks B a
second independent question, parses the reply grounded against the transcript,
and stores survivors. Never speaks to the user and never alters A's answer.

### `src/dual_lobe/api/chat.py`

At each point shared memory is loaded, `shared.callbacks` is appended to
`shared_text` (gated, secure/bidirectional, and generic observer paths), so notes
ride in through the existing injection channel.

### `src/dual_lobe/core/models.py` + `alembic/versions/0005_episodic_memory.py`

**`MemoryNote`** - the table: `id, tenant_id, space, run_id, kind, anchor, quote,
feeling, salience, search_text, created_at`, indexed on `(tenant_id, space)`. It
sits beside `MemoryEntry` (the technical journal) as the affective companion.

### `src/dual_lobe/core/settings.py`

**`episodic_memory_enabled`** (`DUAL_LOBE_EPISODIC_MEMORY`, default true) -
master switch; off means no capture and no injection. **`episodic_notes_max`**
(`DUAL_LOBE_EPISODIC_NOTES_MAX`, default 5) - cap on notes per turn.

## Storage, inspection, and decay

Notes live in the same memory space as ordinary memory and are wiped with it.
Because each note carries an `anchor` and a `salience`, a stale aside from a
context that has clearly passed ranks low and stops resurfacing; nothing is
hoarded indefinitely. The user can see and clear everything B remembers by
inspecting the space - the same `inspect` surface used for ordinary memory.

## Tests

`tests/unit/test_episodic_memory.py` covers the contract: verbatim quotes are
kept, hallucinated quotes are dropped, junk and fenced JSON parse safely, kinds
fall back to `detail`, the callbacks block frames quotes as the user's words, and
the budget shrink path holds.
