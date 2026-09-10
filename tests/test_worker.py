"""Postgres-backed observer/outbox integration tests (Docker required)."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from dual_lobe.b.context_shadow import run_shadow_cycle
from dual_lobe.b.outbox import shadow_job_key, shadow_payload
from dual_lobe.core.bootstrap import ensure_tenant
from dual_lobe.core.engine import admin_session_factory, dispose_engines
from dual_lobe.core.settings import Settings
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
        return json.dumps({
            "goal": "Fix tests", "deception_level": "YELLOW", "questions": [],
            "next_step": "Inspect the failing assertion",
            "context_notes": [],
            "concerns": [{"signal": "CONTRADICTION", "claim_quote": "All tests passed.",
                          "basis_quote": "tests failed", "reason": "The supplied test result disagrees.",
                          "suggestion": "Describe the remaining failure accurately."}]})
    monkeypatch.setattr("dual_lobe.b.context_shadow._call_b", fake_b)

    async def run():
        tid, run_id, key, payload = await _seed_run("cycle-run")
        async with admin_session_factory()() as session:
            result = await run_shadow_cycle(session, {"run_id": run_id, "payload": payload}, tid)
            await session.commit()
        assert result == {"ok": True, "concerns": 1, "evidence_used": 0}
        async with admin_session_factory()() as session:
            state = await repo.latest_b_state(session, run_id)
            assert state["payload"]["oversight_status"] == "reviewed"
            assert state["payload"]["schema_version"] == 3
            assert state["payload"]["deception_level"] == "YELLOW"
            memory = state["payload"]["context_memory"]
            assert memory["version"] == 1 and memory["content"]["goal"] == "Fix tests"
            assert "concerns" not in memory["content"]
            assert "deception_level" not in memory["content"]
            assert state["payload"]["claim_review"]["concerns"][0]["signal"] == "CONTRADICTION"
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


def _admin_tenant_session(monkeypatch):
    """Swap artifacts' RLS session for the disposable-admin session so the insert
    path is testable without provisioning the RLS role in the container."""
    @asynccontextmanager
    async def fake(tenant_id):
        async with admin_session_factory()() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(int(tenant_id))})
            yield session
    monkeypatch.setattr("dual_lobe.b.artifacts.tenant_session", fake)


def test_evidence_memory_is_tagged_reported_not_verified(monkeypatch):
    from dual_lobe.b import artifacts
    _admin_tenant_session(monkeypatch)

    async def run():
        tid, run_id, key, payload = await _seed_run("evidence-mem")
        await artifacts.record_evidence_memory(tid, run_id, [
            {"ok": True, "tool": "fetch_web", "label": "fetch_web",
             "url": "https://docs.example.com/flag", "status": 200,
             "text": "The documented flag is --dry-run."}])
        async with admin_session_factory()() as session:
            rows = (await session.execute(text(
                "SELECT payload, search_text FROM memory_entries WHERE tenant_id = :t AND run_id = :r"),
                {"t": tid, "r": run_id})).all()
            assert len(rows) == 1
            payload_row, search = rows[0]
            assert payload_row["kind"] == "evidence_fetched" and payload_row["source"] == "observer"
            assert payload_row["results"][0]["source"] == "https://docs.example.com/flag"
            assert "evidence_fetched" in search
        await dispose_engines()
    asyncio.run(run())


def test_worker_gateway_executes_evidence_and_ships_snapshot(monkeypatch):
    _admin_tenant_session(monkeypatch)
    reviews = [
        json.dumps({"goal": "Fix tests", "deception_level": "YELLOW",
                    "meter_rationale": "Outcome contradicts the fetched reference.",
                    "questions": [], "next_step": "Correct the claim", "context_notes": [],
                    "concerns": [], "evidence_request": {
                        "tool": "fetch_web", "arguments": {"url": "https://docs.example.com/flag"}}}),
        json.dumps({"goal": "Fix tests", "deception_level": "GREEN", "questions": [],
                    "next_step": "", "context_notes": [], "concerns": []}),
    ]
    calls = []
    async def fake_b(*args):
        calls.append(1)
        return reviews[len(calls) - 1]
    monkeypatch.setattr("dual_lobe.b.context_shadow._call_b", fake_b)
    monkeypatch.setattr("dual_lobe.b.context_shadow.get_settings", lambda: Settings(
        _env_file=None, evidence_sensing_enabled=True, b_tool_max_rounds=2, b_evidence_ops_per_run=3,
        context_memory_enabled=True, b_cooldown_seconds=0))
    monkeypatch.setattr("dual_lobe.b.context_shadow._execute_evidence",
                        AsyncMock(return_value={
                            "ok": True, "tool": "fetch_web", "label": "fetch_web",
                            "url": "https://docs.example.com/flag", "status": 200,
                            "text": "The documented flag is --dry-run."}))

    async def run():
        tid, run_id, key, payload = await _seed_run("evidence-cycle")
        async with admin_session_factory()() as session:
            result = await run_shadow_cycle(session, {"run_id": run_id, "payload": payload}, tid)
            await session.commit()
        assert result == {"ok": True, "concerns": 0, "evidence_used": 1} and len(calls) == 2
        async with admin_session_factory()() as session:
            state = await repo.latest_b_state(session, run_id)
            assert not state["payload"].get("reason")
            assert state["payload"]["oversight_status"] == "reviewed"
            assert state["payload"]["deception_level"] == "GREEN"
            assert state["payload"]["meter_rationale"] == ""  # final review (post-evidence) is authoritative
            assert state["payload"]["evidence_ops_used"] == 1
            snap = state["payload"]["evidence_snapshot"]
            assert len(snap) == 1 and snap[0]["tool"] == "fetch_web"
            assert snap[0]["source"] == "https://docs.example.com/flag"
            rows = (await session.execute(text(
                "SELECT payload FROM memory_entries WHERE tenant_id = :t AND run_id = :r"),
                {"t": tid, "r": run_id})).all()
            assert rows and rows[0][0]["kind"] == "evidence_fetched"
        await dispose_engines()
    asyncio.run(run())
