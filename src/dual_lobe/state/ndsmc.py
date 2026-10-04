"""ND-SMC buffer lifecycle: keep the juice, bake it, then shed bytes not rows.

Phase 1/2 (`ingest`) runs at turn end beside C's note capture: the turn is
scored by C's own signals plus the subconscious baseline, cleared rows are
stored with their raw transcript byte-exact, everything else is dropped on
the floor.

Phase 3/4 (`consolidate`) is the nightly dream, and it is non-destructive:
unlockable rows are exported verbatim to a JSONL training file, an optional
training command runs against it, and on success the exported rows become
baked raw. Nothing is deleted. Data leaves the table only through two
separate, conservative paths:

- `compress_space` (the tiered compression): a baked row's verbatim copy is
  distilled to minimum text by an optional local model, stored in a
  distinctly-named compressed tier so a lossy row can never masquerade as
  verbatim, and the raw payload is released to free the bytes.
- `evict_overflow` (the overflowing bowl): while a space is over its rim
  (`ndsmc_buffer_rows * ndsmc_bowl_ratio`), the oldest compressed rows are
  deleted to hold the ceiling. Never raw rows, never unexported rows, never
  the newest fragments. True amnesia only when the bowl actually overflows.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from ..b.subconscious import (emotional_variance, parse_baseline,
                              render_baseline, sensory_vector, style_markers,
                              update_baseline)
from ..core.engine import tenant_session
from ..core.models import MemorySpace, RawInteraction
from ..core.settings import get_settings

LOG = logging.getLogger("dual_lobe.ndsmc")


@dataclass
class BufferStats:
    kept: int
    dropped: bool
    variance: float
    utilization: float  # 0.0-1.0 of the row cap, for the 0%-after-purge metric


async def ingest(tenant_id: int, space: str, raw_transcript: str, *,
                 run_id=None, kind: str = "detail", salience: int = 1,
                 chord: dict | None = None) -> BufferStats:
    """Phase 1 + 2. Stores the turn exactly as given, or drops it whole.

    Never raises into the request path: on any failure the turn is simply
    not kept (the buffer is an optimization over forgetting, never a
    dependency).
    """
    s = get_settings()
    text = raw_transcript or ""
    if not text.strip():
        return BufferStats(0, True, 0.0, 0.0)
    try:
        async with tenant_session(tenant_id) as session:
            # The space may not exist yet (first-ever turn): upsert so the
            # baseline update below has a row to write into.
            await session.execute(insert(MemorySpace).values(
                tenant_id=tenant_id, name=space).on_conflict_do_nothing())
            baseline_vec: list[float] = []
            space_row = (await session.execute(
                select(MemorySpace.subconscious).where(
                    MemorySpace.tenant_id == tenant_id,
                    MemorySpace.name == space))).scalar_one_or_none()
            if space_row:
                baseline_vec = parse_baseline(space_row)
            vector = sensory_vector(text)
            novelty = 0.5 if not baseline_vec else round(1.0 - max(0.0, min(1.0, _cos(vector, baseline_vec))), 3)
            markers = style_markers(text)
            variance = emotional_variance(text, markers=markers, chord=chord,
                                          salience=salience, kind=kind,
                                          novelty=novelty)
            kept = 0
            if variance >= s.ndsmc_variance_floor:
                await session.execute(insert(RawInteraction).values(
                    tenant_id=tenant_id, space=space,
                    run_id=run_id, raw_transcript=text, raw_chars=len(text),
                    style_markers=markers, embedding=vector, variance=variance))
                kept = 1
                # The bowl holds its shape: at the rim, shed oldest fragments.
                await _bowl_trim(session, tenant_id, space, ceiling=_ceiling(s))
            # The baseline tracks ordinariness: weakly for kept peaks so one
            # dramatic day does not redefine normal, normally for the rest.
            new_baseline = update_baseline(baseline_vec, vector,
                                           alpha=s.ndsmc_baseline_alpha
                                           if not kept else s.ndsmc_baseline_alpha / 4)
            await session.execute(
                update(MemorySpace).where(MemorySpace.tenant_id == tenant_id,
                                          MemorySpace.name == space)
                .values(subconscious=render_baseline(new_baseline)))
            await session.commit()
            rows = (await session.execute(
                select(func.count()).select_from(RawInteraction).where(
                    RawInteraction.tenant_id == tenant_id,
                    RawInteraction.space == space))).scalar_one()
            return BufferStats(kept, kept == 0, variance,
                               min(1.0, rows / max(1, s.ndsmc_buffer_rows)))
    except Exception as exc:  # noqa: BLE001 - the buffer never breaks a turn
        LOG.warning("ndsmc ingest failed space=%s: %s", space, type(exc).__name__)
        return BufferStats(0, True, 0.0, 0.0)


def _cos(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _ceiling(s) -> int:
    """Hard row capacity per space."""
    return s.ndsmc_buffer_rows


def _rim(s, capacity: int) -> int:
    """Steady level eviction drains back to (the 90-95% line)."""
    return max(1, int(capacity * s.ndsmc_bowl_ratio))


async def _bowl_trim(session, tenant_id: int, space: str, *,
                     ceiling: int, floor_level: int | None = None) -> int:
    """FIFO eviction: over capacity, delete oldest compressed rows, bit by
    bit, back to the steady line. Compressed rows only - raw rows are never
    deleted for space, however full the bowl gets.
    """
    rows = (await session.execute(
        select(func.count()).select_from(RawInteraction).where(
            RawInteraction.tenant_id == tenant_id,
            RawInteraction.space == space))).scalar_one()
    if rows <= ceiling:
        return 0
    target = floor_level if floor_level is not None else rows - 1  # ingest: one is enough
    over = rows - max(1, target)
    doomed = list((await session.execute(
        select(RawInteraction.id).where(
            RawInteraction.tenant_id == tenant_id,
            RawInteraction.space == space,
            RawInteraction.compressed_at.is_not(None))
        .order_by(RawInteraction.id).limit(over))).scalars())
    if doomed:
        await session.execute(delete(RawInteraction).where(RawInteraction.id.in_(doomed)))
        LOG.info("ndsmc bowl overflow space=%s evicted=%s (capacity=%s steady=%s)",
                 space, len(doomed), ceiling, target)
    return len(doomed)


async def evict_overflow(tenant_id: int, space: str) -> int:
    """The overflow trigger, callable on its own (the dock's disk monitor or
    the nightly job). Delegates to the same trim with hysteresis: drain from
    capacity back to the steady line in one pass."""
    s = get_settings()
    async with tenant_session(tenant_id) as session:
        evicted = await _bowl_trim(session, tenant_id, space,
                                   ceiling=_ceiling(s), floor_level=_rim(s, _ceiling(s)))
        await session.commit()
        return evicted


@dataclass
class ConsolidationResult:
    exported: int
    file: str
    compressed: int
    trained: bool


async def consolidate(tenant_id: int, space: str, *, directory: str | None = None,
                      train_command: str | None = None) -> ConsolidationResult:
    """Phase 3 + 4. Export verbatim -> train (optional) -> bake. No deletion.

    Order still matters: rows are locked (exported) before training runs,
    and only a successful (or absent) training step marks them baked, which
    is what licenses compression later. A crash mid-run leaves rows present
    at whatever tier they had reached - the bowl never loses data here.
    """
    s = get_settings()
    out_dir = Path(directory or s.ndsmc_export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Stable across processes (unlike hash()): the nightly job must append to
    # the same file the previous night wrote.
    tag = hashlib.blake2b(f"{tenant_id}:{space}".encode(), digest_size=8).hexdigest()
    out_file = out_dir / f"{tag}.jsonl"
    exported_ids: list[int] = []
    async with tenant_session(tenant_id) as session:
        rows = list((await session.execute(
            select(RawInteraction).where(
                RawInteraction.tenant_id == tenant_id,
                RawInteraction.space == space,
                RawInteraction.locked.is_(False))
            .order_by(RawInteraction.id))).scalars())
        with out_file.open("a", encoding="utf-8") as fh:
            for row in rows:
                # Verbatim out. The training set IS the transcripts; any
                # rewriting here would be the exact sin the invariant bans.
                # Only verbatim rows export: a compressed row's lossy text
                # never enters a training file.
                fh.write(json.dumps({
                    "schema": "ndsmc-raw/2",
                    "timestamp": row.created_at.isoformat(),
                    "raw_transcript": row.raw_transcript,
                    "speaker_style_vectors": row.style_markers,
                    "emotional_variance_score": row.variance,
                }, ensure_ascii=False) + "\n")
                exported_ids.append(row.id)
        if exported_ids:
            await session.execute(
                update(RawInteraction).where(RawInteraction.id.in_(exported_ids))
                .values(locked=True))
            await session.commit()
    trained = False
    command = train_command or s.ndsmc_train_command
    if command and exported_ids:
        # Operator-provided LoRA/QLoRA pipeline; runs against the exported
        # file only. Non-zero exit aborts the bake - the rows stay raw and
        # unexported for tomorrow's rerun.
        argv = shlex.split(command) + [str(out_file)]
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=s.ndsmc_train_timeout)
        if completed.returncode != 0:
            LOG.error("ndsmc training failed rc=%s; bake skipped, %s rows stay raw",
                      completed.returncode, len(exported_ids))
            return ConsolidationResult(len(exported_ids), str(out_file), 0, False)
        trained = True
    compressed = 0
    if exported_ids:
        async with tenant_session(tenant_id) as session:
            # Bake everything exported, including leftovers from earlier
            # failed nights: they left verbatim copies in the file too.
            await session.execute(
                update(RawInteraction).where(
                    RawInteraction.tenant_id == tenant_id,
                    RawInteraction.space == space,
                    RawInteraction.locked.is_(True))
                .values(baked=True))
            await session.commit()
        compressed = await compress_space(tenant_id, space)
        LOG.info("ndsmc consolidation space=%s exported=%s baked=%s compressed=%s",
                 space, len(exported_ids), len(exported_ids), compressed)
    return ConsolidationResult(len(exported_ids), str(out_file), compressed, trained)


async def compress_space(tenant_id: int, space: str, *,
                         compressor_command: str | None = None) -> int:
    """Tiered compression: distill baked raw rows down to minimum text.

    Lossy by design, which is exactly why it requires the bake: the verbatim
    copy earned its place in the training file before its bytes are traded
    for disk. The result goes to `compressed_transcript` (never back into
    `raw_transcript`) so provenance survives the rewrite. Without a local
    compressor configured this is a no-op: the bowl simply stays heavier,
    longer. External-API distillation of user text is out of policy.
    """
    s = get_settings()
    command = compressor_command or s.ndsmc_compressor_command
    if not command:
        return 0
    async with tenant_session(tenant_id) as session:
        rows = list((await session.execute(
            select(RawInteraction).where(
                RawInteraction.tenant_id == tenant_id,
                RawInteraction.space == space,
                RawInteraction.baked.is_(True),
                RawInteraction.compressed_at.is_(None))
            .order_by(RawInteraction.id)
            .limit(s.ndsmc_compressor_batch))).scalars())
        if not rows:
            return 0
        payload = "\n".join(json.dumps({"id": r.id, "text": r.raw_transcript},
                                       ensure_ascii=False) for r in rows)
        try:
            completed = subprocess.run(shlex.split(command), input=payload,
                                       capture_output=True, text=True,
                                       timeout=s.ndsmc_compressor_timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            LOG.warning("ndsmc compressor failed space=%s: %s", space, type(exc).__name__)
            return 0
        if completed.returncode != 0:
            LOG.warning("ndsmc compressor rc=%s; rows stay verbatim", completed.returncode)
            return 0
        distilled: dict[int, str] = {}
        for line in completed.stdout.splitlines():
            try:
                obj = json.loads(line)
                if isinstance(obj.get("id"), int) and isinstance(obj.get("text"), str):
                    distilled[obj["id"]] = obj["text"]
            except (ValueError, TypeError):
                continue
        done = 0
        for row in rows:
            text = distilled.get(row.id)
            if text is None or not text.strip():
                continue  # compressor skipped this row: keep it verbatim
            await session.execute(
                update(RawInteraction).where(RawInteraction.id == row.id)
                .values(compressed_transcript=text,
                        raw_transcript="", raw_chars=0,  # release the bytes
                        compressed_chars=len(text), compressed_at=func.now()))
            done += 1
        await session.commit()
        if done:
            LOG.info("ndsmc compressed %s baked rows space=%s", done, space)
        return done


@dataclass
class BufferStats:
    kept: int
    dropped: bool
    variance: float
    utilization: float  # 0.0-1.0 of the row capacity
    tiers: tuple[int, int, int] = (0, 0, 0)  # (raw, baked raw, compressed)
    bytes_held: int = 0                      # transcript payload bytes in the bowl


async def buffer_stats(tenant_id: int, space: str) -> BufferStats:
    s = get_settings()
    async with tenant_session(tenant_id) as session:
        rows = (await session.execute(
            select(func.count()).select_from(RawInteraction).where(
                RawInteraction.tenant_id == tenant_id,
                RawInteraction.space == space))).scalar_one()
        raw_only = (await session.execute(
            select(func.count()).select_from(RawInteraction).where(
                RawInteraction.tenant_id == tenant_id,
                RawInteraction.space == space,
                RawInteraction.compressed_at.is_(None),
                RawInteraction.baked.is_(False)))).scalar_one()
        baked = (await session.execute(
            select(func.count()).select_from(RawInteraction).where(
                RawInteraction.tenant_id == tenant_id,
                RawInteraction.space == space,
                RawInteraction.compressed_at.is_(None),
                RawInteraction.baked.is_(True)))).scalar_one()
        held = (await session.execute(
            select(func.coalesce(func.sum(RawInteraction.raw_chars
                                          + RawInteraction.compressed_chars), 0))
            .where(RawInteraction.tenant_id == tenant_id,
                   RawInteraction.space == space))).scalar_one()
    return BufferStats(rows, False, 0.0, min(1.0, rows / max(1, _ceiling(s))),
                       (raw_only, baked, rows - raw_only - baked), int(held))