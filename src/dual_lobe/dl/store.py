"""Ring-buffer store for dual-lobe mode.

One store per tenant. Never empties, no TTL: writes append, and once the store is
at its cap the oldest entry is discarded to make room for the newest.

Isolated: own tables, tenant-scoped, nothing shared with ``memory_entries`` or
the director session store.
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


class DualLobeStore:
    def __init__(self, tenant_id: int):
        self.tenant_id = tenant_id

    def entries(self):
        return select(DualLobeEntry).where(DualLobeEntry.tenant_id == self.tenant_id)

    async def newest(self, limit: int) -> list[dict]:
        """Newest entries first, capped by ``limit`` rows."""
        if limit <= 0:
            return []
        async with tenant_session(self.tenant_id) as session:
            rows = list((await session.execute(
                self.entries().order_by(DualLobeEntry.seq.desc()).limit(limit)
            )).scalars())
        return [{"seq": r.seq, "kind": r.kind, "body": r.body,
                 "at": r.created_at.isoformat()} for r in rows]

    async def read_slice(self, max_entries: int, max_chars: int) -> list[dict]:
        """Newest entries verbatim until the byte budget is spent.

        This is the injected slice. It is not semantically ranked: dual-lobe mode
        delivers the most recent thinking unconditionally, because ranked
        retrieval can silently omit the entry the next turn depends on.
        """
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
        """Write one entry, then evict the oldest while over cap. Returns its seq."""
        async with tenant_session(self.tenant_id) as session:
            result = await session.execute(insert(DualLobeEntry).values(
                tenant_id=self.tenant_id,
                run_id=uuid.UUID(run_id) if run_id else None,
                kind=kind,
                body=body,
            ).returning(DualLobeEntry.seq))
            seq = result.scalar_one()
            # Ring behaviour: discard from the oldest end only, never the new row.
            evicted = await session.execute(delete(DualLobeEntry).where(
                DualLobeEntry.tenant_id == self.tenant_id,
                DualLobeEntry.seq.in_(
                    select(DualLobeEntry.seq)
                    .where(DualLobeEntry.tenant_id == self.tenant_id)
                    .order_by(DualLobeEntry.seq.desc())
                    .offset(cap)
                ),
            ))
            await session.commit()
        if evicted.rowcount:
            LOG.info("dual-lobe ring buffer evicted %d oldest entrie(s)", evicted.rowcount)
        return seq

    async def size(self) -> int:
        async with tenant_session(self.tenant_id) as session:
            return (await session.execute(
                select(func.count()).select_from(DualLobeEntry)
                .where(DualLobeEntry.tenant_id == self.tenant_id)
            )).scalar_one()

    async def clear(self, run_id: str | None = None) -> int:
        async with tenant_session(self.tenant_id) as session:
            query = delete(DualLobeEntry).where(DualLobeEntry.tenant_id == self.tenant_id)
            if run_id:
                query = query.where(DualLobeEntry.run_id == uuid.UUID(run_id))
            result = await session.execute(query)
            await session.commit()
        return result.rowcount
