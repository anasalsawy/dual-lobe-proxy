# Dual-lobe mode (background thinking + persistent short-term memory)

Status: **design only** — nothing in this document is implemented. No code has been
written for it. This describes a new, isolated mode that does not modify any
existing path.

## What it is

A standalone inference mode, parallel to the hierarchy tiers and to the gated
handler. It answers as usual, then keeps working in the background: A and B run a
bounded private exchange about the task just answered, a summary of that exchange
is stored, and the next request starts from it.

Goal: the user feels that the work continued after the reply.

## Isolation

- New module. Does not import from or modify `gated/handler.py`, `gated/prompts.py`,
  `director/*`, `state/memory.py`, `b/*`, or the tier personas in `roles.py`.
- New alias `sawii/dual-lobe`. The alias currently named `sawii/dual-lobe` is
  renamed to `sawii/dual-lobe-old`.
- The rename is the only change to existing files: one entry in
  `provider/registry.py` (the `env_targets()` dict and the `user_facing_models`
  set) and the alias maps in `api/chat.py`.
- New store, own tables, tenant-scoped. Nothing is shared with the existing
  memory space or with any tier.

## Turn sequence

1. Request arrives on the dual-lobe alias.
2. A answers.
3. B rates A's answer (GREEN / YELLOW / RED + rationale). This is the same
   rating concept the gated handler uses, produced by a dual-lobe-owned prompt.
4. The user receives A's answer with the meter, exactly as the gated mode
   presents it today. No extra user-visible wait beyond the rating.
5. After the response is sent, a FastAPI `BackgroundTask` starts:
   - The meter B just produced is handed back to A privately (no user-visible
     hold, no flip-back).
   - A and B exchange 4 rounds, silently. Default 4, configurable.
   - A summariser call writes up the exchange as a full-fidelity summary, phrased
     as A continuing its own thought ("so I was thinking about this…").
   - The exchange and the summary are written to the store.
6. The next request reads the store and injects the newest rounds into A's prompt.

The loop is not bounded by user idleness. Nothing schedules it: the proxy only
exists while a request is open, so the loop starts when the response is sent and
runs to completion regardless of whether the user has returned.

## What the loop sees (relevance)

The private exchange must carry the task, or A and B exchange rounds with no
statement of what the user actually wants. That is a larger relevance risk than
drift over 4 rounds.

Each round's context:

```
system:    the task/intent (host system prompt or the run goal)
context:   the injected slice — newest rounds from previous turns
context:   the meter B produced this turn
user:      the host's latest message
assistant: A's answer this turn
then:      rounds 1..4 between A and B
```

The loop reuses the same injected slice that the next request receives. Building
that slice once serves both consumers, so continuity costs nothing extra in
tokens — only a second store read per turn.

If A wants more context than the slice, it can call the memory tool. Inside the
background loop that extra A generation is invisible to any user.

## Turn start

The loop's history is reset per invocation. Each invocation starts from the
current turn plus the injected slice; it does not carry the previous invocation's
private rounds as conversation history.

## Storage

- Ring buffer. Never empties, no TTL, no session reset.
- Cap: 256 (default). At the cap, the oldest entry is discarded to make room for
  the newest write.
- Bound options exposed to the caller: per run / per session / per time.
- Per tenant.
- The cap is a configured number, not the disk. Rationale: the store shares the
  Postgres volume with the proxy's other tables, so an uncapped store could
  exhaust the volume and take down every tenant, not just its own.

### Read side (two layers)

1. **Injected.** The newest thinking rounds are loaded into A's prompt on the next
   request. Guaranteed delivery, no extra model call, no added latency. Bounded by
   a read budget, since a full store cannot fit in a prompt.
2. **Tool.** The remainder of the store is reachable through a proxy-owned tool A
   can call. Retrieved on demand; costs nothing when unused.

Why both: injection is unconditional but costs prompt tokens on every call; the
tool is free when unused but only works if the model chooses to call it, and it
costs one extra A generation when it does.

## The memory tool

- The proxy adds the tool definition to the `tools` list it sends to A. The host
  is not required to implement or even know about it.
- The proxy inspects A's returned tool calls. A call to the reserved tool name is
  resolved by the proxy against the store and is never handed to the host.
- Resolving the call requires a second A call in the same request (A has already
  finished generating when the call is seen). So the handler is not one-shot while
  the tool is in use: one extra full A generation.
- The result must be appended as a `tool`-role message with the matching
  `tool_call_id`.
- The tool name must be namespaced to avoid colliding with host tools, and the
  proxy must ignore it if the host already defines that name.

Consequence to record deliberately: this is the first case where the proxy
executes something on A's behalf. A store read is side-effect-free, which is the
narrowest possible exception, but it is an exception and should be documented as
one rather than treated as a normal passthrough.

## Settings (proposed)

| Setting | Default | Scope | Meaning |
|---|---|---|---|
| `dual_lobe_enabled` | on | per tenant | Enables the mode |
| `dual_lobe_rounds` | 4 | per tenant | Private rounds per turn |
| `dual_lobe_summarize` | on | per tenant | Write the summary after the loop |
| `dual_lobe_bound_by` | (tbd) | per tenant | run / session / time / turns / consumed |
| `dual_lobe_bound_value` | (tbd) | per tenant | Value for the chosen bound |
| `dual_lobe_store_cap` | 256 | per tenant | Ring buffer cap |
| `dual_lobe_read_budget` | (tbd) | per tenant | Injected-context budget |
| `dual_lobe_max_seconds` | (tbd) | global | Wall-clock budget for the loop |
| `dual_lobe_tenant_enabled` | on | global | Kill switch |

## Known costs and risks

- **Model cost.** The loop is ~9 extra calls per user turn (4 rounds × 2, plus the
  summariser) on every turn, for every user, from the moment they connect.
  Default-on means this is the base cost of the mode.
- **Provider contention.** The loop runs after the response, so it overlaps with
  the same tenant's next request. If B inherits A's provider, that is 2 A-side
  calls plus B's calls competing for one quota.
- **No durability guarantee.** A `BackgroundTask` dies with the process. A deploy
  or restart mid-loop loses those rounds silently. There is no replay.
- **Delivery is not use.** Injection guarantees the text reaches A's prompt. It
  does not guarantee the model reads or acts on it.
- **Host memory is not involved.** The proxy cannot write into the host's session
  memory. Persistence lives in the proxy's own store and is re-injected per call.

## Open items

1. `dual_lobe_bound_by` default and value.
2. `dual_lobe_read_budget` value.
3. `dual_lobe_max_seconds` value.
4. Whether the summariser is the same model as B or a separate target.
5. Confirmation that the human-readable summary phrasing is generated per call
   rather than being a fixed string.
