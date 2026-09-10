"""GET /v1/dual-lobe/state/{run_id} — derived current B state for a run."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..core.engine import tenant_session
from ..state import repositories as repo
from . import auth
from .schemas import StateResponse
from ..b.protocol import usable_state
from ..b.channels import completed_memory, memory_status, prepare_context
from ..core.settings import get_settings

router = APIRouter()


@router.get("/v1/dual-lobe/state/{run_id}")
async def run_state(
    run_id: str,
    request: Request,
    principal: auth.Principal = Depends(auth.require_scope(auth.SCOPE_STATE_READ)),
):
    async with tenant_session(principal.tenant_id) as session:
        run = await repo._get_run_or_none(session, principal.tenant_id, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found in tenant")
        internal = str(run.id)
        latest = await repo.latest_b_state(session, internal)
        claims = [repo.claim_to_dict(c) for c in await repo.list_claims(session, run_id=internal, limit=50)]
        evidence = [repo.evidence_to_dict(e) for e in await repo.list_evidence(session, run_id=internal, limit=50)]
        events = [repo.event_to_dict(e) for e in await repo.list_events(session, run_id=internal, limit=25)]
    state_payload = dict(latest["payload"]) if latest else {}
    memory = completed_memory(state_payload)
    settings = get_settings()
    delivery = prepare_context(state_payload, run.current_floor, run.current_attempt, settings)
    state_payload["deception_status"] = delivery.deception_status
    state_payload["deception_level"] = delivery.deception_status if delivery.deception_status in ("GREEN", "YELLOW", "RED") else None
    state_payload["observer_delivery"] = delivery.receipt()
    if state_payload.get("oversight_status") == "reviewed" and not usable_state(
        state_payload, run.current_floor, run.current_attempt,
        settings.b_state_ttl_seconds,
    ):
        state_payload["oversight_status"] = "stale"
    # Memory remains independently usable after claim findings expire or a later
    # review degrades. Inspection never refreshes timestamps or starts a model.
    state_payload["context_memory_status"] = memory_status(
        memory, run.current_floor, run.current_attempt,
        settings.context_memory_ttl_seconds,
    )
    if memory is not None:
        state_payload["context_memory"] = memory.model_dump()
    return StateResponse(
        run_id=internal,
        revision=latest["revision"] if latest else None,
        pulse=latest["pulse"] if latest else None,
        payload=state_payload,
        claims=claims,
        evidence=evidence,
        recent_events=events,
    )
