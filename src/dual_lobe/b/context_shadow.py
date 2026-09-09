"""One bounded tool-free review. No verifier, tools, holds, or truth verdicts."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..api.limits import _local_sliding
from ..core import redact
from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from ..state import repositories as repo
from . import prompts
from .protocol import Review, ground_review, parse_review

LOG = logging.getLogger("dual_lobe.b.context_shadow")


async def _call_b(target_alias: str, prompt: str) -> dict[str, Any]:
    s = get_settings()
    # One attempt, including the transport layer. No search, tools, repair loop,
    # or model debate. Timeout includes the entire provider operation.
    async with asyncio.timeout(s.b_timeout):
        response = await get_registry().adapter(target_alias).buffered(
            NormalizedRequest(
                messages=[{"role": "system", "content": prompts.B_SYSTEM},
                          {"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=s.b_max_output_tokens,
                timeout=s.b_timeout,
            )
        )
    data = response_dict(response)
    content = data["choices"][0]["message"].get("content") or ""
    return parse_review(content).model_dump()


async def run_shadow_cycle(session: AsyncSession, job: dict[str, Any],
                           tenant_id: int) -> dict[str, Any]:
    # Caller owns the transaction and per-run lock, including marking the job done.
    s = get_settings()
    payload = job.get("payload") or {}
    run_id = str(payload.get("run_id") or job.get("run_id") or "")
    observed_at = float(payload.get("observed_at") or 0)
    latest = await repo.latest_b_state(session, run_id)
    previous = (latest or {}).get("payload") or {}
    now = time.time()

    reason = None
    if not s.b_enabled:
        reason = "disabled"
    elif not 0 <= now - observed_at <= s.b_state_ttl_seconds:
        reason = "expired"
    elif float(previous.get("observed_at", 0)) >= observed_at:
        reason = "superseded"
    elif now - float(previous.get("reviewed_at", 0)) < s.b_cooldown_seconds:
        reason = "cooldown"
    else:
        allowed, _ = await _local_sliding(
            f"b:tenant:{tenant_id}", 60, s.b_rpm_limit, weight=1
        )
        if not allowed:
            reason = "budget"
    if reason:
        await repo.append_event(session, "shadow_skipped", tenant_id, run_id=run_id,
                                actor="lobe-b", payload={"reason": reason})
        return {"ok": False, "skipped": reason}

    events = await repo.list_events(session, run_id=run_id, limit=20)
    # B never sees old B assertions repackaged as independent evidence.
    events_preview = "\n".join(
        f"{e.id} {e.kind}: {json.dumps(redact.redact_payload(e.payload or {}), ensure_ascii=False)[:1000]}"
        for e in reversed(events) if e.kind in ("worker_call", "worker_error", "client_event")
    )
    prompt = prompts.build_cycle_prompt(
        str(payload.get("context_text") or ""),
        str(payload.get("response_text") or ""),
        events_preview,
        json.dumps(previous.get("review") or {}, ensure_ascii=False),
        max_chars=s.max_shadow_input_chars,
    )
    try:
        data = await _call_b("lobe-b", prompt)
        review = ground_review(Review.model_validate(data), prompt)
    except Exception as exc:
        # Do not leak provider errors/keys or mislabel invalid JSON as success.
        LOG.warning("Observer degraded run=%s error_type=%s", run_id, type(exc).__name__)
        await repo.save_b_state(session, tenant_id, run_id, {
            "oversight_status": "degraded", "observed_at": observed_at,
            "reviewed_at": time.time(), "floor_id": payload.get("floor_id", ""),
            "attempt_id": payload.get("attempt_id", 1),
            "reason": type(exc).__name__,
        })
        await repo.append_event(session, "shadow_degraded", tenant_id, run_id=run_id,
                                actor="lobe-b", payload={"error_type": type(exc).__name__})
        return {"ok": False, "degraded": True}

    state = {
        "schema_version": 2,
        "run_id": run_id,
        "source_call": payload.get("source_call", str(payload.get("call_seq", ""))),
        "observed_at": observed_at,
        "reviewed_at": time.time(),
        "floor_id": payload.get("floor_id", ""),
        "attempt_id": payload.get("attempt_id", 1),
        "oversight_status": "reviewed",
        "review": review.model_dump(),
    }
    await repo.save_b_state(session, tenant_id, run_id, state)
    await repo.append_event(session, "b_context_shadow", tenant_id, run_id=run_id,
                            actor="lobe-b", payload={
                                "n_concerns": len(review.concerns),
                                "source_call": state["source_call"],
                                "assessment_only": True,
                            })
    if review.concerns:
        await repo.append_event(session, "oversight", tenant_id, run_id=run_id,
                                actor="lobe-b", payload={
                                    "concerns": [c.model_dump() for c in review.concerns],
                                    "assessment_only": True,
                                })
    return {"ok": True, "concerns": len(review.concerns)}
