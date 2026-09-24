"""Streaming A transport with best-effort, post-response observer enqueue."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import aclosing
import httpx
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from ..b.outbox import shadow_job_key, shadow_payload
from ..b.channels import ObserverContext, prepare_context
from ..b.prompts import OBSERVATION_REMINDER, head_tail
from ..b.recipient_router import route_message as _route_message
from ..core import stage as stage_mod
from ..core.engine import tenant_session
from ..core.redact import redact_payload
from ..core.settings import get_settings
from ..provider.adapters import resolve_request, response_dict
from ..provider.registry import get_registry
from ..roles import get_role_persona
from ..state import repositories as repo
from ..state.memory import load_memory, record_memory, validate_space
from ..director.protocol import Completion
from . import auth, correlation, limits
from .schemas import ChatCompletionRequest, ThreeWayIntervention

LOG = logging.getLogger("dual_lobe.api.chat")
router = APIRouter()


def _token_estimate(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, ensure_ascii=False)) // 4


def _messages_text(messages: list[dict[str, Any]], max_chars: int) -> str:
    # Preserve tool requests AND results. Do not collect hidden reasoning fields.
    parts = []
    for i, message in enumerate(messages):
        observed = {k: v for k, v in message.items()
                    if k in ("role", "content", "tool_calls", "tool_call_id", "name")}
        parts.append(f"message[{i}] " + json.dumps(observed, ensure_ascii=False))
    return head_tail("\n".join(parts), max_chars)


def _original_goal(messages: list[dict]) -> str:
    content = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    if isinstance(content, list):
        content = "\n".join(str(p.get("text", "")) for p in content)
    return head_tail(str(content or ""), 1000)


def _latest_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(p.get("text", "")) for p in content)
        return head_tail(str(content or ""), 1600)
    return ""


def _insert_role_persona(messages: list[dict], persona: str) -> list[dict]:
    """Insert hierarchy role persona as a system message after existing system messages."""
    index = 0
    while index < len(messages) and messages[index].get("role") in ("system", "developer"):
        index += 1
    return messages[:index] + [{"role": "system", "content": persona}] + messages[index:]


def _effective_messages(messages: list[dict], context: ObserverContext, reminder: bool,
                        monitoring_role: str = "system", *,
                        reminder_text: str = OBSERVATION_REMINDER, shared_text: str | None = None) -> list[dict]:
    effective = list(messages)
    # Insert only before the conversation, never between an assistant tool call
    # and its results. B's generated text is NOT promoted into a system message.
    index = 0
    while index < len(effective) and effective[index].get("role") in ("system", "developer"):
        index += 1
    if reminder:
        effective.insert(index, {"role": monitoring_role, "content": reminder_text})
        index += 1
    # Two separately stored/delivered paths. Model-generated material stays at
    # user-message priority; only the fixed monitoring instruction is privileged.
    for name, content in (("shared_memory", shared_text),
                          ("observer_deception", context.deception_text),
                          ("observer_evidence", context.evidence_text),
                          ("observer_memory", context.memory_text),
                          ("observer_claims", context.claims_text)):
        if content:
            effective.insert(index, {"role": "user", "name": name, "content": content})
            index += 1
    return effective


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return status in (429, 500, 502, 503, 504) or isinstance(
        exc, (TimeoutError, ConnectionError, httpx.TransportError)
    )


async def _call_a_with_retry(fn, retries: int):
    for attempt in range(max(1, retries)):
        try:
            response = await fn()
            data = response_dict(response)
            choices = data.get("choices") or []
            if not choices:
                raise ValueError("upstream returned no choices")
            if not any((c.get("message") or {}).get("content")
                       or (c.get("message") or {}).get("tool_calls")
                       or (c.get("message") or {}).get("refusal") for c in choices):
                raise ValueError("upstream returned empty content and no tool calls")
            return data
        except Exception as exc:
            if attempt + 1 >= retries or not _is_retryable(exc):
                raise
            await asyncio.sleep(min(.25 * (attempt + 1), 1))


async def _read_context(tenant_id: int, run_id: str, floor: str, attempt: int) -> ObserverContext:
    s = get_settings()
    try:
        async with asyncio.timeout(s.b_state_read_timeout):
            async with tenant_session(tenant_id) as session:
                try:
                    latest = await repo.latest_b_state(session, run_id)
                except asyncio.TimeoutError:
                    raise
                except Exception:
                    # One immediate retry for a transient read failure, still bounded
                    # by the same overall deadline below which A fails open.
                    latest = await repo.latest_b_state(session, run_id)
        return prepare_context((latest or {}).get("payload"), floor, attempt, s)
    except Exception as exc:
        LOG.warning("Observer state unavailable run=%s error_type=%s; A continues",
                    run_id, type(exc).__name__)
        return ObserverContext(memory_status="unavailable", claim_status="unavailable",
                               status="state_unavailable")


async def _persist_observation(tenant_id: int, run_id: str, external_run: str,
                               corr: dict, target_alias: str, context_text: str,
                               audit: dict, observe: bool,
                               latest_user_text: str = "",
                               routing_mode: str | None = None) -> None:
    """After response delivery. Failures cannot change an already-sent A answer."""
    s = get_settings()
    try:
        audit["output"] = redact_payload(audit["output"])
        async with asyncio.timeout(5):
            async with tenant_session(tenant_id) as session:
                await repo.record_provider_attempt(
                    session, tenant_id, run_id=run_id, call_id=audit["call_id"],
                    provider_alias=target_alias, logical_model=audit["logical_model"],
                    stream=audit["stream"], status=audit["status"],
                    latency_ms=audit["latency_ms"], error_kind=audit.get("error_type", ""),
                    prompt_tokens=(audit.get("usage") or {}).get("prompt_tokens"),
                    completion_tokens=(audit.get("usage") or {}).get("completion_tokens"),
                )
                await repo.append_event(
                    session, "worker_call", tenant_id, run_id=run_id, actor=target_alias,
                    payload={"source": "proxy_observed", "call_id": audit["call_id"],
                             "status": audit["status"], "latency_ms": audit["latency_ms"],
                             "correlation": corr, "error_type": audit.get("error_type", ""),
                             "observer_delivery": audit.get("observer_delivery", {}),
                             "output_excerpt": audit["output"][:1500]},
                )
                # Default observes every completed call. Sampling is opt-in.
                count = await repo.count_attempts(session, run_id) if observe else 0
                if observe and (count - 1) % s.pulse_every == 0:
                    payload = shadow_payload(
                        tenant_id, run_id, external_run, int(corr.get("call_seq", 0)),
                        context_text, audit["output"],
                        floor_id=str(corr.get("floor", "")),
                        attempt_id=int(corr.get("attempt", 1)), stage=s.rollout_stage,
                    )
                    payload.update(source_call=audit["call_id"], observed_at=audit["observed_at"],
                                   latest_user_text=latest_user_text,
                                   routing_mode=routing_mode,
                                   provider_alias=target_alias)
                    scope_context = context_text + f"\nSCOPE:{corr.get('floor', '')}:{corr.get('attempt', 1)}"
                    key = shadow_job_key(tenant_id, run_id, 0, scope_context, audit["output"])
                    await repo.enqueue_outbox(session, tenant_id, "b", key, payload)
                await session.commit()
    except Exception as exc:
        LOG.warning("Observation persistence lost run=%s call=%s error_type=%s",
                    run_id, audit["call_id"], type(exc).__name__)


async def _stream_body(adapter, req, public_model: str, audit: dict, save_memory=None):
    started = time.monotonic()
    finished = set()
    seen = set()
    collector = Completion() if save_memory else None
    terminal = []
    try:
        async with asyncio.timeout(req.timeout), aclosing(adapter.stream(req)) as upstream:
            async for raw in upstream:
                chunk = response_dict(raw)
                chunk["model"] = public_model
                if collector is not None:
                    collector.add(chunk)
                for choice in chunk.get("choices") or []:
                    index = choice.get("index", 0)
                    seen.add(index)
                    if choice.get("finish_reason") is not None:
                        finished.add(index)
                    delta = choice.get("delta") or {}
                    text = delta.get("content") or ""
                    if delta.get("tool_calls"):
                        text += "\nTOOL_REQUEST " + json.dumps(delta["tool_calls"], ensure_ascii=False)
                    audit["output"] = head_tail(
                        audit["output"] + text, get_settings().max_shadow_input_chars
                    )
                if chunk.get("usage"):
                    audit["usage"] = chunk["usage"]
                wire = "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                if collector is not None and (finished or terminal):
                    terminal.append(wire)
                else:
                    yield wire
        if not seen or not seen <= finished:
            raise ValueError("upstream stream ended without a terminal choice")
        if collector is not None:
            await save_memory([collector.message(req.tools)])
            for wire in terminal:
                yield wire
        audit["status"] = "SUCCESS"
    except asyncio.CancelledError:
        audit["status"] = "INCOMPLETE"
        audit["error_type"] = "ClientDisconnected"
        raise
    except Exception as exc:
        audit["status"] = "INCOMPLETE"
        audit["error_type"] = type(exc).__name__
        # Flush any buffered terminal chunks so the client sees finish_reason.
        for wire in terminal:
            yield wire
        # Once SSE has started, HTTP status cannot change. Never fabricate stop.
        yield 'data: {"error":{"type":"upstream_stream_error","message":"Stream interrupted; partial output only."}}\n\n'
    finally:
        audit["latency_ms"] = int((time.monotonic() - started) * 1000)
        audit["observed_at"] = time.time()
    yield "data: [DONE]\n\n"


@router.post("/v1/dual-lobe/runs/{run_ref}/intervene")
async def dual_lobe_threeway_intervene(
    run_ref: str,
    body: ThreeWayIntervention,
    principal: auth.Principal = Depends(auth.require_scope(auth.SCOPE_INFERENCE_INVOKE)),
):
    """Inject a real-user message into an active three-way A/B exchange.

    ``run_ref`` may be the external X-DL-Run-ID or the internal run UUID.
    Delivery is persistent in the event ledger and is observed at the next model
    turn boundary, so it works even when the live SSE stream is served elsewhere.
    """
    from ..dl.threeway import append_intervention

    async with tenant_session(principal.tenant_id) as session:
        run = await repo._get_run_or_none(session, principal.tenant_id, run_ref)
    if run is None:
        raise HTTPException(status_code=404, detail="dual-lobe run not found")
    try:
        event = await append_intervention(
            principal.tenant_id, str(run.id),
            content=body.content, recipient=body.to,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({
        "status": "accepted",
        "run_id": run.external_run_id or str(run.id),
        "internal_run_id": str(run.id),
        "intervention": event,
    })


@router.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest, request: Request,
    principal: auth.Principal = Depends(auth.require_scope(auth.SCOPE_INFERENCE_INVOKE)),
):
    s = get_settings()
    payload = body.model_dump(exclude_none=True)
    messages = payload["messages"]
    if not messages:
        raise HTTPException(status_code=400, detail="messages must be non-empty")
    if len(json.dumps(payload).encode()) > s.gateway_max_request_bytes:
        raise HTTPException(status_code=413, detail="request too large")
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
            not isinstance(part, dict) or part.get("type") != "text" for part in content
        ):
            raise HTTPException(status_code=400, detail="image/audio input is disabled; text only")
    alias = payload["model"]
    corr = correlation.parse_headers(request.headers)
    director_mode = alias == "lobe-a-director" or corr.get("mode") == "director"
    if director_mode and (not s.director_enabled or correlation.is_bypass(corr)):
        raise HTTPException(400, "Director mode is disabled or conflicts with bypass.")
    if director_mode and alias not in ("sawii/dual-lobe", "lobe-a-director"):
        raise HTTPException(400, "Use lobe-a-director or lobe-a with director mode.")
    target_alias = "sawii/dual-lobe" if director_mode else alias
    requested_space = request.headers.get("X-DL-Memory-ID")
    selected_space = requested_space if requested_space is not None else (s.default_memory_id if s.shared_memory_enabled else None)
    try:
        memory_space = validate_space(selected_space) if selected_space and selected_space != "off" else None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if memory_space and not s.shared_memory_enabled:
        raise HTTPException(400, "Shared memory is disabled on this proxy.")
    try:
        target = get_registry().target(target_alias)
        if target is None or not target.enabled:
            raise KeyError(alias)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown model: {alias}") from None
    if getattr(target, "kind", "chat_completions") != "chat_completions":
        raise HTTPException(status_code=400, detail="only chat_completions is supported")
    if alias == "lobe-b":
        raise HTTPException(status_code=400, detail="lobe-b is internal; use lobe-a")

    # Map routing model aliases to modes for recipient routing.
    alias_to_mode = {
        "sawii/dual-lobe-old": "off",
        "sawii/dl-dialogue": "flat",
        "sawii/dl-dialogue1": "hierarchy",
        "sawii/dl-dialogue2": "hierarchy",
        "sawii/dl-dialogue3": "hierarchy",
    }
    routing_mode = alias_to_mode.get(alias)
    if routing_mode:
        # Ensure routing is enabled for flat/hierarchy aliases; off alias ignores global toggle.
        if routing_mode != "off" and not s.recipient_routing_enabled:
            raise HTTPException(status_code=400, detail=f"{alias} requires recipient routing enabled")

    limit = await limits.check_limits(principal.tenant_id, _token_estimate(messages))
    if not limit.allowed:
        raise HTTPException(status_code=429, detail="rate limit exceeded",
                            headers={"Retry-After": str(int(limit.retry_after + 1))})
    observe = (s.b_enabled and (s.context_memory_enabled or s.claim_checks_enabled
                                or s.deception_meter_enabled)
               and not correlation.is_bypass(corr))
    external_run = str(corr.get("run") or uuid.uuid4())
    floor, attempt = str(corr.get("floor", "")), int(corr.get("attempt", 1))
    async with tenant_session(principal.tenant_id) as session:
        run = await repo.get_or_create_run(
            session, principal.tenant_id, external_run, floor_id=floor,
            attempt=attempt, goal=_original_goal(messages),
        )
        run_id = str(run.id)
        await session.commit()

    # Dual-lobe mode: pre-final A/B collaboration. For streaming requests,
    # bridge the handler's event sink into the HTTP response so the client can
    # watch A/B turns as they complete rather than waiting for finalization.
    if alias == "sawii/dual-lobe":
        from ..dl import handler as dl_handler

        # Experimental interleaved mode: A streams to the user immediately while
        # B shadows semantic snapshots in parallel. No approval gate is placed in
        # A's normal path; only material B interventions cause resynchronization.
        if payload.get("stream", False) and payload.get("dual_lobe_interleaved", False):
            try:
                from ..dl.interleaved import interleaved_event_stream
            except ImportError:
                raise HTTPException(
                    status_code=400,
                    detail="interleaved mode is not available in this build",
                ) from None
            inline_flags = payload.get("dual_lobe_interleaved_inline_flags")
            if inline_flags is None:
                inline_flags = True
            live_events = payload.get("dual_lobe_live_events")
            if live_events is None:
                live_events = True

            async def _dl_interleaved_stream():
                completion_id = f"chatcmpl-dl-shadow-{run_id}"
                created = int(time.time())
                role_sent = False
                terminal_seen = False

                def _event_chunk(event):
                    return {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": alias,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                        "dual_lobe_event": event,
                    }

                async for item in interleaved_event_stream(
                    payload, run_id, principal.tenant_id, alias, task=str(run.goal or "")
                ):
                    if item["kind"] == "chunk":
                        chunk = item["data"]
                        chunk["id"] = chunk.get("id") or completion_id
                        chunk["model"] = alias
                        # The provider may omit role in its first delta; make sure
                        # generic clients still receive a valid assistant opener.
                        choices = chunk.get("choices") or []
                        if choices and not role_sent:
                            delta = choices[0].get("delta") or {}
                            if not delta.get("role"):
                                opener = {
                                    "id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": alias,
                                    "choices": [{"index": 0, "delta": {"role": "assistant"},
                                                 "finish_reason": None}],
                                }
                                yield "data: " + json.dumps(opener, ensure_ascii=False) + "\n\n"
                            role_sent = True
                        for choice in choices:
                            if choice.get("finish_reason") is not None:
                                terminal_seen = True
                        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                        continue

                    if item["kind"] == "event":
                        event = item["event"]
                        if live_events:
                            yield "data: " + json.dumps(_event_chunk(event), ensure_ascii=False) + "\n\n"
                        if inline_flags and event.get("type") == "shadow_intervention":
                            action = str(event.get("action", "FLAG")).replace("_", " ")
                            content = str(event.get("message", "") or "").strip()
                            if content:
                                text = f"\n\n🅱️ B — {action}\n{content}\n\n🅰️ A\n"
                                visible = {
                                    "id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": alias,
                                    "choices": [{"index": 0, "delta": {"content": text},
                                                 "finish_reason": None}],
                                }
                                yield "data: " + json.dumps(visible, ensure_ascii=False) + "\n\n"
                        if event.get("type") == "shadow_tool_request":
                            # B has the same host tool definitions as A. Surface its
                            # request through the canonical OpenAI tool-call channel so
                            # the existing host executes it normally. On the next turn
                            # both lobes receive the exact tool result from conversation
                            # history/shared reality.
                            tool_calls = event.get("tool_calls") or []
                            if inline_flags and event.get("message"):
                                notice = {
                                    "id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": alias,
                                    "choices": [{"index": 0, "delta": {
                                        "content": f"\n\n🅱️ B — TOOL REQUEST\n{str(event.get('message'))}\n"
                                    }, "finish_reason": None}],
                                }
                                yield "data: " + json.dumps(notice, ensure_ascii=False) + "\n\n"
                            if tool_calls:
                                tool_delta = {
                                    "id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": alias,
                                    "choices": [{"index": 0, "delta": {
                                        "tool_calls": [dict({"index": i}, **call) for i, call in enumerate(tool_calls)]
                                    }, "finish_reason": None}],
                                }
                                yield "data: " + json.dumps(tool_delta, ensure_ascii=False) + "\n\n"
                                terminal = {
                                    "id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": alias,
                                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                                }
                                yield "data: " + json.dumps(terminal, ensure_ascii=False) + "\n\n"
                                terminal_seen = True
                        continue

                    if item["kind"] == "meta":
                        if live_events:
                            yield "data: " + json.dumps(
                                _event_chunk({"type": "shadow_meta", **item["data"]}),
                                ensure_ascii=False
                            ) + "\n\n"

                # The upstream A stream may already have emitted a terminal choice.
                # We still terminate the multiplexed SSE exactly once.
                if not terminal_seen:
                    final = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": alias,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield "data: " + json.dumps(final, ensure_ascii=False) + "\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                _dl_interleaved_stream(), media_type="text/event-stream",
                headers={
                    "X-Dual-Lobe-Mode": "interleaved-shadow",
                    "X-Dual-Lobe-Meter": "LIVE",
                    "X-Dual-Lobe-Wait-Policy": "no-wait-unless-intervention",
                    "X-Dual-Lobe-Tool-Mode": str(payload.get("dual_lobe_tool_mode") or "shared"),
                    "X-DL-Run-ID": external_run,
                    "X-DL-Internal-Run-ID": run_id,
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        if payload.get("stream", False):
            inline_exchange = payload.get("dual_lobe_inline_exchange")
            if inline_exchange is None:
                inline_exchange = True
            live_events = payload.get("dual_lobe_live_events")
            if live_events is None:
                live_events = True

            async def _dl_live_stream():
                completion_id = f"chatcmpl-dl-{run_id}"
                created = int(time.time())
                inline_started = False

                def _chunk(*, delta=None, finish_reason=None, event=None, usage=None, index=0):
                    item = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": alias,
                        "choices": [{
                            "index": index,
                            "delta": delta or {},
                            "finish_reason": finish_reason,
                        }],
                    }
                    if event is not None:
                        item["dual_lobe_event"] = event
                    if usage is not None:
                        item["usage"] = usage
                    return item

                try:
                    # Standard OpenAI-style stream opener. Generic clients can
                    # establish the assistant role before any collaboration text.
                    yield "data: " + json.dumps(
                        _chunk(delta={"role": "assistant"}), ensure_ascii=False
                    ) + "\n\n"

                    async for item in dl_handler.dual_lobe_event_stream(
                        payload, run_id, principal.tenant_id, alias,
                        task=str(run.goal or ""),
                    ):
                        if item["kind"] == "event":
                            event = item["event"]

                            # Structured event: safe for custom clients and does
                            # not alter the canonical assistant text. Unknown
                            # extension fields are ignorable by generic clients.
                            if live_events:
                                yield "data: " + json.dumps(
                                    _chunk(event=event), ensure_ascii=False
                                ) + "\n\n"

                            # Compatibility view: stream a human-readable A/B
                            # transcript as normal content. The next request strips
                            # this display-only prefix back out of history.
                            if inline_exchange and event.get("type") == "exchange_message":
                                actor = str(event.get("actor", "?")).upper()
                                label = "🅰️ A" if actor == "A" else "🅱️ B" if actor == "B" else "👤 YOU" if actor == "USER" else actor
                                content = str(event.get("content", "") or "").strip()
                                if content:
                                    prefix = ""
                                    if not inline_started:
                                        prefix = "Dual-Lobe collaboration\n\n"
                                        inline_started = True
                                    text = prefix + label + "\n" + content + "\n\n"
                                    yield "data: " + json.dumps(
                                        _chunk(delta={"content": text}), ensure_ascii=False
                                    ) + "\n\n"
                            continue

                        data = item["data"]
                        # ``dual_lobe_event_stream`` forces the buffered handler
                        # to keep message.content clean. If we streamed an inline
                        # transcript, add the final marker here exactly once.
                        choices = data.get("choices") or []
                        for choice in choices:
                            message = dict(choice.get("message") or {})
                            finish = choice.get("finish_reason")
                            if finish is None:
                                finish = "tool_calls" if message.get("tool_calls") else "stop"
                            if inline_started and isinstance(message.get("content"), str):
                                message["content"] = (
                                    "──────── FINAL ANSWER ────────\n" + message["content"]
                                )
                            out = _chunk(
                                delta=message,
                                finish_reason=finish,
                                index=int(choice.get("index", 0) or 0),
                                event={
                                    "type": "exchange_result",
                                    "dual_lobe": data.get("dual_lobe"),
                                } if live_events and data.get("dual_lobe") else None,
                                usage=data.get("usage"),
                            )
                            yield "data: " + json.dumps(out, ensure_ascii=False) + "\n\n"
                        yield "data: [DONE]\n\n"
                        return
                except asyncio.CancelledError:
                    # Cancelling the response cancels the queue bridge, which in
                    # turn cancels in-flight dual-lobe inference.
                    raise

            return StreamingResponse(
                _dl_live_stream(), media_type="text/event-stream",
                headers={
                    "X-Dual-Lobe-Mode": "pre-final-live",
                    "X-Dual-Lobe-Meter": "PENDING",
                    "X-Dual-Lobe-Three-Way": "on" if payload.get("dual_lobe_three_way") else "off",
                    "X-DL-Run-ID": external_run,
                    "X-DL-Internal-Run-ID": run_id,
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        try:
            data, dl_headers, dl_background = await dl_handler.dual_lobe_response(
                payload, run_id, principal.tenant_id, alias, task=str(run.goal or ""),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        background = BackgroundTask(dl_background) if dl_background else None
        dl_headers = dict(dl_headers)
        dl_headers["X-DL-Run-ID"] = external_run
        dl_headers["X-DL-Internal-Run-ID"] = run_id
        return JSONResponse(data, status_code=200, headers=dl_headers, background=background)

    # Dialogue co-author mode: B wraps A on both upstream and downstream.
    # This is intentionally separate from the verifier/gated model so both can
    # be tested side-by-side.
    if alias == "sawii/dialogue":
        from ..coauthor.handler import coauthor_response
        data, co_headers = await coauthor_response(
            payload, run_id, principal.tenant_id, alias
        )
        if payload.get("stream", False):
            import json as _json

            async def _coauthor_stream():
                chunk = dict(data)
                chunk["object"] = "chat.completion.chunk"
                for c in chunk.get("choices", []):
                    c["delta"] = c.pop("message", {})
                    c.pop("finish_reason", None)
                    c["finish_reason"] = "stop"
                yield f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                _coauthor_stream(), media_type="text/event-stream",
                headers={**co_headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return JSONResponse(data, status_code=200, headers=co_headers)

    # Gated mode: B sits inline.  Completely separate code path.
    if alias in ("sawii/dl-gated", "sawii/dl-dialogue", "sawii/dual-lobe-old"):
        from ..gated.handler import gated_response
        data, gate_headers = await gated_response(payload, run_id, principal.tenant_id, alias)
        # If Hermes requested streaming, convert the buffered response to SSE
        if payload.get("stream", False):
            import json as _json
            async def _gated_stream():
                chunk = dict(data)
                chunk["object"] = "chat.completion.chunk"
                for c in chunk.get("choices", []):
                    c["delta"] = c.pop("message", {})
                    c.pop("finish_reason", None)
                    c["finish_reason"] = "stop"
                yield f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(
                _gated_stream(), media_type="text/event-stream",
                headers={**gate_headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return JSONResponse(data, status_code=200, headers=gate_headers)

    # Pre-emptive routing check: if routing is enabled for this alias, ask the
    # router LLM (lobe-b) whether this agent should respond BEFORE calling the
    # upstream model. If the rules say "don't respond", return a suppressed
    # response immediately — no upstream tokens spent, no response generated.
    if routing_mode and routing_mode != "off" and s.recipient_routing_enabled:
        LOG.info("Pre-emptive routing check starting for %s mode=%s", alias, routing_mode)
        latest_user_text = _latest_user_text(messages)
        if latest_user_text:
            try:
                async with tenant_session(principal.tenant_id) as session:
                    routing_analysis, _ = await _route_message(
                        session,
                        agent_name=alias,
                        message=latest_user_text,
                        tenant_id=principal.tenant_id,
                        run_id=run_id,
                        mode=routing_mode,
                        context_for_memory={"messages": messages},
                    )
                    await session.commit()
                should_respond = routing_analysis.should_respond
                if not should_respond:
                    LOG.info("Pre-emptive routing suppressed response for %s (confidence=%.2f, reasoning=%s)",
                             alias, routing_analysis.confidence, routing_analysis.reasoning)
                    # Return a minimal "suppressed" response — no upstream call made.
                    suppressed = {
                        "id": f"chatcmpl-suppressed-{uuid.uuid4().hex[:24]}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": alias,
                        "choices": [{
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "",
                            },
                            "finish_reason": "stop",
                        }],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    }
                    return JSONResponse(
                        suppressed,
                        status_code=200,
                        headers={
                            "X-Dual-Lobe-Routing": "suppressed",
                            "X-Dual-Lobe-Routing-Reasoning": routing_analysis.reasoning[:200],
                            "X-Dual-Lobe-Routing-Confidence": str(routing_analysis.confidence),
                        },
                    )
            except Exception as exc:
                LOG.warning("Pre-emptive routing check failed for %s: %s; proceeding with request",
                            alias, type(exc).__name__)
                # On routing failure, fail open — let the request through.
    if director_mode:
        from . import director
        return await director.response(payload, request, principal, run_id, external_run, corr,
                                       target, get_registry(), s, memory_space, observe)
    # No transaction or B model call is held across A's provider operation.
    context = ObserverContext(memory_status="disabled", claim_status="disabled", status="disabled")
    if observe and stage_mod.injection_enabled(s.rollout_stage):
        # Forced runtime hook, on EVERY eligible call, including calls after tools.
        # It reloads completed memory even when the client omits it from history.
        context = await _read_context(principal.tenant_id, run_id, floor, attempt)
    req = resolve_request(payload)
    monitoring = observe and s.observation_reminder
    try:
        shared = await load_memory(principal.tenant_id, memory_space, messages)
    except Exception:
        raise HTTPException(503, "Shared memory is unavailable; no model was invoked.") from None
    req.messages = _effective_messages(messages, context, monitoring, s.monitoring_role, shared_text=shared.text)
    # Inject hierarchy role persona as a system message for dl-dialogue aliases
    role_persona = get_role_persona(alias)
    if role_persona:
        req.messages = _insert_role_persona(req.messages, role_persona)
    req.timeout = s.a_timeout
    adapter = get_registry().adapter(alias)
    context_text = head_tail(
        "Original run goal (caller-reported): " + redact_payload(run.goal) + "\n" +
        _messages_text(redact_payload(messages), s.max_shadow_input_chars),
        s.max_shadow_input_chars,
    )
    latest_user_text = _latest_user_text(messages)
    audit = {"call_id": str(uuid.uuid4()), "logical_model": target.model,
             "stream": req.stream, "status": "INCOMPLETE", "output": "",
             "latency_ms": 0, "observed_at": time.time(),
             "observer_delivery": {**context.receipt(), "monitoring": monitoring}}
    audit["observer_delivery"].update(shared_memory_space=memory_space, shared_entry_ids=list(shared.entry_ids))
    async def save_memory(responses):
        await record_memory(principal.tenant_id, memory_space, run_id, audit["call_id"], messages, responses)
    background = BackgroundTask(_persist_observation, principal.tenant_id, run_id,
                                external_run, corr, alias, context_text, audit, observe,
                                latest_user_text=latest_user_text, routing_mode=routing_mode)
    headers = {"X-Dual-Lobe-Run-Id": run_id, "X-Dual-Lobe-Observer": context.status,
              "X-Dual-Lobe-Call-Id": audit["call_id"],
              "X-Dual-Lobe-Memory": (f"v{context.memory_version}" if context.memory_text else context.memory_status),
              "X-Dual-Lobe-Claims": context.claim_status,
              "X-Dual-Lobe-Deception": context.deception_status,
              "X-Dual-Lobe-Deception-Rationale": (context.deception_text or "")[:300],
              "X-Dual-Lobe-Evidence": context.evidence_status,
              "X-Dual-Lobe-Monitoring": "on" if monitoring else "off",
              "X-Dual-Lobe-Memory-Space": memory_space or "off",
              "X-Dual-Lobe-Shared-Entries": str(len(shared.entry_ids))}
    if req.stream:
        return StreamingResponse(
            _stream_body(adapter, req, alias, audit, save_memory if memory_space else None), media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            background=background,
        )
    started = time.monotonic()
    try:
        async with asyncio.timeout(s.a_timeout):
            data = await _call_a_with_retry(lambda: adapter.buffered(req), s.a_retries)
        data["model"] = alias
        await save_memory([c["message"] for c in data["choices"]])
        audit["output"] = _messages_text(
            [c["message"] for c in data["choices"]], s.max_shadow_input_chars
        )
        audit["usage"] = data.get("usage")
        audit["status"] = "SUCCESS"
        status_code = 200
    except Exception as exc:
        audit["status"], audit["error_type"] = "FAILED", type(exc).__name__
        LOG.warning("upstream call failed: %s: %s", type(exc).__name__, exc)
        data = {"error": {"type": "upstream_error", "message": "Upstream request failed."}}
        status_code = 502
    audit["latency_ms"] = int((time.monotonic() - started) * 1000)
    audit["observed_at"] = time.time()
    audit["output"] = redact_payload(audit["output"])
    return JSONResponse(data, status_code=status_code, headers=headers, background=background)
