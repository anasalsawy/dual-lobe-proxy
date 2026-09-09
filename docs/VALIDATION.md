# Validation record

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
