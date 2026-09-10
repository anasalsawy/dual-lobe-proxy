"""One bounded background review; optional host calls remain app-owned."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..api.limits import _local_sliding
from ..core import redact
from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from ..state import repositories as repo
from ..state.memory import attach_observer_notes
from . import prompts
from .artifacts import bounded_artifacts
from .host_tools import make_plan, planned_call_id
from .peer_snapshot import (snapshot_artifacts, snapshot_messages,
                             snapshot_tool_results, snapshot_tools)
from .channels import completed_memory, knowledge_snapshot, memory_status, reviewed_state
from .protocol import Review, ground_review, parse_review, usable_state

LOG = logging.getLogger("dual_lobe.b.context_shadow")


def _tool_results_present(context_text: str, tool_ids: list[str]) -> bool:
    """Recognize host results for every B-issued call without trusting their content."""
    if not tool_ids:
        return False
    return all(re.search(r'"tool_call_id"\s*:\s*"' + re.escape(tool_id) + r'"', context_text or "")
               for tool_id in tool_ids)


async def _call_b(target_alias: str, prompt: str) -> str:
    s = get_settings()
    instructions = prompts.observer_instructions(s)
    # B receives definitions as data and returns optional requests in JSON; the
    # provider call itself never receives executable tools. Timeout covers the
    # entire observer operation.
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
    choice = data["choices"][0]
    if choice.get("finish_reason") not in (None, "stop") or choice["message"].get("tool_calls") or choice["message"].get("refusal"):
        raise ValueError("observer response is incomplete or unsupported")
    return choice["message"].get("content") or ""


CORRECTION_NOTE = (
    "\n\nYour previous output was rejected: {error}.\n"
    "Return ONLY the JSON contract above, complete and valid, with no prose or fences."
)


class ReviewBudgetExceeded(RuntimeError):
    pass


async def _obtain_review(target_alias: str, prompt: str, tenant_id: int = 0) -> Review:
    """Two attempts at most, each admitted, within ONE overall review deadline."""
    s = get_settings()
    request_prompt = prompt
    async with asyncio.timeout(s.b_timeout):
        for attempt in range(2):
            allowed, _ = await _local_sliding(f"b:tenant:{tenant_id}", 60, s.b_rpm_limit)
            if not allowed:
                raise ReviewBudgetExceeded("observer attempt budget exhausted")
            # Provider/transport failures are not retried. Only a successfully
            # received but invalid review can receive one corrective re-ask.
            raw = await _call_b(target_alias, request_prompt)
            try:
                result = ground_review(parse_review(redact.redact_payload(raw)), prompt)
                if result.knowledge_dropped:
                    LOG.warning("Observer dropped invalid/excess optional guidance count=%s", result.knowledge_dropped)
                return result
            except ValueError as exc:
                if attempt:
                    raise
                LOG.warning("Observer review invalid once; corrective retry error_type=%s", type(exc).__name__)
                # No raw model text, credentials, or validation input in diagnostics.
                request_prompt = prompt + CORRECTION_NOTE.format(error=type(exc).__name__)


async def run_shadow_cycle(session: AsyncSession, job: dict[str, Any],
                           tenant_id: int) -> dict[str, Any]:
    # Caller owns the transaction and per-run lock, including marking the job done.
    s = get_settings()
    payload = redact.redact_payload(job.get("payload") or {})
    run_id = str(payload.get("run_id") or job.get("run_id") or "")
    observed_at = float(payload.get("observed_at") or 0)
    latest = await repo.latest_b_state(session, run_id)
    previous = (latest or {}).get("payload") or {}
    now = time.time()
    pending = previous.get("verification_pending")
    if isinstance(pending, dict):
        try:
            # A host that never returns a requested artifact must not freeze
            # B's future reviews forever. After the normal B TTL, continue
            # fail-open with the newest response and mark the old request stale.
            if now - float(pending.get("observed_at", observed_at)) > s.b_state_ttl_seconds:
                pending = None
        except (TypeError, ValueError):
            pending = None

    reason = None
    if not s.b_enabled or not (s.context_memory_enabled or s.claim_checks_enabled):
        reason = "disabled"
    elif not 0 <= now - observed_at <= s.b_state_ttl_seconds:
        reason = "expired"
    elif float(previous.get("observed_at", 0)) >= observed_at:
        reason = "superseded"
    elif now - float(previous.get("reviewed_at", 0)) < s.b_cooldown_seconds:
        reason = "cooldown"
    elif isinstance(pending, dict) and not _tool_results_present(
        str(payload.get("context_text") or ""), pending.get("tool_ids") or []
    ):
        # Keep the original target and provisional state until the host sends
        # the tool-result continuation. This prevents a normal A follow-up from
        # replacing a pending verification with an unrelated review.
        reason = "verification_waiting"
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
    if prior_memory and not s.context_enrichment_enabled:
        prior_memory.pop("knowledge_notes", None)
    prior_claims = None
    if s.claim_checks_enabled and usable_state(previous, floor, attempt, s.b_state_ttl_seconds):
        prior_claims = previous.get("claim_review") or {"concerns": (previous.get("review") or {}).get("concerns", [])}
    verification_followup = isinstance(pending, dict) and _tool_results_present(
        str(payload.get("context_text") or ""), pending.get("tool_ids") or []
    )
    target_output = (str(pending.get("target_output") or "")
                     if verification_followup else str(payload.get("response_text") or ""))
    # Review the exact canonical snapshot that the proxy gave A.  Older jobs
    # without a snapshot retain their historical context_text fallback, but all
    # newly-created jobs use this shared view so B cannot miss A-only memory,
    # system/developer context, tools, or tool results.
    peer_snapshot = payload.get("peer_snapshot") or {}
    snapshot_context = snapshot_messages(peer_snapshot)
    prompt_context = (json.dumps(snapshot_context, ensure_ascii=False)
                      if snapshot_context else str(payload.get("context_text") or ""))
    review_tools = snapshot_tools(peer_snapshot) or payload.get("host_tools", [])
    review_artifacts = snapshot_artifacts(peer_snapshot) or bounded_artifacts(
        payload.get("artifacts"), s.max_artifact_chars)
    review_tool_results = snapshot_tool_results(peer_snapshot) or payload.get("tool_results", [])
    if verification_followup:
        prompt_context += (
            "\n\nVERIFICATION_FOLLOWUP: The host supplied results for B's requested "
            "checks. Reassess the original target response below using those "
            "results. Do not grade this follow-up response instead."
        )
    prompt = prompts.build_cycle_prompt(
        prompt_context,
        target_output,
        events_preview,
        json.dumps({"context_memory": prior_memory, "claim_review": prior_claims}, ensure_ascii=False),
        max_chars=s.max_shadow_input_chars,
        latest_request=str(payload.get("latest_user_text") or ""),
        host_tools=review_tools if s.b_host_tools_enabled else [],
        artifacts=review_artifacts,
        tool_results=review_tool_results,
    )
    try:
        review = await _obtain_review("lobe-b", prompt, tenant_id)
    except Exception as exc:
        # Do not leak provider errors/keys or mislabel invalid JSON as success.
        LOG.warning("Observer degraded run=%s error_type=%s",
                    run_id, type(exc).__name__)
        memory = completed_memory(previous)
        await repo.save_b_state(session, tenant_id, run_id, {
            "schema_version": 3, "run_id": run_id,
            "oversight_status": "degraded", "observed_at": observed_at,
            "reviewed_at": time.time(), "floor_id": payload.get("floor_id", ""),
            "attempt_id": payload.get("attempt_id", 1),
            "deception_level": "GREEN" if s.claim_checks_enabled else None,
            "source_call": str(payload.get("source_call", "")),
            "context_memory": memory.model_dump() if memory else None,
            "claim_review": None,
            "reason": type(exc).__name__,
        })
        await repo.append_event(session, "shadow_degraded", tenant_id, run_id=run_id,
                                actor="lobe-b", payload={"error_type": type(exc).__name__})
        return {"ok": False, "degraded": True}

    target_payload = {**payload, "run_id": run_id}
    if verification_followup:
        target_payload.update(
            source_call=pending.get("source_call", payload.get("source_call", "")),
            observed_at=pending.get("observed_at", payload.get("observed_at", 0)),
            response_text=target_output,
        )
    state = reviewed_state(previous, review, target_payload,
                           memory_enabled=s.context_memory_enabled,
                           claims_enabled=s.claim_checks_enabled,
                           enrichment_enabled=s.context_enrichment_enabled,
                           source_model=prompts.head_tail(get_registry().target("lobe-b").model, 256))
    offered = json.loads(prompt.split(prompts.EVIDENCE_MARKER, 1)[1])["HOST_TOOLS"]
    try:
        plan = (make_plan(review, offered, state["source_call"])
                if s.b_host_tools_enabled else [])
    except (KeyError, TypeError, ValueError):
        plan = []
    verification_plan = [call for call in plan
                         if call.get("request_kind") in ("artifact_full", "evidence")
                         or call.get("full_artifact")]
    if verification_followup:
        # One evidence round is enough to avoid a B↔host loop. B's final color
        # is based on the supplied result; any further request is ignored.
        state["host_tool_plan"] = []
        state["verification_pending"] = None
        state["verification_status"] = "completed"
    elif plan:
        state["host_tool_plan"] = plan
        if verification_plan:
            state["verification_pending"] = {
                "source_call": state["source_call"],
                "observed_at": state["observed_at"],
                "target_output": str(payload.get("response_text") or ""),
                # General enrichment/search requests may be delivered without
                # holding the claim grade open. Only evidence-bearing calls define
                # the required continuation and final assessment.
                "tool_ids": [planned_call_id(call) for call in verification_plan],
                "preliminary_concerns": state.get("claim_review", {}).get("concerns", []),
            }
            state["verification_status"] = "pending"
            state["deception_level"] = None
            state["deception_reason"] = "Verification requested; final assessment pending host evidence."
            state["claim_review"] = {"concerns": []}
        else:
            state["verification_pending"] = None
            state["verification_status"] = "completed"
    else:
        state["host_tool_plan"] = []
        state["verification_pending"] = None
        state["verification_status"] = "completed"
    concerns = state["claim_review"]["concerns"]
    await repo.save_b_state(session, tenant_id, run_id, state)
    snapshot = knowledge_snapshot(completed_memory(state))
    space = payload.get("memory_space")
    if snapshot and space and s.shared_memory_enabled and s.context_memory_enabled and s.context_enrichment_enabled:
        await attach_observer_notes(session, tenant_id, space, run_id, snapshot)
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
                                "source_call": state["source_call"],
                                "assessment_only": True,
                            })
    if concerns:
        await repo.append_event(session, "oversight", tenant_id, run_id=run_id,
                                actor="lobe-b", payload={
                                    "concerns": concerns,
                                    "assessment_only": True,
                                })
    return {"ok": True, "concerns": len(concerns)}
