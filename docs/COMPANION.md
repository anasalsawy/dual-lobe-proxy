# The companion: one persona, many surfaces

A companion is a single AI persona that lives in many places at once - a PC, a
phone, an app, a voice channel - and is recognisably the *same person* everywhere
because all of those surfaces read and write **one persistent memory**.

dual-lobe already contains the brain: A speaks, B watches, remembers and verifies.
The companion adds two things on top - a **stable identity** and a **presence
layer** - without a rewrite.

## The identity rule: persona = API key

The persona is the API key. Any surface that presents the key *is* the same
persona, and therefore the same memory. There is no separate persona registry to
keep in sync - the key already authenticates every request, so it also defines
who is remembering.

```
surface (phone)  ┐
surface (pc)     ├──  same API key  ──►  same persona  ──►  same memory
surface (app)    ┘
```

A key resolves (today) to a tenant. To make the *key* the persona rather than the
tenant, the key carries a `persona` value:

- `api_keys.persona` - a stable string identity for the key.
- `Principal.persona` - surfaced to every handler alongside `tenant_id`.
- Memory is scoped to `(tenant_id, persona)`.

Two keys may share a persona (same person, a new device - the app on your phone
joins the persona you already have on your PC), or each be its own persona. That
choice is data, not code.

## Why this reuses almost everything

The memory layer is *already* keyed on `(tenant_id, space)`. Making the persona
the `space` means every existing piece - shared memory, the episodic notebook,
full-text relevance ranking, decay - works unchanged. The companion is not a new
storage engine; it is a **scope** and a set of **thin clients**.

## Surface context (ephemeral, never memory)

A surface may tell the server where it is - device, app, locale - so the persona
can be aware of context ("you were on your PC"). This is **ephemeral request
context**: it shapes the current turn, it is not written to long-term memory.
Conflating the two is how a companion becomes uncanny. The rule: *memory is what
the user said; surface is where they said it.*

## Layers

1. **Identity** - persona (= key) scopes all memory. *Phase 1.*
2. **Surface tagging** - the client sends `(device, app)`; stored ephemeral.
   **Built:** `X-DL-Surface-Device` / `X-DL-Surface-App` headers, validated to
   64 safe chars, appended to A's per-turn context as one ephemeral line, and
   recorded on notes as provenance (`surface_device`, `surface_app`) - the
   surface never becomes memory content.
3. **Cross-surface continuity** - "on your PC you were looking at X", via the
   episodic layer's anchors + surface provenance columns. **Built (data side):**
   notes carry surface provenance; rendering "you were on your phone when..."
   is a future composer step over that data.
4. **Proactive companion** - B decides to surface a memory unprompted, through a
   scheduler + a notification channel. *Phase 4. Highest risk of feeling spammy;
   gate hard on relevance and rate.*
5. **Interaction features** - voice, images, tools. Each is an **adapter**, not a
   rewrite: they normalise input into the same message shape A and B already take.

## Interaction features map onto the existing seam

Every "human interaction feature" is an input surface. In dual-lobe the seam is
already there - a normalised message list goes in, A answers, B verifies. So:

```
voice  ──► transcribe ──┐
image  ──► describe   ──┼──►  messages[]  ──►  A (speak)  +  B (watch/remember)
text   ──► as-is      ──┘
```

Each feature is a normaliser in front of the same pipeline. Nothing about A, B,
the meter, or memory changes.

## The hard problems (be honest about these)

- **Conflict** - the user says X on the phone and Y on the PC. Which is true? The
  persona must converge; last-write-wins per memory key is the simple answer, with
  provenance (which surface, when) attached so a later turn can reconcile.
- **Latency of memory** - say something on the phone; is it instant on the PC?
  Postgres is the substrate; a write-through path plus an event bus makes surfaces
  converge. Until then, cross-surface recall has a small delay.
- **Uncanny / privacy** - a system that remembers everything everywhere will feel
  wrong unless the user can **see and wipe** exactly what it knows. The episodic
  layer already makes every note quotable and auditable; the companion must expose
  the same inspect/wipe surface per persona.

## Build order

```
Phase 1  persona-as-key          identity scopes memory        <- built
Phase 2  surface tagging         device/app as ephemeral ctx   <- built
Phase 3  cross-surface recall    "on your PC you were..."      <- provenance built
Phase 4  proactive companion     scheduler + notify (gated)    <- first beat built:
         the unaddressed-question queue (an aside never resurfaced for
         DUAL_LOBE_EPISODIC_UNADDRESSED_DAYS days may surface once, off-topic,
         capped at one; surfaced_by: "never followed up")
Phase 5  interaction adapters    voice / image / tools
```

## Interaction beats (built)

Beyond the phases, four interaction features ride the same seams. All are
deterministic, all fail-open, none touch the meter:

- **Revisit-and-revise** - every verified turn stores a verdict snapshot
  (`verdict_snapshots`); when a later turn on an overlapping topic reaches a
  *different* level, a callback note records it ("previously GREEN, now YELLOW")
  with the user's own words from the current turn as the quote. The companion
  can change its mind and say so.
- **Mood-gated associative recall** - a past note whose feeling matches the
  space's current mood (C's most recent tag) may surface even off-topic, once,
  salience >= 2 (`DUAL_LOBE_EPISODIC_MOOD_RECALL`). Mechanical, no embeddings.
- **Session mood advisory** - when the last three meaningful feeling tags
  agree, A receives a one-line tone advisory ("recent turns have felt
  'tired'"). Style only; it never changes verification.
- **Unaddressed-question queue** - see Phase 4 above.

Every note that surfaces records `resurfaced_at`, so "did we ever get back to
that?" is answerable from data rather than vibes.

Phase 1 is the load-bearing one: everything else assumes the persona is the
memory scope.
