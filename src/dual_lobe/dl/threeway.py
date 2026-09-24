"""Persistent user-intervention channel for three-way User/A/B collaboration.

Interventions are stored in the existing run-scoped event ledger so they work
across workers/processes.  The active exchange polls at turn boundaries; this
means a user can interrupt while inference is running and the message is applied
before the next participant turn without relying on process-local queues.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import func, select

from ..core import models
from ..core.engine import tenant_session
from ..state import repositories as repo

THREEWAY_KIND = "dual_lobe_threeway_user"
Recipient = Literal["A", "B", "both"]


def parse_address(content: str, explicit: str | None = None) -> tuple[Recipient, str]:
    """Resolve @A/@B/@both prefixes while allowing an explicit recipient."""
    text = str(content or "").strip()
    if explicit:
        value = str(explicit).strip().lower()
        mapping = {"a": "A", "b": "B", "both": "both", "all": "both"}
        if value not in mapping:
            raise ValueError("recipient must be A, B, or both")
        return mapping[value], text

    lowered = text.lower()
    for prefix, recipient in (("@both", "both"), ("@all", "both"), ("@a", "A"), ("@b", "B")):
        if lowered == prefix or lowered.startswith(prefix + " ") or lowered.startswith(prefix + ":"):
            rest = text[len(prefix):].lstrip(" :\t")
            return recipient, rest
    return "both", text


async def latest_intervention_seq(tenant_id: int, run_id: str) -> int:
    run_uuid = uuid.UUID(str(run_id))
    async with tenant_session(tenant_id) as session:
        value = await session.scalar(select(func.max(models.Event.seq)).where(
            models.Event.tenant_id == tenant_id,
            models.Event.run_id == run_uuid,
            models.Event.kind == THREEWAY_KIND,
        ))
    return int(value or 0)


async def append_intervention(
    tenant_id: int,
    run_id: str,
    *,
    content: str,
    recipient: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    target, text = parse_address(content, recipient)
    if not text:
        raise ValueError("intervention content must be non-empty")
    if len(text) > 8000:
        raise ValueError("intervention content is too long")

    async with tenant_session(tenant_id) as session:
        event = await repo.append_event(
            session,
            THREEWAY_KIND,
            tenant_id,
            run_id=run_id,
            actor="user",
            payload={"to": target, "content": text},
            idempotency_key=idempotency_key,
        )
        await session.commit()
        return {
            "id": str(event.id),
            "seq": int(event.seq),
            "to": target,
            "content": text,
            "run_id": run_id,
        }


async def interventions_since(
    tenant_id: int,
    run_id: str,
    after_seq: int,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    run_uuid = uuid.UUID(str(run_id))
    async with tenant_session(tenant_id) as session:
        rows = list((await session.execute(
            select(models.Event).where(
                models.Event.tenant_id == tenant_id,
                models.Event.run_id == run_uuid,
                models.Event.kind == THREEWAY_KIND,
                models.Event.seq > int(after_seq),
            ).order_by(models.Event.seq.asc()).limit(max(1, min(limit, 200)))
        )).scalars())
    result = []
    for event in rows:
        payload = event.payload or {}
        target, text = parse_address(str(payload.get("content", "") or ""), str(payload.get("to", "both") or "both"))
        result.append({
            "id": str(event.id),
            "seq": int(event.seq),
            "actor": "USER",
            "recipient": target,
            "content": text,
            "at": event.ts.isoformat() if event.ts else None,
        })
    return result
