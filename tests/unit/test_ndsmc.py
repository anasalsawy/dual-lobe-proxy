"""ND-SMC: the juice filter, verbatim storage, baseline drift, and the
nightly dream (export -> optional train -> purge-exported-only).

Unit tests pin the scorer and the raw-data invariant without a database;
DB tests use the shared tenant fixture. Generative replay and LoRA training
live outside the proxy (operator pipeline behind ndsmc_train_command); the
tests here pin that a failing training command blocks the purge.
"""
from __future__ import annotations

import asyncio
import json

from dual_lobe.b.subconscious import (emotional_variance, parse_baseline,
                                      render_baseline, sensory_vector,
                                      style_markers, update_baseline)
from dual_lobe.state import ndsmc
from dual_lobe.state.memory import MemoryStore as mem_store


# ---------- Phase 2: the filter, pure ----------

def test_routine_small_talk_scores_boring():
    plain = "please summarize the quarterly numbers in a table"
    score = emotional_variance(plain, novelty=0.2, salience=1, kind="detail")
    assert score < 0.55  # below the production floor: never stored


def test_raw_vulnerability_scores_juicy():
    text = ("W-well, I don't know... it's just... super crazy, you know? "
            "I'm terrified I'll lose her too... forget it.")
    score = emotional_variance(text, novelty=0.8, salience=3, kind="affect",
                               chord={"sadness": 0.8, "fear": 0.7})
    assert score >= 0.55


def test_style_markers_describe_without_touching_text():
    raw = "W-well I DON'T know... it's just... super crazy, you know? I mean, like, whatever"
    markers = style_markers(raw)
    assert "hesitant" in markers and "informal" in markers
    assert "shouting" in markers
    assert raw.count("...") == 2  # markers label the text; the text is untouched


def test_variance_is_bounded_and_zero_for_empty():
    assert emotional_variance("") == 0.0
    assert 0.0 <= emotional_variance("LOL WOW!!! absolutely NOTHING matters",
                                     novelty=1.0, salience=3,
                                     kind="affect") <= 1.0


# ---------- the sensory gut vector + baseline ----------

def test_sensory_vector_is_deterministic_and_never_a_query_handle():
    v = sensory_vector("the room smells like rain and coffee")
    assert v == sensory_vector("the room smells like rain and coffee")
    assert len(v) > 0


def test_baseline_moves_toward_ordinary_and_resists_peaks():
    ordinary = sensory_vector("thanks ok sounds good see you later")
    another = sensory_vector("alright see you tomorrow then")
    peak = sensory_vector("I LOVED it... it was... the best day, okay?!")
    base = update_baseline([], ordinary)                 # first turn seeds it
    drifted = update_baseline(base, another, alpha=0.3)
    assert drifted != base
    # a dramatic turn must not redefine normal: weak alpha keeps distance high
    before = 1 - _cos(base, peak)
    after = 1 - _cos(update_baseline(base, peak, alpha=0.3 / 4), peak)
    assert after < before  # moved a little
    assert after > 0.5 * before  # but far less than a full step


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_baseline_roundtrip_survives_garbage():
    vec = sensory_vector("ok then see you tomorrow")
    back = parse_baseline(render_baseline(vec))
    assert len(back) == len(vec)
    assert abs(sum(x * y for x, y in zip(vec, back)) - 1.0) < 1e-3
    assert parse_baseline("not json") == []
    assert parse_baseline("{\"surprise\": 1}") == []
    assert parse_baseline("") == []


# ---------- DB: ingestion keeps raw or drops whole ----------

def _messy():
    return ("D-dude I can't... I can't stop thinking about it, she actually "
            "left, and I just... whatever, forget it!!")


def test_low_variance_turn_is_never_stored(tenant):
    tid, _ = tenant

    async def _run():
        return await ndsmc.ingest(tid, "ndsmc-boring",
                                  "ok thanks. summarize the report please.",
                                  kind="detail", salience=1)

    stats = asyncio.run(_run())
    assert stats.dropped and stats.kept == 0
    assert asyncio.run(_count(tid, "ndsmc-boring")) == 0


def test_high_variance_turn_is_stored_byte_exact(tenant):
    tid, _ = tenant
    raw = _messy()

    async def _run():
        stats = await ndsmc.ingest(tid, "ndsmc-juice", raw, kind="affect",
                                   salience=3, chord={"sadness": 0.9, "fear": 0.6})
        rows = await ndsmc.buffer_stats(tid, "ndsmc-juice")
        return stats, rows

    stats, rows = asyncio.run(_run())
    assert stats.kept == 1 and not stats.dropped and stats.variance >= 0.55
    assert rows.kept == 1
    stored = asyncio.run(_raw(tid, "ndsmc-juice"))
    assert stored == raw  # the invariant: byte-exact, stutters and all


def test_baseline_updates_even_when_the_turn_is_dropped(tenant):
    """Ordinary turns teach what ordinary is, even though they are discarded."""
    tid, _ = tenant

    async def _run():
        await ndsmc.ingest(tid, "ndsmc-base", "ok thanks, sounds good, see you",
                           kind="detail", salience=1)
        from sqlalchemy import select
        from dual_lobe.core.engine import tenant_session
        from dual_lobe.core.models import MemorySpace
        async with tenant_session(tid) as session:
            blob = (await session.execute(select(MemorySpace.subconscious).where(
                MemorySpace.tenant_id == tid,
                MemorySpace.name == "ndsmc-base"))).scalar_one_or_none()
        return blob

    blob = asyncio.run(_run())
    assert parse_baseline(blob)  # baseline exists despite zero stored rows


# ---------- DB: dream cycle ----------

def test_consolidation_exports_verbatim_then_purges_only_exported(tenant, tmp_path):
    tid, _ = tenant
    raw = _messy()

    async def _run():
        await ndsmc.ingest(tid, "ndsmc-dream", raw, kind="affect",
                           salience=3, chord={"sadness": 0.9})
        return await ndsmc.consolidate(tid, "ndsmc-dream",
                                       directory=str(tmp_path))

    result = asyncio.run(_run())
    assert result.exported == 1 and result.purged == 1 and not result.trained
    lines = [json.loads(line) for line in
             open(result.file, encoding="utf-8").read().splitlines()]
    assert lines[0]["raw_transcript"] == raw          # verbatim out
    assert lines[0]["emotional_variance_score"] >= 0.55
    assert asyncio.run(_count(tid, "ndsmc-dream")) == 0  # buffer at zero


def test_failed_training_blocks_the_purge(tenant, tmp_path):
    """Phase 3 fails -> Phase 4 must not run. Data survives a bad dream."""
    tid, _ = tenant

    async def _run():
        await ndsmc.ingest(tid, "ndsmc-crash", _messy(), kind="affect",
                           salience=3, chord={"fear": 0.9})
        return await ndsmc.consolidate(tid, "ndsmc-crash",
                                       directory=str(tmp_path),
                                       train_command="false")

    result = asyncio.run(_run())
    assert result.purged == 0
    assert asyncio.run(_count(tid, "ndsmc-crash")) == 1


async def _count(tid, space):
    from sqlalchemy import func, select
    from dual_lobe.core.engine import tenant_session
    from dual_lobe.core.models import RawInteraction
    async with tenant_session(tid) as session:
        return (await session.execute(
            select(func.count()).select_from(RawInteraction).where(
                RawInteraction.tenant_id == tid,
                RawInteraction.space == space))).scalar_one()


async def _raw(tid, space):
    from sqlalchemy import select
    from dual_lobe.core.engine import tenant_session
    from dual_lobe.core.models import RawInteraction
    async with tenant_session(tid) as session:
        return (await session.execute(
            select(RawInteraction.raw_transcript).where(
                RawInteraction.tenant_id == tid,
                RawInteraction.space == space))).scalars().first()

# ---------- C as the sole memory channel ----------

async def _seed_run(tid) -> str:
    """record() is FK-bound to a real run; create one and return its id."""
    from dual_lobe.core.engine import tenant_session
    from dual_lobe.core.models import Run
    from sqlalchemy import insert
    import uuid as _uuid
    async with tenant_session(tid) as session:
        run_id = (await session.execute(
            insert(Run).values(tenant_id=tid, external_run_id=_uuid.uuid4().hex)
            .returning(Run.id))).scalar_one()
        await session.commit()
    return str(run_id)

def test_journal_is_not_injected_but_c_notes_are(tenant, monkeypatch):
    """With the sole channel on, A recalls only C's material.

    The mechanical journal still records every turn (C's raw material and
    the operator's audit trail); it simply stops being A's memory.
    """
    tid, _ = tenant
    from dual_lobe.core.settings import get_settings
    monkeypatch.setattr(get_settings(), "c_sole_memory_channel", True)

    async def _run():
        rid = await _seed_run(tid)
        store = mem_store(tid, "sole-channel")
        await store.record(rid,
                           "22222222-2222-2222-2222-222222222222",
                           [{"role": "user", "content": "deploy the helmchart v2"}],
                           [{"role": "assistant", "content": "deployed helmchart v2"}])
        await store.annotate(None, [{
            "kind": "callback", "anchor": "the launch",
            "quote": "we shipped it on the first try, remember?",
            "feeling": "proud", "chord": {"joy": 0.8}, "salience": 2}])
        return await store.load(
            [{"role": "user", "content": "the helmchart again please"}], 8000)

    loaded = asyncio.run(_run())
    assert "helmchart" not in (loaded.text or "")
    assert loaded.callbacks and "shipped it on the first try" in loaded.callbacks


def test_dual_channel_mode_restores_journal_injection(tenant, monkeypatch):
    tid, _ = tenant
    from dual_lobe.core.settings import get_settings
    monkeypatch.setattr(get_settings(), "c_sole_memory_channel", False)

    async def _run():
        rid = await _seed_run(tid)
        store = mem_store(tid, "dual-channel")
        await store.record(rid,
                           "44444444-2222-2222-2222-222222222222",
                           [{"role": "user", "content": "remember the passphrase zebra42"}],
                           [{"role": "assistant", "content": "noted zebra42"}])
        return await store.load(
            [{"role": "user", "content": "the passphrase zebra42 again"}], 8000)

    loaded = asyncio.run(_run())
    assert "zebra42" in (loaded.text or "")
