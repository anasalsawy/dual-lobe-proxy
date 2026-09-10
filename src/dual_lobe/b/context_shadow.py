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
from . import artifacts as evidence_artifacts
from . import fetch as evidence_fetch
from .channels import completed_memory, memory_status, reviewed_state
from .protocol import Review, ground_review, parse_review, usable_state

LOG = logging.getLogger("dual_lobe.b.context_shadow")


async def _call_b(target_alias: str, prompt: str) -> str:
    s = get_settings()
    instructions = prompts.B_SYSTEM
    if not s.context_memory_enabled:
        instructions += "\nContext memory is disabled: return empty goal, questions, next_step and context_notes."
    if not s.claim_checks_enabled:
        instructions += "\nClaim checking is disabled: return an empty concerns array."
    if not s.deception_meter_enabled:
        instructions += "\nThe deception meter is disabled: return deception_level GREEN with an empty meter_rationale."
    # B never executes; only the gateway senses via evidence_request. Timeout
    # includes the entire provider operation.
    async with asyncio.timeout(s.b_timeout):
        response = await get_registry().adapter(target_alias).buffered(
            NormalizedRequest(
                messages=[{"role": "system", "content": instructions},
                          {"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=s.b_max_output_tokens,
                timeout=s.b_timeout,
            )
        )
    data = response_dict(response)
    return data["choices"][0]["message"].get("content") or ""


CORRECTION_NOTE = (
    "\n\nYour previous output was rejected: {error}.\n"
    "Return ONLY the JSON contract above, complete and valid, with no prose or fences."
)


async def _obtain_review(target_alias: str, prompt: str) -> Review:
    """One bounded review with at most one corrective worker-side retry.

    Retries only parse/grounding failures (ValueError) because a re-ask can fix
    them. Transport errors (429/5xx/timeouts) are honest provider failures and
    must not double-fire against limits; they degrade immediately.
    """
    raw = await _call_b(target_alias, prompt)
    try:
        return ground_review(parse_review(raw), prompt)
    except ValueError as exc:
        LOG.warning("Observer review invalid once; corrective retry error_type=%s",
                    type(exc).__name__)
        # Runs only between turns; A is never blocked on this second attempt.
        raw = await _call_b(target_alias, prompt + CORRECTION_NOTE.format(error=str(exc)[:300]))
        return ground_review(parse_review(raw), prompt)


async def _execute_evidence(request, settings) -> dict:
    """Execute one validated evidence_request in code; B never runs tools."""
    tool = request.tool
    if tool == "fetch_web":
        url = (request.arguments or {}).get("url", "")
        if not url:
            return {"ok": False, "tool": tool, "label": "", "error": "missing url argument"}
        return await evidence_fetch.fetch_url(url)
    if tool == "read_artifact":
        path = (request.arguments or {}).get("path", "")
        if not path:
            return {"ok": False, "tool": tool, "label": "", "error": "missing path argument"}
        return await evidence_artifacts.read_artifact(path)
    return {"ok": False, "tool": tool, "label": "", "error": "unsupported tool"}


async def _review_with_sensing(base_prompt: str, rebuild, previous: dict) -> tuple[Review, list[dict], int]:
    """One bounded review, then up to N gateway-executed sensing rounds, all off
    the request critical path. Budgets are deterministic counters, not model calls.
    """
    s = get_settings()
    review = await _obtain_review("lobe-b", base_prompt)
    if not s.evidence_sensing_enabled:
        return review, [], int(previous.get("evidence_ops_used", 0))
    used = int(previous.get("evidence_ops_used", 0))
    results: list[dict] = []
    for _ in range(s.b_tool_max_rounds):
        request = review.evidence_request
        if request is None or used >= s.b_evidence_ops_per_run:
            break
        result = await _execute_evidence(request, s)
        used += 1
        results.append(result)
        sensed = evidence_artifacts.sensed_text(results)
        if not sensed:
            break
        try:
            review = await _obtain_review("lobe-b", rebuild(sensed))
        except Exception as exc:
            LOG.warning("Observer sensing re-review failed run=%s error_type=%s; keeping last review",
                        (previous.get("run_id") or ""), type(exc).__name__)
            break
    return review, results, used


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
    if not s.b_enabled or not (s.context_memory_enabled or s.claim_checks_enabled
                               or s.deception_meter_enabled):
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
    # Only relevant, still-valid prior suggestions may influence the new cycle.
    # Keep memory's independent age/scope even if the latest claim review failed.
    memory = completed_memory(previous)
    floor, attempt = str(payload.get("floor_id", "")), int(payload.get("attempt_id", 1))
    prior_memory = memory.content.model_dump() if s.context_memory_enabled and memory_status(
        memory, floor, attempt, s.context_memory_ttl_seconds,
    ) == "available" else None
    prior_claims = None
    if s.claim_checks_enabled and usable_state(previous, floor, attempt, s.b_state_ttl_seconds):
        prior_claims = previous.get("claim_review") or {"concerns": (previous.get("review") or {}).get("concerns", [])}

    def rebuild(sensed: str = "") -> str:
        return prompts.build_cycle_prompt(
            str(payload.get("context_text") or ""),
            str(payload.get("response_text") or ""),
            events_preview,
            json.dumps({"context_memory": prior_memory, "claim_review": prior_claims}, ensure_ascii=False),
            max_chars=s.max_shadow_input_chars,
            latest_request=str(payload.get("latest_user_text") or ""),
            sensed=sensed,
        )

    base_prompt = rebuild()
    try:
        review, tool_results, used = await _review_with_sensing(base_prompt, rebuild, previous)
        if review.deception_level == "RED" and not review.concerns:
            # RED must always ship explicit wording; degrade the meter, never hide it.
            LOG.warning("Deception RED without wording downgraded to YELLOW run=%s", run_id)
            review = review.model_copy(update={"deception_level": "YELLOW"})
    except Exception as exc:
        # Do not leak provider errors/keys or mislabel invalid JSON as success.
        LOG.warning("Observer degraded run=%s error_type=%s after_retry",
                    run_id, type(exc).__name__)
        memory = completed_memory(previous)
        await repo.save_b_state(session, tenant_id, run_id, {
            "schema_version": 3, "run_id": run_id,
            "oversight_status": "degraded", "observed_at": observed_at,
            "reviewed_at": time.time(), "floor_id": payload.get("floor_id", ""),
            "attempt_id": payload.get("attempt_id", 1),
            "deception_level": previous.get("deception_level"),
            "meter_rationale": previous.get("meter_rationale"),
            "evidence_ops_used": int(previous.get("evidence_ops_used", 0)),
            "context_memory": memory.model_dump() if memory else None,
            "claim_review": None,
            "reason": type(exc).__name__,
        })
        await repo.append_event(session, "shadow_degraded", tenant_id, run_id=run_id,
                                actor="lobe-b", payload={"error_type": type(exc).__name__})
        return {"ok": False, "degraded": True}

    state = reviewed_state(previous, review, {**payload, "run_id": run_id},
                           memory_enabled=s.context_memory_enabled,
                           claims_enabled=s.claim_checks_enabled,
                           evidence_used=used, evidence_snapshot=tool_results)
    concerns = state["claim_review"]["concerns"]
    await repo.save_b_state(session, tenant_id, run_id, state)
    if tool_results:
        await evidence_artifacts.record_evidence_memory(tenant_id, run_id, tool_results)
    if s.context_memory_enabled:
        await repo.append_event(session, "context_memory_updated", tenant_id, run_id=run_id,
                                actor="lobe-b", payload={
                                    "memory_version": state["context_memory"]["version"],
                                    "source_call": state["source_call"],
                                })
    await repo.append_event(session, "b_context_shadow", tenant_id, run_id=run_id,
                            actor="lobe-b", payload={
                                "n_concerns": len(concerns),
                                "deception_level": state.get("deception_level"),
                                "meter_rationale": review.meter_rationale,
                                "evidence_used": used, "tool_rounds": len(tool_results),
                                "source_call": state["source_call"],
                                "assessment_only": True,
                            })
    if concerns:
        await repo.append_event(session, "oversight", tenant_id, run_id=run_id,
                                actor="lobe-b", payload={
                                    "concerns": concerns,
                                    "assessment_only": True,
                                })
    return {"ok": True, "concerns": len(concerns), "evidence_used": used}
