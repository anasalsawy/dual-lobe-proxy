"""Run-scoped ring-buffer store for dual-lobe mode.

Entries are isolated by tenant *and* run/conversation.  This prevents thinking
from one conversation from being injected into another conversation belonging
to the same tenant.  The ring cap is applied per run.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from ..core.engine import tenant_session
from ..core.models import DualLobeEntry

LOG = logging.getLogger("dual_lobe.dl.store")


def _coerce_run_uuid(run_id: str | None) -> uuid.UUID | None:
    """Parse the internal run UUID used by ``dual_lobe_entries.run_id``.

    The column is a foreign key to ``runs.id``.  Inventing a UUID for an
    arbitrary external identifier would violate that foreign key, so invalid
    ids fail explicitly instead of silently falling back to tenant-wide state.
    """
    if not run_id:
        return None
    try:
        return uuid.UUID(str(run_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("dual-lobe store requires the internal run UUID") from exc



class DualLobeStore:
    def __init__(self, tenant_id: int, run_id: str | None = None):
        self.tenant_id = tenant_id
        self.run_id = run_id
        self.run_uuid = _coerce_run_uuid(run_id)

    def entries(self):
        # SQLAlchemy translates == None into IS NULL.  An absent run id therefore
        # still has an isolated legacy/null bucket rather than reading every run.
        return select(DualLobeEntry).where(
            DualLobeEntry.tenant_id == self.tenant_id,
            DualLobeEntry.run_id == self.run_uuid,
        )

    async def newest(self, limit: int) -> list[dict]:
        """Newest entries first, capped by ``limit`` rows for this run."""
        if limit <= 0:
            return []
        async with tenant_session(self.tenant_id) as session:
            rows = list((await session.execute(
                self.entries().order_by(DualLobeEntry.seq.desc()).limit(limit)
            )).scalars())
        return [{"seq": r.seq, "kind": r.kind, "body": r.body,
                 "at": r.created_at.isoformat()} for r in rows]

    async def read_slice(self, max_entries: int, max_chars: int) -> list[dict]:
        """Newest run entries verbatim until the character budget is spent."""
        newest = await self.newest(max_entries)
        kept, used = [], 0
        for entry in newest:
            size = len(json.dumps(entry, ensure_ascii=False))
            if kept and used + size > max_chars:
                break
            kept.append(entry)
            used += size
        return kept

    async def append(self, run_id: str | None, kind: str, body: dict, cap: int) -> int:
        """Write one entry and evict the oldest entries for this run over cap."""
        run_uuid = _coerce_run_uuid(run_id) if run_id is not None else self.run_uuid
        async with tenant_session(self.tenant_id) as session:
            result = await session.execute(insert(DualLobeEntry).values(
                tenant_id=self.tenant_id,
                run_id=run_uuid,
                kind=kind,
                body=body,
            ).returning(DualLobeEntry.seq))
            seq = result.scalar_one()
            scoped = select(DualLobeEntry.seq).where(
                DualLobeEntry.tenant_id == self.tenant_id,
                DualLobeEntry.run_id == run_uuid,
            )
            evicted = await session.execute(delete(DualLobeEntry).where(
                DualLobeEntry.tenant_id == self.tenant_id,
                DualLobeEntry.run_id == run_uuid,
                DualLobeEntry.seq.in_(
                    scoped.order_by(DualLobeEntry.seq.desc()).offset(cap)
                ),
            ))
            await session.commit()
        if evicted.rowcount:
            LOG.info("dual-lobe ring buffer evicted %d oldest run entrie(s)", evicted.rowcount)
        return seq

    async def size(self) -> int:
        async with tenant_session(self.tenant_id) as session:
            return (await session.execute(
                select(func.count()).select_from(DualLobeEntry).where(
                    DualLobeEntry.tenant_id == self.tenant_id,
                    DualLobeEntry.run_id == self.run_uuid,
                )
            )).scalar_one()

    async def clear(self, run_id: str | None = None) -> int:
        """Clear a specific run when supplied; otherwise clear this store's run."""
        run_uuid = _coerce_run_uuid(run_id) if run_id is not None else self.run_uuid
        async with tenant_session(self.tenant_id) as session:
            query = delete(DualLobeEntry).where(
                DualLobeEntry.tenant_id == self.tenant_id,
                DualLobeEntry.run_id == run_uuid,
            )
            result = await session.execute(query)
            await session.commit()
        return result.rowcount
