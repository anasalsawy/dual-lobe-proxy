"""Real Postgres guidance persistence; provider judgments are scripted fixtures."""
import importlib.util
import json
import time
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from dual_lobe.b import context_shadow
from dual_lobe.b.protocol import KnowledgeSnapshot
from dual_lobe.core.engine import admin_session_factory, dispose_engines, tenant_session
from dual_lobe.core.settings import Settings
from dual_lobe.state import repositories as repo
from dual_lobe.state.memory import MemoryStore, attach_observer_notes
from tests.helpers import FakeRegistry
from tests.test_director_memory import seed
from tests.unit.test_observer_repairs import BASE, NOTE

pytestmark = pytest.mark.usefixtures("postgres")


def snapshot(call):
    return KnowledgeSnapshot(source_call=call, source_model="model-b",
                             observed_at=time.time(), notes=[NOTE])


async def test_worker_enrichment_survives_reconnect_and_new_app_without_history(monkeypatch):
    tid, run = await seed("enrichment-worker")
    call = str(uuid.uuid4())
    store = MemoryStore(tid, "windows")
    messages = [{"role": "user", "content": "Windows launcher says bash is not recognized."}]
    replies = [{"role": "assistant", "content": "I will change the application code."}]
    await store.record(run, call, messages, replies)
    await store.notebook("Keep deployment simple.")
    async def review(*args):
        return json.dumps({**BASE, "knowledge_notes": [NOTE]})
    monkeypatch.setattr(context_shadow, "_call_b", review)
    monkeypatch.setattr(context_shadow, "get_registry", lambda: FakeRegistry())
    async with tenant_session(tid) as session:
        result = await context_shadow.run_shadow_cycle(session, {"payload": {
            "run_id": run, "source_call": call, "observed_at": time.time(),
            "memory_space": "windows", "context_text": messages[0]["content"],
            "latest_user_text": messages[0]["content"], "response_text": replies[0]["content"],
        }}, tid)
        await session.commit()
    assert result["ok"]
    await dispose_engines()
    # A second application supplies only its new question and the same tenant/space.
    fresh = MemoryStore(tid, "windows")
    loaded = await fresh.load([{"role": "user", "content": "What should I check for Windows startup?"}], 10000)
    assert "Bash interpreter" in loaded.text and "Keep deployment simple" in loaded.text
    entries = (await fresh.inspect(None, 10))["entries"]
    assert entries[0]["messages"] == messages and entries[0]["responses"] == replies
    guidance = entries[0]["observer_notes"]
    assert guidance["origin"] == "model_generated_guidance" and guidance["source_call"] == call
    assert guidance["source_model"] == "fake" and guidance["observed_at"] > 0
    async with tenant_session(tid) as session:
        assert await repo.list_claims(session, run) == []
        assert await repo.list_evidence(session, run) == []
    await dispose_engines()


async def test_guidance_attachment_and_retrieval_respect_all_memory_boundaries():
    owner, run = await seed("enrichment-owner")
    other, other_run = await seed("enrichment-other")
    call = str(uuid.uuid4())
    await MemoryStore(owner, "project").record(run, call, [{"role": "user", "content": "Windows launch"}], [])
    note = snapshot(call)
    async with tenant_session(owner) as session:
        assert not await attach_observer_notes(session, owner, "wrong-space", run, note)
        assert not await attach_observer_notes(session, owner, "project", other_run, note)
        assert not await attach_observer_notes(session, owner, "project", run, snapshot(str(uuid.uuid4())))
        await session.commit()
    async with tenant_session(other) as session:
        # Even a query that explicitly names the owner cannot cross RLS.
        assert not await attach_observer_notes(session, owner, "project", run, note)
        await session.commit()
    assert (await MemoryStore(owner, "project").inspect(None, 10))["entries"][0]["observer_notes"] is None
    async with tenant_session(owner) as session:
        assert await attach_observer_notes(session, owner, "project", run, note)
        await session.commit()
    for tid, space in ((other, "project"), (owner, "different")):
        assert "Bash interpreter" not in (await MemoryStore(tid, space).load([], 10000)).text
    await dispose_engines()


async def test_old_journal_rows_load_and_saved_guidance_can_be_disabled(monkeypatch):
    tid, run = await seed("enrichment-compatibility")
    store = MemoryStore(tid, "project")
    calls = [str(uuid.uuid4()) for _ in range(3)]
    for call in calls:
        await store.record(run, call, [{"role": "user", "content": "Windows launcher configuration"}], [])
    assert all(e["observer_notes"] is None for e in (await store.inspect(None, 10))["entries"])
    async with tenant_session(tid) as session:
        for call in calls[:2]:
            assert await attach_observer_notes(session, tid, "project", run, snapshot(call))
        await session.commit()
    messages = [{"role": "user", "content": "Windows startup?"}]
    loaded = await store.load(messages, 10000)
    assert loaded.text.count('"origin": "model_generated_guidance"') == 1
    assert len(loaded.text) <= 10000
    no_notes = await store.load(messages, 10000, include_observer_notes=False)
    assert "Bash interpreter" not in no_notes.text
    monkeypatch.setattr("dual_lobe.state.memory.get_settings", lambda: Settings(_env_file=None, context_enrichment_enabled=False))
    disabled = await store.load(messages, 10000)
    assert "Bash interpreter" not in disabled.text and "Windows launcher" in disabled.text
    assert sum(bool(e["observer_notes"]) for e in (await store.inspect(None, 10))["entries"]) == 2
    await dispose_engines()


async def test_additive_migration_preserves_existing_rows():
    spec = importlib.util.spec_from_file_location("notes_migration", "alembic/versions/0004_observer_notes.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with admin_session_factory()() as session:
        # A temporary old-shape table shadows the real table only on this connection.
        await session.execute(text("CREATE TEMP TABLE memory_entries (id integer, payload jsonb) ON COMMIT DROP"))
        await session.execute(text("INSERT INTO memory_entries VALUES (1, '{\"old\": true}')"))
        connection = await session.connection()
        def upgrade(sync_connection):
            with Operations.context(MigrationContext.configure(sync_connection)):
                migration.upgrade()
        await connection.run_sync(upgrade)
        row = (await session.execute(text("SELECT payload, observer_notes FROM memory_entries WHERE id = 1"))).one()
        assert row.payload == {"old": True} and row.observer_notes is None
        await session.commit()
    await dispose_engines()
