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
import sys

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


# ---------- DB: dream cycle (non-destructive) ----------

def test_consolidation_exports_verbatim_and_keeps_everything(tenant, tmp_path):
    """The bowl retains: export -> bake -> (no compressor) -> rows persist."""
    tid, _ = tenant
    raw = _messy()

    async def _run():
        await ndsmc.ingest(tid, "ndsmc-dream", raw, kind="affect",
                           salience=3, chord={"sadness": 0.9})
        return await ndsmc.consolidate(tid, "ndsmc-dream",
                                       directory=str(tmp_path))

    result = asyncio.run(_run())
    assert result.exported == 1 and result.compressed == 0 and not result.trained
    lines = [json.loads(line) for line in
             open(result.file, encoding="utf-8").read().splitlines()]
    assert lines[0]["schema"] == "ndsmc-raw/2"
    assert lines[0]["raw_transcript"] == raw          # verbatim out
    assert lines[0]["emotional_variance_score"] >= 0.60
    # Post-dream retention: the row is still here, verbatim, and baked.
    stored = asyncio.run(_raw(tid, "ndsmc-dream"))
    assert stored == raw
    tiers = asyncio.run(_tiers(tid, "ndsmc-dream"))
    assert tiers == {"raw": 0, "baked": 1, "compressed": 0}


def test_failed_training_blocks_the_bake(tenant, tmp_path):
    """Phase 3 fails -> compression stays unlicensed; rows remain raw."""
    tid, _ = tenant

    async def _run():
        await ndsmc.ingest(tid, "ndsmc-crash", _messy(), kind="affect",
                           salience=3, chord={"fear": 0.9})
        return await ndsmc.consolidate(tid, "ndsmc-crash",
                                       directory=str(tmp_path),
                                       train_command="false")

    result = asyncio.run(_run())
    assert result.compressed == 0 and not result.trained
    assert asyncio.run(_count(tid, "ndsmc-crash")) == 1
    assert asyncio.run(_tiers(tid, "ndsmc-crash")) == {"raw": 1, "baked": 0, "compressed": 0}


def test_compression_only_touches_baked_and_never_fakes_verbatim(tenant, tmp_path):
    """A fake local distiller shrinks baked rows into the compressed tier;
    the raw payload is released and the lossy text is honestly labelled."""
    tid, _ = tenant
    raw = _messy()
    script = tmp_path / "distill.py"
    script.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    o = json.loads(line)\n"
        "    print(json.dumps({'id': o['id'], 'text': o['text'][:24]}))\n")
    cmd = f"{sys.executable} {script}"

    async def _run():
        await ndsmc.ingest(tid, "ndsmc-squeeze", raw, kind="affect",
                           salience=3, chord={"sadness": 0.9})
        await ndsmc.consolidate(tid, "ndsmc-squeeze", directory=str(tmp_path),
                                train_command="true")
        n = await ndsmc.compress_space(tid, "ndsmc-squeeze", compressor_command=cmd)
        stats = await ndsmc.buffer_stats(tid, "ndsmc-squeeze")
        return n, stats

    n, stats = asyncio.run(_run())
    assert n == 1
    assert stats.tiers == (0, 0, 1)  # raw, baked_raw, compressed
    assert stats.bytes_held == len(raw[:24])  # raw payload released, lossy counted
    assert asyncio.run(_raw(tid, "ndsmc-squeeze")) == ""  # verbatim released
    assert asyncio.run(_compressed_text(tid, "ndsmc-squeeze")) == raw[:24]
    # The compressed row never re-enters a training export as if verbatim:
    second = asyncio.run(ndsmc.consolidate(tid, "ndsmc-squeeze", directory=str(tmp_path)))
    assert second.exported == 0


def test_bowl_evicts_oldest_compressed_never_raw(tenant, monkeypatch):
    """Overflow: over capacity the oldest compressed fragments fall FIFO down
    to the steady line; raw rows are never evicted however full the bowl."""
    tid, _ = tenant
    from dual_lobe.core.settings import get_settings
    from datetime import datetime, timezone
    s = get_settings()
    monkeypatch.setattr(s, "ndsmc_buffer_rows", 10)
    monkeypatch.setattr(s, "ndsmc_bowl_ratio", 0.8)  # steady line: 8

    async def _seed(n_compressed, n_raw):
        from dual_lobe.core.engine import tenant_session
        from dual_lobe.core.models import RawInteraction
        from sqlalchemy import insert, delete
        async with tenant_session(tid) as session:
            await session.execute(delete(RawInteraction).where(
                RawInteraction.tenant_id == tid, RawInteraction.space == "ndsmc-bowl"))
            now = datetime.now(timezone.utc)
            for i in range(n_compressed):
                await session.execute(insert(RawInteraction).values(
                    tenant_id=tid, space="ndsmc-bowl", raw_transcript="",
                    compressed_transcript=f"lossy {i}", compressed_chars=8,
                    compressed_at=now, locked=True, baked=True))
            for i in range(n_raw):
                await session.execute(insert(RawInteraction).values(
                    tenant_id=tid, space="ndsmc-bowl",
                    raw_transcript=f"verbatim raw {i}", raw_chars=14))
            await session.commit()

    async def _survivors():
        from dual_lobe.core.engine import tenant_session
        from dual_lobe.core.models import RawInteraction
        from sqlalchemy import select
        async with tenant_session(tid) as session:
            rows = (await session.execute(
                select(RawInteraction).where(
                    RawInteraction.tenant_id == tid,
                    RawInteraction.space == "ndsmc-bowl")
                .order_by(RawInteraction.id))).scalars().all()
        return [r.compressed_transcript or r.raw_transcript for r in rows]

    # Under the rim: nothing moves.
    asyncio.run(_seed(6, 4))  # 10 rows == capacity
    assert asyncio.run(ndsmc.evict_overflow(tid, "ndsmc-bowl")) == 0
    assert len(asyncio.run(_survivors())) == 10

    # Overflow: 8 compressed + 4 raw = 12 > capacity 10 -> drain to 8 by
    # dropping the 4 OLDEST compressed; every raw row survives.
    asyncio.run(_seed(8, 4))
    assert asyncio.run(ndsmc.evict_overflow(tid, "ndsmc-bowl")) == 4
    survivors = asyncio.run(_survivors())
    assert survivors == ["lossy 4", "lossy 5", "lossy 6", "lossy 7"] + \
        [f"verbatim raw {i}" for i in range(4)]

    # Full bowl with zero compressed: still deletes nothing. Raw is never
    # sacrificed for space; compression is the only door to eviction.
    asyncio.run(_seed(0, 14))
    assert asyncio.run(ndsmc.evict_overflow(tid, "ndsmc-bowl")) == 0
    assert len(asyncio.run(_survivors())) == 14


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


async def _compressed_text(tid, space):
    from sqlalchemy import select
    from dual_lobe.core.engine import tenant_session
    from dual_lobe.core.models import RawInteraction
    async with tenant_session(tid) as session:
        return (await session.execute(
            select(RawInteraction.compressed_transcript).where(
                RawInteraction.tenant_id == tid,
                RawInteraction.space == space))).scalars().first()


async def _tiers(tid, space):
    stats = await ndsmc.buffer_stats(tid, space)
    raw, baked, compressed = stats.tiers
    return {"raw": raw, "baked": baked, "compressed": compressed}
