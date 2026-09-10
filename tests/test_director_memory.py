"""Real Postgres persistence, isolation, leases, and HTTP tool handoff.

Run against disposable Postgres via Docker or DUAL_LOBE_TEST_DATABASE_URL.
The A/B model replies remain scripted; these are not real-model efficacy tests.
"""
import asyncio
import json
import uuid

import pytest
from sqlalchemy import select, text

from dual_lobe.core.bootstrap import ensure_tenant
from dual_lobe.core.engine import admin_session_factory, tenant_session, dispose_engines
from dual_lobe.director.store import DirectorStore, SessionConflict
from dual_lobe.state import repositories as repo
from dual_lobe.state.memory import MemoryStore

pytestmark = pytest.mark.usefixtures("postgres")


async def seed(name):
    async with admin_session_factory()() as session:
        tenant = await ensure_tenant(session, name, name)
        run = await repo.get_or_create_run(session, tenant.id, str(uuid.uuid4()))
        await session.commit()
        return tenant.id, str(run.id)


async def test_memory_survives_new_connections_and_recalls_older_keyword_matches():
    tid, run = await seed("memory-persistence")
    store = MemoryStore(tid, "project")
    first_call = str(uuid.uuid4())
    await store.record(run, first_call, [{"role": "user", "content": "The deployment codename is orchid."}],
                       [{"role": "assistant", "content": "Recorded the codename."}])
    await store.record(run, first_call, [{"role": "user", "content": "duplicate"}], [])
    await store.notebook("Always include Windows installation steps.")
    for i in range(8):
        await store.record(run, str(uuid.uuid4()), [{"role": "user", "content": f"Unrelated exchange {i}"}],
                           [{"role": "assistant", "content": "An unrelated answer"}])
    await dispose_engines()
    fresh = MemoryStore(tid, "project")
    loaded = await fresh.load([{"role": "user", "content": "Which deployment used orchid?"}], 10000)
    assert "orchid" in loaded.text and "Windows" in loaded.text
    raw = await fresh.inspect(None, 50)
    assert len(raw["entries"]) == 9
    assert "duplicate" not in json.dumps(raw)
    await dispose_engines()


async def test_memory_spaces_and_tenants_are_isolated_and_rls_rejects_foreign_writes():
    tid1, run1 = await seed("memory-owner")
    tid2, run2 = await seed("memory-other")
    await MemoryStore(tid1, "same-name").record(run1, str(uuid.uuid4()),
        [{"role": "user", "content": "Tenant-one-only fact"}], [])
    assert not (await MemoryStore(tid2, "same-name").inspect(None, 10))["entries"]
    assert not (await MemoryStore(tid1, "other-space").inspect(None, 10))["entries"]
    for table in ("director_sessions", "memory_entries", "memory_spaces"):
        async with tenant_session(tid2) as session:
            count = (await session.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :other"),
                                            {"other": tid1})).scalar_one()
            assert count == 0
    async with tenant_session(tid2) as session:
        with pytest.raises(Exception):
            await session.execute(text("INSERT INTO memory_spaces (tenant_id, name, notes) VALUES (:t, 'illegal', '')"), {"t": tid1})
    await dispose_engines()


async def test_session_lease_serializes_requests_and_checkpoint_survives_reconnect():
    tid, run = await seed("director-lease")
    one, two = DirectorStore(tid, run), DirectorStore(tid, run)
    token, initial = await one.acquire(30)
    assert initial == {}
    with pytest.raises(SessionConflict):
        await two.acquire(30)
    await one.save(token, {"status": "waiting_tools", "pending_tools": ["call-1"]}, release=True)
    await dispose_engines()
    token2, saved = await two.acquire(30)
    assert saved["pending_tools"] == ["call-1"]
    with pytest.raises(SessionConflict):
        await one.save(token, {"wrong": True})
    await two.save(token2, saved, release=True)
    assert (await one.read())["status"] == "waiting_tools"
    await dispose_engines()


def test_http_director_tool_resume_and_shared_memory_with_real_database(client, tenant, monkeypatch):
    from tests.helpers import FakeRegistry
    from tests.unit.test_director import ScriptA, ScriptB, CALL, TOOLS
    a = ScriptA([{"tool_calls": [CALL]}, "The actual test result reports FAIL."])
    b = ScriptB([{"action": "stop", "message": "The failed test is still unresolved."}])
    monkeypatch.setattr("dual_lobe.api.chat.get_registry", lambda: FakeRegistry(a, b))
    headers = {"Authorization": "Bearer " + tenant[1], "X-DL-Run-ID": "db-director-" + uuid.uuid4().hex,
               "X-DL-Memory-ID": "db-shared"}
    body = {"model": "lobe-a-director", "tools": TOOLS, "messages": [{"role": "user", "content": "Check the app"}]}
    first = client.post("/v1/chat/completions", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assistant = first.json()["choices"][0]["message"]
    body["messages"] += [assistant, {"role": "tool", "tool_call_id": "call-1", "content": "FAIL: test_login"}]
    second = client.post("/v1/chat/completions", json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert "B (director)" in second.json()["choices"][0]["message"]["content"]
    state = client.get("/v1/dual-lobe/director/" + headers["X-DL-Run-ID"], headers=headers)
    assert state.json()["status"] == "stopped" and state.json()["a_calls"] == 2
    memory = client.get("/v1/dual-lobe/memory/db-shared", headers=headers).json()
    assert len(memory["entries"]) == 2 and "test_login" in json.dumps(memory)
