"""Director session entrypoint and inspection, using the existing inference API."""
from __future__ import annotations

import asyncio
import copy
import time
from contextlib import aclosing

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from ..b.channels import ObserverContext
from ..core import stage
from ..core.engine import tenant_session
from ..core.redact import redact_payload
from ..director import engine, protocol
from ..director.store import DirectorStore, SessionConflict
from ..provider.adapters import resolve_request
from ..state import repositories as repo
from ..state.memory import load_memory, record_memory
from . import auth, limits

router = APIRouter()


class ClosingStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # In ASGI 2.4 a failed send can otherwise leave a suspended generator
            # waiting for garbage collection, with an upstream request still open.
            await self.body_iterator.aclose()


async def response(payload, request, principal, run_id, external_run, corr,
                   target, registry, settings, memory_space, observe):
    from . import chat

    fmt = payload.get("response_format")
    if fmt is not None and fmt != {"type": "text"}:
        raise HTTPException(400, "Director mode emits a labelled text transcript; JSON/structured response_format is incompatible.")
    if not corr.get("run"):
        raise HTTPException(400, "Director mode requires a stable X-DL-Run-ID on every request, including tool results.")
    history = request.headers.get("X-DL-History", "full").lower()
    if history not in ("full", "delta"):
        raise HTTPException(400, "X-DL-History must be full or delta.")
    request_id = request.headers.get("X-DL-Request-ID")
    if (history == "delta" and not request_id) or (request_id is not None and not 1 <= len(request_id) <= 128):
        raise HTTPException(400, "Delta history requires X-DL-Request-ID (1-128 characters), unique per request and reused on retries.")
    try:
        b_target = registry.target("lobe-b")
        if not b_target or not b_target.enabled or getattr(b_target, "kind", "chat_completions") != "chat_completions":
            raise ValueError()
    except (KeyError, ValueError):
        raise HTTPException(400, "Director mode requires an enabled Chat Completions B provider.") from None

    store = DirectorStore(principal.tenant_id, run_id)
    try:
        async with asyncio.timeout(5):
            token, previous = await store.acquire(settings.director_max_seconds)
    except SessionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Director state is unavailable; no model was invoked.") from None
    try:
        state = protocol.begin(previous, payload["messages"], {
            "floor": str(corr.get("floor", "")), "attempt": int(corr.get("attempt", 1)),
            "worker": str(corr.get("worker", "")), "task": str(corr.get("task", "")),
            "memory_space": memory_space, "delta": history == "delta",
            "request_id": request_id,
        }, settings)
        async with asyncio.timeout(5):
            await store.save(token, state)
    except Exception as exc:
        try:
            async with asyncio.timeout(5):
                await store.save(token, previous, release=True)
        except Exception:
            pass
        if isinstance(exc, SessionConflict):
            raise HTTPException(409, str(exc)) from None
        raise HTTPException(503, "Director state could not be saved; no model was invoked.") from None

    observations = []
    receipts = {}

    async def compose_a(messages):
        context = ObserverContext(memory_status="disabled", claim_status="disabled", status="disabled")
        if observe and stage.injection_enabled(settings.rollout_stage):
            context = await chat._read_context(principal.tenant_id, run_id,
                                               str(corr.get("floor", "")), int(corr.get("attempt", 1)))
        shared = await load_memory(principal.tenant_id, memory_space, messages)
        receipts.update(**context.receipt(), monitoring=True,
                        shared_memory_space=memory_space, shared_entry_ids=list(shared.entry_ids))
        return chat._effective_messages(messages, context, True, settings.monitoring_role,
                                         reminder_text=protocol.A_INSTRUCTIONS, shared_text=shared.text)

    async def admit(messages):
        result = await limits.check_limits(principal.tenant_id, chat._token_estimate(messages))
        if not result.allowed:
            raise RuntimeError("Director admission budget reached")

    async def record_a(call_id, messages, message, usage, latency_ms):
        # Shared memory is committed before returning executable tool calls or
        # declaring this segment complete. The observer remains best effort.
        await record_memory(principal.tenant_id, memory_space, run_id, call_id, messages, [message])
        audit = {"call_id": call_id, "logical_model": target.model, "stream": True,
                 "status": "SUCCESS", "output": chat._messages_text([message], settings.max_shadow_input_chars),
                 "latency_ms": latency_ms, "observed_at": time.time(), "usage": usage,
                 "observer_delivery": copy.deepcopy(receipts)}
        observations.append((chat._messages_text(messages, settings.max_shadow_input_chars), audit))

    async def persist_observations():
        for context_text, audit in observations:
            await chat._persist_observation(principal.tenant_id, run_id, external_run, corr,
                                            "lobe-a", context_text, audit, observe)

    req = resolve_request(payload)
    loop = engine.DirectorLoop(state, store, token, req, registry.adapter("lobe-a"),
                               registry.adapter("lobe-b"), settings, compose_a, admit, record_a)
    background = BackgroundTask(persist_observations)
    headers = {"X-Dual-Lobe-Run-Id": run_id, "X-Dual-Lobe-Director": "on",
               "X-Dual-Lobe-Director-Cycle": state["cycle_id"], "X-Dual-Lobe-Memory": "per-turn",
               "X-Dual-Lobe-Claims": "per-turn", "X-Dual-Lobe-Monitoring": "on",
               "X-Dual-Lobe-Memory-Space": memory_space or "off"}
    if req.stream:
        return ClosingStreamingResponse(
            engine.stream(loop, payload["model"], bool((req.stream_options or {}).get("include_usage"))),
            media_type="text/event-stream", headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            background=background,
        )
    # A non-streaming caller receives the same transcript, after the segment ends.
    finish = None
    async with aclosing(engine.with_heartbeats(loop.events(), interval=1)) as events:
        async for event in events:
            if await request.is_disconnected():
                raise HTTPException(499, "Client disconnected; director cancelled.")
            if event is None:
                continue
            if event["kind"] == "error":
                return JSONResponse({"error": event["error"]}, status_code=502, headers=headers, background=background)
            if event["kind"] == "end":
                finish = event["finish_reason"]
    data = {"id": loop.response_id, "created": loop.created, "model": payload["model"],
            "object": "chat.completion", "choices": [
                {"index": 0, "message": state["wire"][-1], "finish_reason": finish}]}
    if loop.usage_complete:
        data["usage"] = loop.segment_usage
    return JSONResponse(data, headers=headers, background=background)


@router.get("/v1/dual-lobe/director/{run_id}")
async def inspect(run_id: str, principal: auth.Principal = Depends(auth.require_scope(auth.SCOPE_STATE_READ))):
    async with tenant_session(principal.tenant_id) as session:
        run = await repo._get_run_or_none(session, principal.tenant_id, run_id)
        if not run:
            raise HTTPException(404, "Run not found in this tenant.")
        internal = str(run.id)
    state = await DirectorStore(principal.tenant_id, internal).read()
    if not state:
        raise HTTPException(404, "No director session for this run.")
    return redact_payload({k: v for k, v in state.items() if k != "wire"})
