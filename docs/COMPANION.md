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
2. **Surface tagging** - the client sends `(device, app)`; stored ephemeral. *Phase 2.*
3. **Cross-surface continuity** - "on your PC you were looking at X", via the
   episodic layer's anchors. *Phase 3.*
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
Phase 1  persona-as-key          identity scopes memory        <- start here
Phase 2  surface tagging         device/app as ephemeral ctx
Phase 3  cross-surface recall    "on your PC you were..."
Phase 4  proactive companion     scheduler + notify (gated)
Phase 5  interaction adapters    voice / image / tools
```

Phase 1 is the load-bearing one: everything else assumes the persona is the
memory scope.
