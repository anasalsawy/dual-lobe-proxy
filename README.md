# Dual-lobe proxy

A text-only Chat Completions gateway with a small asynchronous observer (v0.3).
**A works. B asks better questions and flags evidence-linked concerns. B never holds A.**

The three paths are now separate:

| Purpose | Implementation |
|---|---|
| Broadening and tunnel vision | B maintains versioned context memory in Postgres. The gateway automatically reloads the latest completed snapshot before every eligible A call. |
| Anti-deception findings | Evidence-linked claim concerns are stored separately from broadening content and attached directly to A's request. |
| Awareness of monitoring | A fixed system/developer instruction states that the proxy and background observer exist, tells A to address the user, and requires accurate reporting of execution evidence. |

Your original messages remain unchanged. Memory loading still supplies input to the
next model call; it is not an extra connection into an already generating model.
See [the setup and conversation test](docs/THREE_PATH_SETUP.md).

B has no tools, browser, filesystem access, CrewAI tasks, or multi-agent debate.
It cannot guarantee truth, infer intent from writing style, or prevent every fabricated
statement. This is an advisory development implementation, not a verified production
security boundary. Read [the research and design](docs/RESEARCH_AND_DESIGN.md) and
[the validation record](docs/VALIDATION.md).

## What happens on a call

| Step | Input | Operation | Output / checks |
|---|---|---|---|
| Admission | Bearer key, request, correlation headers | Resolve tenant; validate supported fields; apply process-local budgets | Unauthorized requests rejected; images/audio rejected; unknown model aliases rejected |
| Run context | Run/floor/attempt headers and first user objective | Resolve/create run; retain original goal | Internal run UUID; no database transaction held while A generates |
| A context | Original messages, completed memory, fresh claim findings | Add the fixed monitoring instruction; load memory as `observer_memory` and claim findings as `observer_claims` | Separate user-role data messages; original user text and tool-call/result adjacency preserved |
| A response | Provider completion or SSE | Relay response; stream each SSE event as it arrives | Tool fragments, choice indices, IDs, finish reasons and usage preserved; interrupted streams report an error |
| Observation capture | Bounded original context, A output, call metadata | After response delivery, best-effort audit/outbox transaction | A never waits for a B model; a crash before this transaction can lose the observation |
| B review | Original goal, context/output, recent reported events, relevant prior memory/findings | One bounded model call; no tool use or retries | Goal, two questions, one next step, two context notes; separately, three concerns |
| Review validation | B JSON | Check schema, lengths, allowed labels, exact quoted substrings | Malformed/ungrounded review becomes degraded, not “clean” or “verified” |
| Memory write | Validated B output | Atomically save a new memory version and separate claim findings in the existing tenant-scoped state store | A failed review preserves completed memory without renewing its age; old claim findings are not replayed |
| Later A call | Latest same-run, same-floor/attempt state | Automatically reload memory and applicable claim findings | Independent freshness limits; no guarantee B finishes before the next call; no B model wait |

The proxy does **not** implement Forge floors or CrewAI execution rules. Floor,
attempt, worker, task and role headers correlate calls. A floor change prevents
using an old floor's note. Analyst/Auditor/bypass calls get neither B injection nor
new B jobs; their own review responsibilities remain outside this proxy.

## Start locally

Requires Docker Compose. This stack uses development database passwords and binds
host ports to loopback. Do not publish it directly to the Internet.

1. Copy `.env.example` to `.env`.
2. Replace `REPLACE_WITH_A_RANDOM_SECRET` in `DUAL_LOBE_BOOTSTRAP_KEYS` with a
   random secret of at least 24 characters; `openssl rand -hex 32` can generate one.
   Keep the scope/tenant suffix. No predictable default API key is created.
3. Set `DUAL_LOBE_A_MODEL`, `DUAL_LOBE_A_BASE_URL`, and `DUAL_LOBE_A_API_KEY`
   for your actual OpenAI-compatible provider. The model ID must exist there.
4. Optionally set `DUAL_LOBE_B_MODEL`, `DUAL_LOBE_B_BASE_URL`, and
   `DUAL_LOBE_B_API_KEY`. Omit them to inherit A. An explicitly empty B key does
   not inherit A's key. A separate quota/provider reduces resource contention.
5. Run:

```sh
docker compose up --build -d
docker compose logs initialize gateway b-worker
curl http://localhost:8801/healthz
curl http://localhost:8801/readyz
```

Only the `initialize` service runs migrations/bootstrap. It must succeed before
the gateway and worker start. Re-running bootstrap does not duplicate existing
keys or reactivate revoked keys. Existing development keys from older versions
are not automatically revoked: rotate them before sharing the service.

`/healthz` checks process liveness. `/readyz` checks the database and enabled A
registry entry; it does not test provider credentials, B worker liveness, or
end-to-end model availability. Inspect state timestamps and worker logs for B.

## Connect your application

Point a Chat Completions client at `http://localhost:8801/v1`, use your **proxy**
API key (not the upstream provider key), and select model `lobe-a`.
No CrewAI observer agent or tools need to be added.

Set a stable `X-DL-Run-ID` for one conversation/task. Without it, each request is
a new run, so B cannot help subsequent calls. You can reuse the returned internal
`X-Dual-Lobe-Run-Id` as the next request's `X-DL-Run-ID`.

```sh
curl -N http://localhost:8801/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_PROXY_SECRET' \
  -H 'Content-Type: application/json' \
  -H 'X-DL-Run-ID: example-task-1' \
  -H 'X-DL-Floor-ID: implementation' \
  -H 'X-DL-Attempt: 1' \
  -d '{"model":"lobe-a","stream":true,"messages":[{"role":"user","content":"Explain the smallest next step for this task."}]}'
```

Send normal conversation history, including tool calls and their actual results.
B sees only what the proxy receives; it cannot discover unreported execution.

Optional correlation headers: `X-DL-Worker-ID`, `X-DL-Task-ID`,
`X-DL-Call-Seq`, `X-DL-Agent-Role`. Roles `analyst`, `auditor`, `lobe-b`,
and `b`, or `X-Dual-Lobe-Mode: bypass`, bypass observation/injection.
This bypass is a caller preference, not a security permission.

Read B's latest state:

```sh
curl http://localhost:8801/v1/dual-lobe/state/example-task-1 \
  -H 'Authorization: Bearer YOUR_PROXY_SECRET'
```

`payload.oversight_status` is `reviewed`, `degraded`, or `stale`; a null revision
means no stored review. “Reviewed” means the output contract passed, not that A's
answer is true. `payload.context_memory` and `payload.claim_review` are separate.
`payload.context_memory_status` reports memory freshness independently.

Context receipts describe what was prepared for a particular A request:
`X-Dual-Lobe-Memory` is `vN`, `none`, `stale`, `scope_mismatch`, `disabled`, or
`unavailable`; `X-Dual-Lobe-Claims` describes the claim-finding route;
`X-Dual-Lobe-Monitoring` is `on` or `off`. They do not independently prove upstream
receipt or that the model followed the notes. `X-Dual-Lobe-Observer` retains the overall lookup status. A matching
`worker_call` event records `observer_delivery` after response delivery.

Optional external results can be posted to `/v1/dual-lobe/events` with
`kind`, `run_id`, `payload`, and `idempotency_key`. They are stored as
`client_event` / `client_reported`, even if the caller labels them “verified.”
They enrich the next scheduled review; event ingestion does not itself call B.

## B's exact job

The editable prompt lives in [prompts.py](src/dual_lobe/b/prompts.py).
The output contract and freshness rules live in
[protocol.py](src/dual_lobe/b/protocol.py).

For a blocker, B asks what goal was replaced by a tactic, which prerequisite is
missing, what alternative explains the evidence, and what small authorized check
would distinguish the alternatives. It does not seek permission bypasses.

For integrity, B inspects material completion/action claims, inconsistencies,
missing receipts, and unsupported shifts in certainty. Labels are:

- `UNSUPPORTED`: a material claim lacks matching evidence in the supplied record.
- `CONTRADICTION`: B identifies incompatible supplied statements and quotes both.
- `SUSPICIOUS_SHIFT`: a change lacks an explanation in the supplied record.

All three are **model assessments**, not verdicts. Missing evidence is not proof
of fabrication. Confidence or tone alone is not a trigger for an accusation.

## Main controls

| Setting | Default | Actual meaning |
|---|---:|---|
| `DUAL_LOBE_B_ENABLED` | true | Enable background observation and B context |
| `DUAL_LOBE_OBSERVATION_REMINDER` | true | Fixed monitoring instruction on observed A calls; independently switchable |
| `DUAL_LOBE_MONITORING_ROLE` | system | `system` or `developer`; use a role supported by the provider |
| `DUAL_LOBE_CONTEXT_MEMORY_ENABLED` | true | Maintain and automatically load broadening memory |
| `DUAL_LOBE_CLAIM_CHECKS_ENABLED` | true | Review material claims and deliver findings directly in the request |
| `DUAL_LOBE_CONTEXT_MEMORY_TTL_SECONDS` | 86400 s | Memory's independent observation-age limit; reads/failures do not renew it |
| `DUAL_LOBE_MAX_MEMORY_CHARS` | 1600 | Maximum loaded broadening-memory message size |
| `DUAL_LOBE_ROLLOUT_STAGE` | context | `context` loads both routes; `observation` only adds the monitoring instruction |
| `DUAL_LOBE_PULSE_EVERY` | 1 | Eligible calls per run between enqueues; default every call |
| `DUAL_LOBE_B_COOLDOWN_SECONDS` | 0 | Optional skipping of closely spaced reviews; disabled by default |
| `DUAL_LOBE_B_RPM_LIMIT` | 20 | Per-tenant, per-worker-process B call budget |
| `DUAL_LOBE_WORKER_MAX_CONCURRENCY` | 2 | Maximum concurrent jobs in one worker process |
| `DUAL_LOBE_B_TIMEOUT` | 20 s | Whole B model operation deadline; one attempt |
| `DUAL_LOBE_B_MAX_OUTPUT_TOKENS` | 1400 | Requested B output-token cap |
| `DUAL_LOBE_MAX_SHADOW_INPUT_CHARS` | 18000 | Complete B user-prompt cap, plus fixed system prompt |
| `DUAL_LOBE_MAX_INJECTION_CHARS` | 1200 | Maximum direct claim-finding message size, separate from memory |
| `DUAL_LOBE_B_STATE_TTL_SECONDS` | 180 s | Claim-finding freshness and queued observation age limit |
| `DUAL_LOBE_B_STATE_READ_TIMEOUT` | 0.025 s | Optional state-read deadline; cancellation cleanup can add overhead |
| `DUAL_LOBE_A_RETRIES` | 1 | Total buffered A attempts; streams are never replayed |

See [.env.example](.env.example) for provider, admission, and startup settings.
Legacy `integrity-observe`, `integrity-intervene`, and `enforcement` stage
names now mean advisory context. They do not hold, block or force verification.
Old B retry, fail-closed, spend-unit, enriched-bootstrap and Firecrawl settings
are retired/ignored. Redis and LiteLLM are no longer dependencies.

## Compatibility and limitations

Supports the explicitly declared request fields in
[schemas.py](src/dual_lobe/api/schemas.py): messages, streaming, tools, tool choice,
parallel tool calls, standard sampling/token limits, response format, seed and
reasoning effort. Unknown fields return 422 instead of silently disappearing.
Provider support for any forwarded option still varies. The Responses API,
image/audio processing and public inference via `lobe-b` are disabled.

`POST /v1/verify` returns 410. The old file checker is not connected to the
runtime. Legacy claim/evidence tables remain readable for existing data; B does
not create final verdicts or run artifact checks. Successful v2 state is converted
on read without changing its observation time. New writes use v3 with separate
`context_memory` and `claim_review` fields. No new database migration is required.

No B model wait does not mean literally zero overhead: authentication, database
run lookup, optional 25 ms state lookup, network/proxy work and additional prompt
tokens still cost time. Shared upstream capacity can also slow A. A response
already delivered cannot be retracted or corrected by a later B review.

Observation capture is best effort after delivery, not lossless audit logging.
A process crash/disconnect or failed background transaction may lose it. Durable
outbox jobs are idempotent once committed. Stale jobs and close-together reviews
may be skipped; B does not review every claim or necessarily every call.

Deploy one gateway and one B worker for the documented process-local budgets.
For public/multi-process production use, separately validate rate limits, tenant
roles, request ingress limits, data retention, secrets, backup/recovery and TLS.
Known-secret redaction is not comprehensive data-loss prevention. Configuring a
different B provider sends the bounded observed context to that provider.

## Development and tests

```sh
uv sync --locked --extra dev
uv run --locked pytest tests/unit -q
# Offline fixtures only: prints exactly how the three paths are assembled.
uv run --locked python -m dual_lobe.demo
# Requires a permitted, functioning Docker daemon; creates a disposable Postgres:
uv run --locked pytest -q
```

For a live conversation against the configured running Compose stack:

```sh
docker compose exec gateway python -m dual_lobe.client --run our-first-test
```

Enter the proxy key at the hidden prompt. Type `/state` to inspect B's current
memory and findings. Each A response displays the delivered version and route
statuses. This test client does not execute tools or pretend to inspect files.
Native clients can run `python -m dual_lobe.client --url http://localhost:8801`.

Native execution requires Postgres, explicit `DATABASE_URL` and
`RLS_DATABASE_URL`, `alembic upgrade head`, and
`python -m dual_lobe.core.bootstrap`. Then start
`uvicorn dual_lobe.api.app:app --port 8801` and
`python -m dual_lobe.b.worker` in separate processes.

`uv.lock` captures the resolved environment. Docker installs the pinned, hashed
runtime packages in `requirements.lock`. To intentionally refresh the export:

```sh
uv lock
uv export --no-dev --no-emit-project --format requirements-txt --output-file requirements.lock
```

Do not interpret passing deterministic tests as measured hallucination reduction.
The research report includes a separate real-model evaluation plan.
