"""B worker: polls the outbox, moves rows into idempotent ``b_jobs``, and runs
Lobe-B shadow cycles with per-run serialization and bounded concurrency."""
from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import text

from ..core.engine import admin_session_factory, dispose_engines
from ..core.settings import get_settings
from ..state import repositories as repo
from ..provider.adapters import close_http_client
from .context_shadow import run_shadow_cycle

LOG = logging.getLogger("dual_lobe.b.worker")


def _lock_key(run_id: str) -> int:
    try:
        return uuid.UUID(run_id).int & ((1 << 63) - 1)
    except Exception:  # pragma: no cover - defensive
        return abs(hash(run_id)) & ((1 << 63) - 1)


async def _process_job(job: dict) -> None:
    s = get_settings()
    run_id = str(job.get("run_id") or (job.get("payload") or {}).get("run_id") or "")
    job_key = str(job.get("job_key"))
    tenant_id = int(job.get("tenant_id"))
    if not run_id:
        async with admin_session_factory()() as session:
            await repo.mark_job_failed(session, job_key)
        return
    try:
        async with admin_session_factory()() as session:
            # Per-run serialization: only one B job for a given run processes at a time.
            locked = (await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _lock_key(run_id)}
            )).scalar_one()
            if not locked:
                # Another worker owns this run. Defer without burning an attempt.
                await session.execute(text(
                    "UPDATE b_jobs SET status='pending', lock_until=NULL WHERE job_key=:key"
                ), {"key": job_key})
                await session.commit()
                return
            current = (await session.execute(text(
                "SELECT status FROM b_jobs WHERE job_key=:key"
            ), {"key": job_key})).scalar_one_or_none()
            if current == "done":
                return
            newer = (await session.execute(text(
                "SELECT EXISTS (SELECT 1 FROM b_jobs n JOIN b_jobs j ON j.job_key=:key "
                "WHERE n.run_id=j.run_id AND "
                "COALESCE((n.payload->>'observed_at')::double precision,0) > "
                "COALESCE((j.payload->>'observed_at')::double precision,0))"
            ), {"key": job_key})).scalar_one()
            if newer:
                await repo.mark_job_done(session, job_key)
                return
            await repo.append_event(
                session, "b_job_start", tenant_id,
                run_id=run_id, actor="worker",
                payload={"kind": job.get("kind"), "job_key": job_key},
            )
            result = await run_shadow_cycle(session, job, tenant_id)
            # State, oversight event, job completion, and lock release are atomic.
            await repo.mark_job_done(session, job_key)
            LOG.info("Lobe-B job done run=%s result=%s", run_id, result)
    except Exception:
        LOG.exception("Lobe-B job failed run=%s job_key=%s", run_id, job_key)
        try:
            async with admin_session_factory()() as session:
                await repo.mark_job_failed(session, job_key)
        except Exception:
            LOG.exception("could not mark job failed")
        return


async def _cycle_once(s: object) -> None:
    async with admin_session_factory()() as session:
        await repo.move_outbox_to_jobs(session, limit=16)
    async with admin_session_factory()() as session:
        jobs = await repo.claim_worker_jobs(
            session, limit=_slots(),
            lock_seconds=max(get_settings().worker_lock_seconds, int(get_settings().b_timeout) + 30)
        )
    if jobs:
        await asyncio.gather(*(_process_job(j) for j in jobs))


def _slots() -> int:
    return max(1, get_settings().worker_max_concurrency)


async def run_forever() -> None:
    s = get_settings()
    LOG.info("B worker starting (poll=%ss, concurrency=%d)", s.worker_poll_seconds, s.worker_max_concurrency)
    while True:
        try:
            await _cycle_once(s)
        except asyncio.CancelledError:
            LOG.info("B worker stopping")
            return
        except Exception:
            LOG.exception("B worker cycle error")
        await asyncio.sleep(s.worker_poll_seconds)


def main() -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(message)s")
    async def run():
        try:
            await run_forever()
        finally:
            await close_http_client()
            await dispose_engines()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
