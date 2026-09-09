"""Health, readiness, models, and the verifier RPC."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..core.engine import admin_session_factory, tenant_session
from ..provider.registry import get_registry
from ..state import repositories as repo
from . import auth
from .schemas import VerifyRequest

router = APIRouter()


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz():
    ledger = "ok"
    try:
        async with admin_session_factory()() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        ledger = "down"
    registry = "ok" if get_registry().target("lobe-a").enabled else "down"
    rc = 200 if ledger == "ok" else 503
    return JSONResponse({"status": "ok" if rc == 200 else "unavailable", "ledger": ledger, "registry": registry}, status_code=rc)


@router.get("/v1/models")
async def models():
    return {
        "object": "list",
        "data": [{"id": m["id"], "object": "model", "owned_by": "local", "logical_model": m["logical_model"], "capabilities": m["capabilities"]} for m in get_registry().models()],
    }


@router.post("/v1/verify")
async def verify(
    body: VerifyRequest,
    request: Request,
    principal: auth.Principal = Depends(auth.require_scope(auth.SCOPE_STATE_READ)),
):
    raise HTTPException(status_code=410, detail=(
        "Automatic file verification is retired. B is tool-free and advisory; "
        "send caller-reported results to /v1/dual-lobe/events."
    ))
