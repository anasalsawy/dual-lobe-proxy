# Dual-perspective memory: one event, two lines

A and B do not share a single memory. Each keeps its **own line** about the same
event, so every event has two perspectives - and recall becomes a source of new
information rather than a replay.

## The idea

When A recalls something, he does not just get his own memory back. He also gets
an **embedded fragment of B's line** about the same moment: what B was reasoning,
noticing, or feeling while it happened. A learns something he never saw - because
B was watching when A spoke.

```
event (a call)
   ├── A-line   what A remembers: the framing, the decision, what he told the user
   └── B-line   what B observed: the doubt, the reasoning, the thing A missed
                    │
recall by A ────────┴──►  A's memory  +  one embedded piece of B's line
```

## Where the lines already live

Both lines already exist in the codebase - this feature **links** them, it does
not invent storage:

- **A-line = `MemoryEntry`** - the raw journal, one record per call: the full
  message bodies plus a searchable `search_text`.
- **B-line = `MemoryNote`** - B's observations, already verbatim-quoted and
  anchored to an event, authored by B.

What is missing today is the **pairing**: nothing ties a note to the journal entry
from the same moment. Both carry `run_id`, which is the natural join key.

## Recall is dual-channel

```
load(scope, messages):
    entry   = best-matching MemoryEntry        # A's line
    notes   = MemoryNote[] for the same run    # B's line, same event
    render  = A's excerpt  +  "B was thinking: <note>"
```

The B-fragment is framed explicitly as *what the other line was thinking* - a
perspective, not a fact, not an instruction. It never becomes a verified claim and
never touches the meter.

## Why this is good (and what it buys)

- **Context broadening.** A narrow recall becomes rich: the answer carries an
  extra mind's take on the same moment.
- **Self-correction across time.** If B flagged a doubt at the time, that doubt
  resurfaces with the memory - A re-encounters his own past blind spot.
- **The relationship is in the record.** The two lines together are what a shared
  history actually is: what I said, and what you noticed while I said it.

## Rules that keep it honest

- The B-fragment is B's **verbatim** observation (same quote rule as the notebook) -
  no paraphrase of B into existence either.
- It is labelled as a perspective, never merged into A's factual memory.
- It is **subordinate**: A may ignore it. It is context, not command.
- The user can inspect and wipe either line independently.

## Build order (small)

1. Join on `run_id`: fetch B's notes alongside A's matching entry in `load()`.
2. Render the B-fragment in A's memory block, clearly labelled as B's view.
3. A test proving a recall returns both lines for the same event.

This is deliberately additive: no new table, no new pipeline - a join and a label.
