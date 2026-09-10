"""Read/search stored history and edit the shared notebook without another app."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..core.redact import redact_payload
from ..state.memory import MemoryStore, validate_space
from . import auth

router = APIRouter()


def checked(space):
    try:
        return validate_space(space)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


class Notebook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str = Field(max_length=4000)


@router.get("/v1/dual-lobe/memory/{space}")
async def inspect(space: str, before: int | None = Query(default=None, ge=1),
                  limit: int = Query(default=10, ge=1, le=50), q: str = Query(default="", max_length=500),
                  principal: auth.Principal = Depends(auth.require_scope(auth.SCOPE_STATE_READ))):
    return redact_payload(await MemoryStore(principal.tenant_id, checked(space)).inspect(before, limit, q))


@router.put("/v1/dual-lobe/memory/{space}/notebook")
async def notebook(space: str, body: Notebook,
                   principal: auth.Principal = Depends(auth.require_scope(auth.SCOPE_INFERENCE_INVOKE))):
    try:
        await MemoryStore(principal.tenant_id, checked(space)).notebook(body.notes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"space": space, "saved": True}
