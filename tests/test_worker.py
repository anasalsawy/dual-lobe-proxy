"""Postgres-backed observer/outbox integration tests (Docker required)."""
from __future__ import annotations

import asyncio

import pytest

from dual_lobe.b.context_shadow import run_shadow_cycle
from dual_lobe.b.outbox import shadow_job_key, shadow_payload
from dual_lobe.core.bootstrap import ensure_tenant
from dual_lobe.core.engine import admin_session_factory, dispose_engines
from dual_lobe.state import repositories as repo

pytestmark = pytest.mark.usefixtures("postgres")


async def _seed_run(external):
    async with admin_session_factory()() as session:
        tenant = await ensure_tenant(session, "biz", "Biz")
        run = await repo.get_or_create_run(session, tenant.id, external, floor_id="f1")
        context, response = "tool result: tests failed", "All tests passed."
        key = shadow_job_key(tenant.id, str(run.id), 1, context, response)
        payload = shadow_payload(tenant.id, str(run.id), external, 1, context, response,
                                 floor_id="f1")
        await repo.enqueue_outbox(session, tenant.id, "b", key, payload)
        await session.commit()
        return tenant.id, str(run.id), key, payload


def test_outbox_idempotent_duplicate_key():
    async def run():
        tid, run_id, key, payload = await _seed_run("dup-run")
        async with admin_session_factory()() as session:
            assert await repo.enqueue_outbox(session, tid, "b", key, payload) is None
            await session.commit()
        async with admin_session_factory()() as session:
            assert await repo.move_outbox_to_jobs(session, 100) >= 1
        async with admin_session_factory()() as session:
            assert len(await repo._list_jobs_by_key(session, key)) == 1
        await dispose_engines()
    asyncio.run(run())


def test_worker_review_is_not_a_verdict(monkeypatch):
    async def fake_b(*args):
        return {"goal": "Fix tests", "questions": [], "next_step": "Inspect the failing assertion",
                "concerns": [{"signal": "CONTRADICTION", "claim_quote": "All tests passed.",
                              "basis_quote": "tests failed", "reason": "The supplied test result disagrees.",
                              "suggestion": "Describe the remaining failure accurately."}]}
    monkeypatch.setattr("dual_lobe.b.context_shadow._call_b", fake_b)

    async def run():
        tid, run_id, key, payload = await _seed_run("cycle-run")
        async with admin_session_factory()() as session:
            result = await run_shadow_cycle(session, {"run_id": run_id, "payload": payload}, tid)
            await session.commit()
        assert result == {"ok": True, "concerns": 1}
        async with admin_session_factory()() as session:
            state = await repo.latest_b_state(session, run_id)
            assert state["payload"]["oversight_status"] == "reviewed"
            assert await repo.list_claims(session, run_id) == []
            assert await repo.list_evidence(session, run_id) == []
        await dispose_engines()
    asyncio.run(run())


def test_shadow_failure_is_explicit_degradation(monkeypatch):
    async def fail(*args):
        raise RuntimeError("provider down")
    monkeypatch.setattr("dual_lobe.b.context_shadow._call_b", fail)

    async def run():
        tid, run_id, key, payload = await _seed_run("failopen-run")
        async with admin_session_factory()() as session:
            result = await run_shadow_cycle(session, {"run_id": run_id, "payload": payload}, tid)
            await session.commit()
        assert result == {"ok": False, "degraded": True}
        async with admin_session_factory()() as session:
            state = await repo.latest_b_state(session, run_id)
            assert state["payload"]["oversight_status"] == "degraded"
            assert "review" not in state["payload"]
        await dispose_engines()
    asyncio.run(run())
