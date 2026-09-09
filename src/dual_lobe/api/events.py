"""POST /v1/dual-lobe/events — external ledger ingestion (CrewAI tools etc.)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy import select
from ..core.idgen import sha256_short

from ..core.engine import tenant_session
from ..core.redact import redact_payload
from ..state import repositories as repo
from . import auth
from .schemas import EventIngest

router = APIRouter()


@router.post("/v1/dual-lobe/events")
async def ingest_event(
    body: EventIngest,
    request: Request,
    principal: auth.Principal = Depends(auth.require_scope(auth.SCOPE_EVENTS_WRITE)),
):
    run_id = body.run_id
    async with tenant_session(principal.tenant_id) as session:
        if run_id:
            run = await repo._get_run_or_none(session, principal.tenant_id, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="run not found in tenant")
            run_id = str(run.id)
        key = (f"client:{principal.tenant_id}:{sha256_short(body.idempotency_key)}"
               if body.idempotency_key else None)
        if key:
            existing = (await session.execute(select(repo.models.Event).where(
                repo.models.Event.idempotency_key == key,
                repo.models.Event.tenant_id == principal.tenant_id,
            ))).scalar_one_or_none()
            if existing:
                return {"status": "ok", "event_id": str(existing.id), "duplicate": True}
        ev = await repo.append_event(
            session,
            "client_event",
            principal.tenant_id,
            run_id=run_id,
            actor="client",
            payload={"source": "client_reported", "kind": body.kind,
                     "actor": body.actor, "data": redact_payload(body.payload)},
            idempotency_key=key,
        )
        await session.commit()
    return {"status": "ok", "event_id": str(ev.id), "run_id": run_id, "kind": body.kind}
