"""A lease spans one HTTP segment; no DB transaction spans provider calls.

The stored wire transcript prevents SDK retries or mismatched tool results from
silently duplicating a conversation. A crashed in-flight segment is not replayed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update, func
from sqlalchemy.dialects.postgresql import insert

from ..core.engine import tenant_session
from ..core.models import DirectorSession


class SessionConflict(ValueError):
    pass


class DirectorStore:
    def __init__(self, tenant_id: int, run_id: str):
        self.tenant_id = tenant_id
        self.run_id = uuid.UUID(run_id)

    def query(self):
        return select(DirectorSession).where(
            DirectorSession.tenant_id == self.tenant_id,
            DirectorSession.run_id == self.run_id,
        )

    async def read(self) -> dict | None:
        async with tenant_session(self.tenant_id) as session:
            row = (await session.execute(self.query())).scalar_one_or_none()
            if row is None:
                return None
            payload = dict(row.payload)
            if row.lease and row.lease_until and row.lease_until <= datetime.now(timezone.utc):
                payload.update(status="interrupted", reason="lease_expired_without_completion")
            return payload

    async def acquire(self, seconds: float) -> tuple[str, dict]:
        token = uuid.uuid4()
        async with tenant_session(self.tenant_id) as session:
            await session.execute(insert(DirectorSession).values(
                tenant_id=self.tenant_id, run_id=self.run_id,
            ).on_conflict_do_nothing(constraint="uq_director_tenant_run"))
            row = (await session.execute(self.query().with_for_update())).scalar_one()
            now = datetime.now(timezone.utc)
            if row.lease and row.lease_until and row.lease_until > now:
                raise SessionConflict("This director session is already running.")
            if row.lease:
                raise SessionConflict("The previous segment was interrupted. Start with a new X-DL-Run-ID; it will not be replayed.")
            row.lease, row.lease_until = token, now + timedelta(seconds=seconds + 30)
            payload = row.payload
            await session.commit()
        return str(token), payload

    async def save(self, token: str, payload: dict, *, release: bool = False) -> None:
        values = {"payload": payload, "updated_at": func.now()}
        if release:
            values.update(lease=None, lease_until=None)
        async with tenant_session(self.tenant_id) as session:
            result = await session.execute(update(DirectorSession).where(
                DirectorSession.tenant_id == self.tenant_id,
                DirectorSession.run_id == self.run_id,
                DirectorSession.lease == uuid.UUID(token),
            ).values(**values))
            if result.rowcount != 1:
                raise SessionConflict("Director session ownership was lost; no automatic retry.")
            await session.commit()
