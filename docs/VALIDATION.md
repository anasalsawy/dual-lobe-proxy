# Validation record

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
