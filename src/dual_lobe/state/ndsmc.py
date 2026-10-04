"""ND-SMC buffer lifecycle: keep the juice, export it, then forget it.

Phase 1/2 (`ingest`) runs at turn end beside C's note capture: the turn is
scored by C's own signals plus the subconscious baseline, cleared rows are
stored with their raw transcript byte-exact, everything else is dropped on
the floor. Phase 3/4 (`consolidate`) is the weekend dream: unlockable rows
are exported verbatim to a JSONL training file, an optional training
command runs against that file, and only after both succeed are the
exported rows deleted. Purge without export is a bug we refuse to ship:
destruction is gated on the data having left the building intact.
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
                    run_id=run_id, raw_transcript=text,
                    style_markers=markers, embedding=vector, variance=variance))
                kept = 1
                # Bounded footprint: when the cap is reached, the oldest
                # unlocked rows fall first (locked rows already left a copy in
                # an export file, but deleting them before consolidation would
                # risk losing the only DB-side proof of the export).
                over = (await session.execute(
                    select(func.count()).select_from(RawInteraction).where(
                        RawInteraction.tenant_id == tenant_id,
                        RawInteraction.space == space))).scalar_one() - s.ndsmc_buffer_rows
                if over > 0:
                    stale = list((await session.execute(
                        select(RawInteraction.id).where(
                            RawInteraction.tenant_id == tenant_id,
                            RawInteraction.space == space,
                            RawInteraction.locked.is_(False))
                        .order_by(RawInteraction.id).limit(over))).scalars())
                    if stale:
                        await session.execute(delete(RawInteraction)
                                              .where(RawInteraction.id.in_(stale)))
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


@dataclass
class ConsolidationResult:
    exported: int
    file: str
    purged: int
    trained: bool


async def consolidate(tenant_id: int, space: str, *, directory: str | None = None,
                      train_command: str | None = None) -> ConsolidationResult:
    """Phase 3 + 4. Export verbatim -> train (optional) -> purge exported only.

    Order is the whole point: rows are locked (exported) before anything is
    deleted, and deletion happens only after the training step reports
    success. A crash mid-run leaves data locked-but-present, never gone.
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
                fh.write(json.dumps({
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
        # file only. Non-zero exit aborts the purge - data stays locked.
        argv = shlex.split(command) + [str(out_file)]
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=s.ndsmc_train_timeout)
        if completed.returncode != 0:
            LOG.error("ndsmc training failed rc=%s; purge skipped, %s rows retained",
                      completed.returncode, len(exported_ids))
            return ConsolidationResult(len(exported_ids), str(out_file), 0, False)
        trained = True
    purged = 0
    if exported_ids:
        async with tenant_session(tenant_id) as session:
            purged = (await session.execute(
                delete(RawInteraction).where(
                    RawInteraction.tenant_id == tenant_id,
                    RawInteraction.space == space,
                    RawInteraction.locked.is_(True)))).rowcount or 0
            await session.commit()
        LOG.info("ndsmc consolidation space=%s exported=%s purged=%s trained=%s",
                 space, len(exported_ids), purged, trained)
    return ConsolidationResult(len(exported_ids), str(out_file), purged, trained)


async def buffer_stats(tenant_id: int, space: str) -> BufferStats:
    s = get_settings()
    async with tenant_session(tenant_id) as session:
        rows = (await session.execute(
            select(func.count()).select_from(RawInteraction).where(
                RawInteraction.tenant_id == tenant_id,
                RawInteraction.space == space))).scalar_one()
    return BufferStats(rows, False, 0.0, min(1.0, rows / max(1, s.ndsmc_buffer_rows)))