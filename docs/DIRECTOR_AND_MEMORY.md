# Director mode and persistent memory (v0.5)

Director mode automates the back-and-forth you previously carried between an
advisor chat and your agent. B reads A's answer and writes the next question or
direction in your conversational place. The proxy calls A again with that turn.
B does not execute A's work directly. It may request an information-gathering
tool already supplied by the connected app; the app executes it and returns the
result through the normal tool loop. No connector or extra application is added.

In v0.5, B uses the same explicit tunnel vision / “open sesame” enrichment role
in both modes. See [the editable prompt and meter guide](OBSERVER_GUIDANCE.md).
Upgrade with migration 0004 before starting updated gateway/worker code; it adds
attributed observer notes without resetting existing memory.

## Start and watch it

Configure the existing A/B provider settings and proxy key in `.env` first.
For an existing Compose deployment, update and apply the new migrations:

```sh
git pull --ff-only
docker compose build
docker compose stop gateway b-worker
docker compose run --rm initialize
docker compose up -d
docker compose exec gateway python -m dual_lobe.client --director --memory-id main --run director-test-1
```

Enter your **proxy key** at the hidden prompt. Provider keys stay in the server's
environment. A useful first text-only test is:

> First give a rough plan for a small Python task-list CLI and identify one
> assumption. Then refine the plan after the director questions it. It must work
> on Windows, persist tasks locally, and stay simple. Planning only; no execution.

The client shows A's text as it arrives, followed by B's validated short message,
then A's next turn. Both labels appear inside one accumulated assistant response.
These are separate, successive model calls; B reacts to the preceding A answer.
A sufficient initial answer may lead B to stop after one exchange.

Commands: `/director`, `/normal`, `/new`, `/state`, `/director-state`, `/memory`,
`/quit`. Ctrl+C closes the request and cancels this client. A new mode or `/new`
uses a new run ID; the selected shared memory space remains the same. The client
does not have an executor or offer tool definitions. Test real tools using your
existing agent application.

For a native installation, use:

```sh
uv run --locked python -m dual_lobe.client --url http://localhost:8801 --director --memory-id main --run director-test-1
```

Use `--message "your request"` for a single invocation instead of interactive input.

## Connect an existing agent

Keep the usual Chat Completions base URL and proxy API key. Select
`model: lobe-a-director`, or keep `lobe-a` and send
`X-Dual-Lobe-Mode: director`. No phrase inside the user's text secretly enables
the mode. Always send a stable `X-DL-Run-ID` for a director session, including on
requests carrying tool results. Use a different run for concurrent agents.

Normal `lobe-a` retains the asynchronous observer. Director mode invokes the same
B provider inline with a separate prompt and deliberately adds time and model
calls. `DUAL_LOBE_B_ENABLED` controls the background observer;
`DUAL_LOBE_DIRECTOR_ENABLED` independently controls the opt-in loop.

| Event | What the proxy does | What the application sees |
|---|---|---|
| New director request | Loads stored context and calls A with the caller's tools/settings | A's labelled text streams |
| A finishes a text answer | Calls B with the observed context, answer, and bounded host-tool definitions | B's short question, correction, stop message, or optional information-call request |
| B continues | Adds a user-role message named `director_b`, then calls A | A's next answer in the same growing response |
| A requests tools | Validates complete IDs/arguments; commits memory and session; ends the response with `finish_reason: tool_calls` | Ordinary function tool calls with their original IDs and arguments |
| Host executes tools | The proxy waits for the host's next request; it executes nothing | The existing application's usual execution/approval interface |
| Results return | Matches each pending ID once, restores A/B history, and calls A with the results | The resumed exchange in a new HTTP response |
| B stops or budget expires | Stores the stop reason and completes the response | An explicit stop notice; this is not proof that work succeeded |

Tool calls are buffered until complete and durably checkpointed; ordinary A text
streams earlier. The proxy never holds an open response waiting for a tool that
the host cannot run until that response finishes. Parallel function calls must
all receive one matching result. Missing, duplicate, altered, or unrelated results
return 409. The proxy never turns a tool request or B's approval into evidence of
execution. A consumes actual host-reported results before B's next review.

Tool schemas and tool-choice settings come from the host and are retained for A;
B receives bounded definitions for any tools supplied by the host. It can request
reads or mutations, but the proxy never executes tools itself: the host application
retains permission, approval and execution control. Forced tool choice can keep A producing tools, so
those calls also count toward the invocation budget. Host permissions remain in
effect. An inference proxy cannot create new native user bubbles or promise that
every client will display text alongside tool requests. Host-specific signed
reasoning/replay state is not supported by director mode. Use ordinary mode for
provider-specific protocols.

Full-history clients resend their usual history, including the combined assistant
response and real tool results. The proxy checks this against the stored wire
history but supplies A the original individual A/B turns, avoiding duplicated
transcripts. SDK-added optional null fields are accepted. Changed system prompts,
edited history, or a different task/floor/attempt need a new run.

If a client explicitly sends only newly added messages, use `X-DL-History: delta`
and `X-DL-Request-ID`. The request ID must be unique for each HTTP request and
reused on retries. Stored history is prepended by the proxy. Reused IDs return
409 without new model calls. The default `full` mode detects replay through its
expected history. Delta mode is an explicit contract, not guessed from message
contents. Different apps can share memory without sharing a director run.

`stream: false` runs the same loop but returns the accumulated transcript only
after the segment ends. Live observation requires streaming. Structured/JSON
`response_format` is rejected in director mode because transcript labels would
break that contract. Ordinary `lobe-a` still forwards that option.

## Persistent memory across agents and apps

The proxy owns two durable stores, separate from the observer's run-scoped notes:

| Store | Scope | Contents and use |
|---|---|---|
| Director session | Tenant + run, fixed task/floor/attempt/worker/memory space | Canonical individual A/B conversation, host-visible history, tool handoff, counters, deadline, and short transaction lease |
| Shared memory | Tenant + named memory space | Supplied conversation/A replies, separate generated observer notes with model/call/time provenance, plus an editable pinned notebook; bounded selection loaded on each A call |

`DUAL_LOBE_DEFAULT_MEMORY_ID=main` makes all apps on the same proxy tenant share
memory by default. Different API keys may share it if they belong to that same
tenant. Different tenants cannot share it. Use `X-DL-Memory-ID: project-name` to
select a distinct space. `X-DL-Memory-ID: off` disables only the shared journal
and notebook for that request. Set the default name to empty if explicit opt-in
is preferred. Disabling shared memory does not erase existing data.

An app can send only its latest question and the shared memory is still loaded.
The proxy does not require an app's own memory feature, a model tool call, or an
embedding service. It retains observed conversation bodies in Postgres; full
history clients create overlapping journal records. Private reasoning fields are
excluded. Partial/interrupted completions are not stored as successful exchanges.
The director session separately retains its accepted individual turns.

Retrieval uses the complete pinned notebook, the three latest journal entries,
up to four keyword matches, and the first entry if it is not already selected.
Entries are deduplicated and excerpted to fit the configured context budget.
Each excerpt carries its record ID, timestamp, and source run. This is lexical
search, not semantic/embedding search. Long entries have a bounded 64,000-character
search preview; the recorded message bodies remain available through inspection.
The search uses PostgreSQL's built-in [text search and ranking](https://www.postgresql.org/docs/current/textsearch-controls.html)
and [GIN indexing](https://www.postgresql.org/docs/current/textsearch-tables.html).

Completed B knowledge notes attach to their source journal entry. At most one
snapshot (two notes) is selected, within the existing memory budget and TTL,
favoring a keyword-matched conversation. The generated wording is not separately
indexed. Run-local knowledge takes precedence to avoid duplicate snapshots.
Disabling enrichment suppresses saved notes without deleting them; raw history
and pinned notes are separate. Notes remain model-generated guidance, never
independent evidence that work occurred.

Memory is supplied as a separate user-role `shared_memory` message, marked as
untrusted historical data. It is not neural memory or an injection into a running
generation. Older statements may be wrong or superseded. Durable storage does not
mean every archived fact fits in the model's current context or that the model
cannot overlook a loaded fact. Put essential enduring constraints in the notebook.

Inspect/search the archive using a key with `state:read`:

```sh
curl 'http://localhost:8801/v1/dual-lobe/memory/main?limit=10&q=Windows' \
  -H 'Authorization: Bearer YOUR_PROXY_SECRET'
```

Use `next_before` as the `before` query parameter for older pages. This endpoint
returns the stored message bodies, not just prompt excerpts. Edit the notebook
using a key with `inference:invoke`:

```sh
curl -X PUT http://localhost:8801/v1/dual-lobe/memory/main/notebook \
  -H 'Authorization: Bearer YOUR_PROXY_SECRET' \
  -H 'Content-Type: application/json' \
  -d '{"notes":"Prefer simple solutions. Deployment target: Windows. Report tests actually run and remaining failures."}'
```

The notebook is a replace operation with a 4,000 JSON-encoded-character limit;
an empty string clears it. A and B do not automatically edit the pinned notebook.
Automatic saving applies to the journal, including ordinary-mode calls.

If selected shared memory cannot be read, normal inference returns 503 before
calling A. If saving fails, the response is explicitly incomplete instead of
claiming a successful, remembered turn. Streaming text may already be visible;
the terminal event is withheld until the write succeeds. This adds database
latency. Optional observer-state reads retain their older fail-open behavior.

Persistence survives app restarts and new database connections as long as the
Postgres data remains. Compose uses its persistent `pg_data` volume. Removing that
volume, losing the database, or moving to an unrelated proxy/database does not
preserve memory. Backups and retention are deployment responsibilities; the
journal has no automatic expiry. Shared memory does not move workspace files,
tools, permissions, or a live execution session between applications.

## Settings and limits

| Setting | Default | Meaning |
|---|---:|---|
| `DUAL_LOBE_DIRECTOR_ENABLED` | true | Allows explicitly selected director mode |
| `DUAL_LOBE_DIRECTOR_MAX_A_CALLS` | 8 | A calls per invocation, including all tool resumptions |
| `DUAL_LOBE_DIRECTOR_MAX_SECONDS` | 300 | Wall time per invocation, including waiting for host tools |
| `DUAL_LOBE_DIRECTOR_A_MAX_TOKENS` | 4096 | A output cap; a smaller caller cap is retained |
| `DUAL_LOBE_DIRECTOR_B_MAX_TOKENS` | 1000 | B decision output cap |
| `DUAL_LOBE_DIRECTOR_MAX_STATE_BYTES` | 1048576 | Session budget; stop before the next call when half is used, reserving space for output and wire history |
| `DUAL_LOBE_SHARED_MEMORY_ENABLED` | true | Enables journal recording and retrieval |
| `DUAL_LOBE_DEFAULT_MEMORY_ID` | main | Shared space for apps that omit the header; empty disables the default |
| `DUAL_LOBE_SHARED_MEMORY_MAX_CHARS` | 10000 | Selected memory supplied to each A call |
| `DUAL_LOBE_SHARED_MEMORY_TIMEOUT` | 5 s | Deadline for selected memory reads/writes |

Each internal A/B call also checks the existing process-local gateway admission
budget; the original HTTP request consumes an admission too. Director mode does
not use the background worker's separate B RPM budget. B's call timeout remains
`DUAL_LOBE_B_TIMEOUT`; A's remains `DUAL_LOBE_A_TIMEOUT`, bounded by the invocation
deadline. Streaming is not retried. Invalid B output, incomplete A output, and
storage failures end the exchange explicitly. B must return only its two-field
JSON decision; the user sees its message, not the JSON or hidden reasoning.

SSE heartbeats run while a provider is waiting. They cannot override a client's
absolute timeout. Disconnect cancellation stops ongoing model work when the host
propagates it. Cancellation checkpoints are best effort; an unreleased crashed
lease prevents automatic replay. A new run can reuse the saved memory space.
No exactly-once external tool execution guarantee is claimed.

The session's counters/deadline survive tool handoffs. A later real user message
starts a new invocation budget, retaining the accepted conversation. B's stop
decision is not verified success. Total SSE usage is emitted only when requested
and all A/B calls in that HTTP segment return accounting; it includes both models.
Round/token/time caps are not monetary quotas.

Inspect director state with `GET /v1/dual-lobe/director/{run_id}` and `state:read`.
The state includes the transcript, status/reason, counts, deadline, and pending
tool IDs. Each completed A turn also queues the original observer audit after the
HTTP segment, so director mode does not replace the three earlier observer paths.

## Validation

Run `uv sync --locked --extra dev`, then `uv run --locked pytest tests/unit -q`
for deterministic protocol tests. These use scripted providers and storage fakes.
`uv run --locked pytest -q` additionally uses real Postgres via Docker. A host
without Docker can explicitly set `DUAL_LOBE_TEST_DATABASE_URL` to a **disposable**
database whose name ends in `_test`. Never point tests at the application database.
The CI workflow provisions that isolated Postgres service automatically.

To check cross-app memory interactively, tell normal mode a distinctive project
fact, start a fresh client with the same `--memory-id`, and ask about it without
copying any earlier conversation. Use `/memory` to distinguish what was stored
from what a model actually recalled. For a real tool test, use your existing agent
and confirm that its tool result ID appears in `/director-state` and that a
deliberately failed check is not reported as successful execution.

Current executed results and limits are in [VALIDATION.md](VALIDATION.md).
