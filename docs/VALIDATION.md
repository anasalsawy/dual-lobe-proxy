# Validation record

## v0.5 observer repairs and enrichment — 2026-09-10

Starting point: main `5023d944ec5c627d12bf744944affcfbf48553da`. Its existing
[CI run 34441080808](https://github.com/anasalsawy/dual-lobe-proxy/actions/runs/34441080808)
passed 149 tests, including 22 real Postgres cases. That is baseline evidence,
not a result for the changes below.

Current local Python 3.12 validation:

- **158 unit tests passed**; 184 cases collected, including 26 database cases.
- New regressions cover secret redaction through normal observation/B input,
  B-selected GREEN/YELLOW/RED delivery, default GREEN with separate review
  status, source-call/age attribution, disabling saved feedback, retry accounting
  and deadline, fresh state-read transactions, director latest-user wiring,
  optional knowledge validation, host-tool schema/reservation fail-open behavior,
  and strict UX probe completion handling.
- The labeled B evaluation driver passed with a scripted provider through the
  actual production prompt/validation path. The eight-case offline prompt preview
  ran successfully. These are not real-model quality measurements.
- Existing five-check offline observer demo, source/test/migration/tool
  compilation, lock consistency, installed dependency compatibility, entrypoint
  shell syntax and whitespace checks passed. Runtime dependencies are unchanged.
- The new Postgres cases cover worker-to-journal guidance attachment, reconnect
  retrieval, tenant/space/run/call boundaries, suppression of saved notes,
  old-record compatibility and additive migration 0004 on pre-existing rows.
  Their execution is pending CI at this checkpoint; collection is not a pass.

No real provider credentials or running deployment are configured in the authoring
environment. The live three-arm comparison has not run. Both assessments now
label previous live reports as historical and distinguish delivery tests from
semantic efficacy. The [evaluation guide](UX_ASSESSMENT_WITH_VS_WITHOUT.md)
specifies retained redacted inputs, outputs, settings, source IDs and timing.

GREEN means **no deception detected** and is the default; B selects the color from
the whole available record; there is no UNKNOWN
color. A failed/unavailable review retains an explicit operational status and
does not become a completed review. No test establishes verified honesty or
the absence of every design flaw. Normal mode adds no B model wait, while the
existing database/prompt costs still preclude a zero-latency guarantee.

Apply migration 0004 before updated gateway/worker code. The Compose initializer
runs it once before those services; existing journal/state data need no reset.

## v0.4 director mode and shared persistent memory — 2026-09-10

Local Python 3.12 validation:

- **109 deterministic tests passed**, including all 67 previous unit tests.
- **131 cases collected**: 109 deterministic and 22 Postgres integration cases.
- Visible A/B alternation, single outer completion identity/termination, fragmented
  parallel tool handoff, exact result matching, full and delta history, duplicate
  request prevention, SDK null-field compatibility, persistent invocation budgets,
  malformed B/A output, cancellation and ASGI send-failure cleanup were exercised.
- Memory tests cover a second app sending no previous chat history, the default
  shared space, bounded valid context, notebook preservation, selection headers,
  and committing memory before the successful terminal event.
- Locked offline install built v0.4.0. No new Python runtime dependency was added.
- Source/test/migration compilation, whitespace checks, lock validation, installed
  client help, and the existing five-check offline observer demo passed.
- New migrations 0002 and 0003 successfully generated Postgres SQL with Alembic
  from the existing 0001 revision. SQL generation is not a database execution test.

The local Postgres test was attempted and failed during fixture setup because
Docker access raised `PermissionError(1, 'Operation not permitted')`. No database
assertion ran locally. Native Postgres is absent; package-manager setup was also
blocked by this environment's process permissions.

The new [CI workflow](../.github/workflows/ci.yml) runs the complete suite against
an isolated Postgres 18 service. Its database cases test new-connection memory
retention, old-record lookup, namespace/tenant isolation, SQL leases, migrations,
and a full HTTP tool handoff. [CI run 34434262978](https://github.com/anasalsawy/dual-lobe-proxy/actions/runs/34434262978)
completed successfully on the implementation commit 7443e431: **131 passed** on
Python 3.12.3, with a real Postgres 18 service. The model replies remain scripted.
The install, offline observer demo, and compilation steps also passed in CI.

No live A/B provider conversation has run in this checkout: no provider key or
running proxy is configured here. These tests do not measure model honesty,
judgment, recall accuracy, or provider compatibility. The installed client and
the [director/memory test guide](DIRECTOR_AND_MEMORY.md) provide the live test.

The earlier release records below are historical, not additional v0.4 results.

## v0.3 memory and separate request paths — 2026-09-10

Executed in a fresh Python 3.12 environment created for the updated checkout:

- `uv sync --locked --offline --extra dev`: built and installed v0.3.0, 53 packages.
- `uv run --locked --offline pytest tests/unit -q`: **67 passed**.
- Collection: **85 cases**, including 18 Postgres integration cases.
- `python -m dual_lobe.demo`: all five plumbing checks passed. It explicitly uses
  hand-authored fixtures and displays the composed A request; no model was called.
- Compilation, `git diff --check`, `uv lock --check --offline`, `uv pip check`,
  Compose YAML/environment wiring, and the entrypoint shell syntax check passed.
- `python -m dual_lobe.client --help`: passed; the installed live test client is
  available, but a real provider conversation has not run in this environment.

New tests exercise independent memory/claim delivery and switches, memory versions,
expiry and floor/attempt scope, preservation after a failed review without age
renewal, clearing resolved findings, v2 read compatibility, bounded JSON, automatic
reload on successive A calls while B is busy, original-message preservation,
context receipts, state inspection, and incomplete-stream handling in the client.

The Postgres test was attempted again (`pytest tests/test_worker.py -x -q`), but
failed in fixture setup because Docker daemon access raised
`PermissionError(1, 'Operation not permitted')`. No application/database assertion
ran in that attempt. This update uses the existing tenant-scoped state table and
requires no new migration, but actual database/RLS behavior remains unverified here.

There is no configured provider API key or running proxy deployment in this
checkout. Real A/B behavior, provider role support, effective tunnel-vision relief,
claim-check accuracy, and latency remain to be tested with the user. The ready-to-run
client and first conversation scenario are in [THREE_PATH_SETUP.md](THREE_PATH_SETUP.md).

The older v0.2 validation record follows for continuity.

## Executed checks

Validated locally on Python 3.12 using the resolved dependencies in `uv.lock`:

- `python -m pytest tests/unit -q`: **49 passed**.
- `python -m compileall -q src tests`: passed.
- `git diff --check`: passed.
- Test collection: 67 cases, including 18 database-backed integration cases.
- `uv lock --check` and `uv pip check`: lock is current; installed requirements compatible.
- Compose YAML parsing/service wiring and `sh -n docker/entrypoint.sh`: passed.

The focused tests cover early SSE/ASGI delivery, post-response observation
persistence ordering, multiple tool fragments, usage preservation, interrupted
streams, invalid SSE, cancellation cleanup, empty responses, no-tool/single-call
B invocation, strict JSON/quote validation, bounded input/output, original-goal
retention, stale/wrong-floor/degraded notes, bypass behavior, unsupported fields,
text-only input, alias routing, tenant-setting transaction lifetime and budgets.

These are deterministic software tests with mocked providers and repositories.
They do not establish semantic accuracy of a real B model or actual SQL isolation.

## Blocked or not executed

The Postgres integration suite was attempted, but Testcontainers could not access
the Docker daemon: `PermissionError(1, 'Operation not permitted')`. This is an
environment setup failure, not a passing test or a demonstrated application
assertion failure. No permission bypass was attempted.

The following remain unverified:

- Real Postgres migrations, row-level security and pooled tenant isolation.
- Concurrent database outbox/job behavior and crash recovery.
- Docker image build and full Compose startup on Python 3.11.
- Real provider model availability, credentials, option compatibility and latency.
- Real-model contradiction detection, false-positive rate and blocker recovery.
- Production security, ingress limits, retained data volume and horizontal scaling.

Run `uv sync --locked --extra dev` and `uv run --locked pytest -q` on a machine
with permitted Docker access before deployment. Then exercise the configured
providers and follow the evaluation plan in
[RESEARCH_AND_DESIGN.md](RESEARCH_AND_DESIGN.md).

## Dependency reproducibility

`uv.lock` includes the resolved development environment. `requirements.lock`
is an exported, pinned, hashed runtime dependency list consumed by the Dockerfile.
This improves repeatability; it is not a vulnerability audit or a guarantee that
upstream releases, the base image or build tooling are free of problems.
