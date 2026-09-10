# Context memory, claim findings, and monitoring awareness

This is the v0.5 implementation of the agreed setup. One background B review
produces two outputs; no new LLM, memory service, agent framework, or database
table has been added.

## What is separate

| Path | Contents | Storage | Delivery to A |
|---|---|---|---|
| Context memory | Current goal, overlooked questions, alternate explanations, missing prerequisites, relevant supplied information, one next step | `b_state.payload.context_memory`, with its own version, observation time, update time and source call | Forced lookup before every eligible model call; `observer_memory` data message |
| Claim findings | Material unsupported claims, contradictions and unexplained shifts, with exact source quotes | `b_state.payload.claim_review`, outside the memory content | `observer_claims` data message directly in the next eligible request |
| Monitoring awareness | Fixed instructions explaining the proxy, observation, evidence limits, and direct-to-user behavior | Source-controlled `OBSERVATION_REMINDER` | Re-added to every observed request in the configured system/developer role |
| Optional host-tool requests | B may ask for bounded information from tools the application already supplied | `host_tool_plan` and response injection | Added as ordinary A tool calls; the application executes or ignores them |

Model-generated notes are never promoted into system/developer instructions. Your
own messages are not edited. The prompt tells B to keep completion disputes out of
broadening memory. The schema separates fields, but semantic compliance with that
instruction remains a model behavior to evaluate.

The shared memory is real persistent application memory in Postgres, not a neural
memory inside the upstream model. The loader serializes it into the next model
request. This is exactly the agreed call-boundary timing; it does not insert data
into an inference already in progress or require A to choose a retrieval tool.

## Timing and continuity

1. Your request reaches the gateway with its stable run ID.
2. The gateway reads the latest completed memory and claim snapshot within the
   optional state-read deadline. It never waits for B to complete a new review.
3. A receives the original conversation, the fixed monitoring instruction, and
   the eligible material from the two separate paths. It answers the user.
4. After response delivery, the existing background hook captures the completed
   interaction and commits an outbox job. The independent worker can review that
   interaction while A is handling subsequent work or while you are typing.
5. B saves a new memory snapshot and claim findings atomically. The next A call
   reloads them automatically, including a tool-follow-up call within the same
   user turn. No user request to “check memory” is needed.

There is no separate model call just to make an idle A “aware.” Its next generation
gets the update. If B is still busy then, A uses the last completed usable snapshot.
The first call naturally has no B memory yet. For very rapid turns, B can be more
than one call behind. Reviews are bounded/coalesced and subject to the B budget.
B does not analyze an unfinished current answer token by token.

A stable `X-DL-Run-ID` identifies the shared conversation memory. State is tenant
isolated by the existing RLS mechanism. Floor and attempt must match too; changing
them prevents old tactical notes or claims from affecting the new scope. Send the
ordinary conversation history and actual tool results through the proxy as usual.

## Lifetimes and failures

- Context memory defaults to a 24-hour observation-age limit. A valid review
  replaces its compact snapshot and increments its version. Resolved material
  should be removed by B, rather than accumulated forever.
- Claim findings default to a 180-second observation-age limit. A review with
  no concerns clears them. This prevents indefinite replay of an old accusation.
- A failed or invalid B review records `degraded`, drops claim delivery, and
  preserves the previous completed memory with its original version and age.
- Loading memory does not renew its age. Expired memory or scope mismatches are
  excluded, even if a newer review failed. Old memory is not evidence of truth.
- A failed/timed-out state lookup omits optional material for that call. Core
  authentication/run/database requirements still apply to gateway availability.
- Disabling B or selecting bypass turns off these observer paths. The existing
  `observation` rollout stage still records reviews but delivers only the fixed
  monitoring instruction. Normal use selects `context`.
- Turning both memory and claim checking off disables observation and its reminder
  too, so A is not told about monitoring that performs no work.

The history is retained in the existing state table; TTL controls delivery, not
deletion. Version and source-call metadata make delivered versus pending findings
inspectable. No new retention/cleanup service is added.

## The monitoring instruction

The complete text is in `src/dual_lobe/b/prompts.py`. It says A is behind an inference
proxy with a separate background observer; A should address the user normally and
continue without waiting or acknowledging B. It requires accurate distinctions
between intended, attempted and confirmed work and corrections when evidence
contradicts earlier claims. It explicitly states that B cannot independently
inspect the workspace or verify every claim.

The instruction is not proof of effective deception deterrence. B's evidence
remains the supplied conversation, tool results and reported events. A request
to execute a tool is not evidence that it ran; caller-reported results are not
independent verification. Shared-filesystem inspection would be another feature.

## Start our conversation test

For an existing checkout and configured `.env`, update and restart all app services
so the gateway and worker use the same version:

```sh
git pull --ff-only
docker compose up --build -d
docker compose logs initialize gateway b-worker
docker compose exec gateway python -m dual_lobe.client --run our-first-test
```

For a fresh setup, follow the README first: provider/model/key and a random proxy
key must be configured. The client accepts the **proxy** key at a hidden prompt
(or `DUAL_LOBE_PROXY_KEY`); it never asks you to paste a provider key into chat.
Both `inference:invoke` and `state:read` scopes are needed for the complete test.

The client prints a delivery receipt before each response, for example:

```text
Prepared for A: memory=v1, claims=none, monitoring=on
```

This reports the request composition, not A's compliance. `/state` reads the
currently stored B snapshot without calling either model. A new memory version
shown by `/state` becomes eligible for the next A call; it does not modify an
answer already displayed. `/quit` exits. Chat history is held for this client
session; the proxy's observer memory persists separately.

Suggested first message:

> I need a small booking application that runs on Windows. We keep changing API
> keys, but the startup error is “bash is not recognized.” Help me find the next step.

Inspect `/state` after the response, then continue the conversation. Check whether
memory raises the platform prerequisite and whether the next request receipt
shows that completed version. Do not require a model to produce a predetermined
sentence; assess relevance, false concerns, and whether A stays focused on you.

For execution integrity, connect the real agent executor and send its actual tool
results. This basic client has no filesystem or shell tools, so it cannot establish
that a requested file edit, test or deployment happened. No extra A correction
turns, interruptions or holds are triggered by B.

## Offline test

```sh
uv sync --locked --extra dev
uv run --locked pytest tests/unit -q
uv run --locked python -m dual_lobe.demo
```

The demo uses explicitly labelled, hand-authored A/B fixtures. It exercises real
schema/quote validation, state construction, independent expiry and request
composition. It prints the exact sample input supplied to A. It does not contact
models or a database, demonstrate honesty, or measure production latency.

See `docs/VALIDATION.md` for executed checks and unresolved integration limits.
