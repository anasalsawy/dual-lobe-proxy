"""Run the configured engine for one /v1/chat/completions request and shape the OpenAI response."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.settings import get_settings
from ..provider import calltrace


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def _is_local_base_url(base_url: str | None) -> bool:
    try:
        host = (urlparse(base_url or "").hostname or "").lower()
    except Exception:
        return False
    return host in {"localhost", "::1", "host.docker.internal"} or host.startswith("127.")


def _assert_clinical_b_local() -> None:
    from ..provider.registry import get_registry

    s = get_settings()
    if s.testing_mode and not s.clinical_b_local_only:
        return
    if not _is_local_base_url(get_registry().target("lobe-b-clinical").base_url):
        raise HTTPException(503, "Clinical B is local-only unless testing mode is explicitly enabled with "
                                 "DUAL_LOBE_TESTING_MODE=true and DUAL_LOBE_CLINICAL_B_LOCAL_ONLY=false.")


def _split_request(messages: list[dict], *, allow_tool_results: bool = False) -> tuple[str, str, dict[str, str]]:
    """Return (latest user message, system text, trailing tool results by tool_call_id)."""
    turns = [m for m in messages if m.get("role") not in {"system", "developer"}]
    tool_results: dict[str, str] = {}
    if allow_tool_results:
        while turns and turns[-1].get("role") == "tool":
            m = turns.pop()
            tool_results[str(m.get("tool_call_id") or "")] = _text(m.get("content"))
        if tool_results:
            # Drop the assistant tool_calls message these results answer.
            while turns and turns[-1].get("role") == "assistant":
                turns.pop()
    users = [m for m in turns if m.get("role") == "user"]
    if not users or (not tool_results and turns[-1].get("role") != "user"):
        raise HTTPException(400, "The last message must have role 'user'"
                                 + ("" if allow_tool_results else
                                    ". Tool-result messages are not supported by this engine."))
    system = "\n\n".join(_text(m.get("content")) for m in messages
                         if m.get("role") in {"system", "developer"}).strip()
    return _text(users[-1].get("content")), system, tool_results


def _latin1(headers: dict[str, Any]) -> dict[str, str]:
    # HTTP header values must be latin-1; B's rationale often is not (dashes, curly quotes).
    return {k: str(v).encode("latin-1", "replace").decode("latin-1") for k, v in headers.items()}


async def _run(engine: str, message: str, system: str, payload: dict[str, Any], tool_results: dict[str, str],
               on_delta=None) -> tuple[str, dict[str, str], dict, list | None]:
    """Run one engine; return (content, headers, dual_lobe extra, tool_calls for the client)."""
    headers: dict[str, str] = {"X-Dual-Lobe-Engine": engine}
    if engine == "split":
        from .split.engine import DualLobeEngine

        result = await DualLobeEngine().run(message)
        verdict = result.verdict
        headers.update({
            "X-Dual-Lobe-Meter": verdict.deception_level,
            "X-Dual-Lobe-Meter-Rationale": " ".join(verdict.rationale.split())[:300],
            "X-Dual-Lobe-Model-Calls": str(result.logical_model_calls),
        })
        extra = {"mode": result.mode, "verdict": verdict.model_dump(), "challenges": result.challenges,
                 "intent_risks": result.intent_risks, "overlooked_context": result.overlooked_context,
                 "delegation_note": result.delegation_note, "timings_ms": result.timings_ms,
                 "logical_model_calls": result.logical_model_calls}
        return result.visible_text(), headers, extra, None
    if engine == "clinical":
        from .clinical.engine import ClinicalDualLobeEngine, take_pending

        _assert_clinical_b_local()
        engine_obj = ClinicalDualLobeEngine()
        client_tools = payload.get("tools") or []
        state = take_pending(list(tool_results)) if tool_results else None
        if state is not None:
            headers["X-Dual-Lobe-Resumed"] = "true"
            result = await engine_obj.resume_clinical(state, tool_results, on_delta=on_delta)
        else:
            patient_context = system
            if tool_results:
                # The paused run is gone (expired or restarted): start over with the
                # results the client already has, given to local B as data.
                headers["X-Dual-Lobe-Resumed"] = "fresh-run"
                patient_context += ("\n\nRESULTS OF TOOLS CALLED EARLIER IN THIS CONVERSATION:\n"
                                    + "\n".join(f"[{cid}] {text}" for cid, text in tool_results.items()))
            # The clinical engine takes the patient record as its own input; the system message carries it.
            result = await engine_obj.run_clinical(query=message, patient_context=patient_context,
                                                   on_delta=on_delta, client_tools=client_tools)
        headers["X-Dual-Lobe-Plan-Revision"] = str(result.plan_revision)
        headers["X-Dual-Lobe-Model-Calls"] = str(result.logical_model_calls)
        headers["X-Dual-Lobe-Memory-Entries"] = str(result.memory_entries_used)
        if result.verdict is not None:
            headers["X-Dual-Lobe-Meter"] = result.verdict.deception_level
            headers["X-Dual-Lobe-Meter-Rationale"] = " ".join(result.verdict.rationale.split())[:300]
        if result.tool_calls:
            headers["X-Dual-Lobe-Tool-Calls"] = str(len(result.tool_calls))
        receipt = asdict(result.privacy_receipt) if result.privacy_receipt else None
        extra = {"mode": "clinical", "plan": result.plan.model_dump(), "plan_revision": result.plan_revision,
                 "plan_sha256": result.plan_sha256, "timings_ms": result.timings_ms,
                 "logical_model_calls": result.logical_model_calls,
                 "memory_entries_used": result.memory_entries_used,
                 "verdict": result.verdict.model_dump() if result.verdict else None,
                 "privacy_receipt": json.loads(json.dumps(receipt, default=str)) if receipt else None}
        visible_answer = result.answer
        if result.verdict is not None:
            meter = f"[{result.verdict.deception_level}] {result.verdict.rationale}".strip()
            visible_answer = f"{visible_answer.rstrip()}\n\nDual-Lobe meter: {meter}"
        return visible_answer, headers, extra, result.tool_calls
    raise HTTPException(500, f"unknown engine {engine}")


async def _run_traced(*args, **kwargs):
    content, headers, extra, tool_calls = await _run(*args, **kwargs)
    # Per-call latency log for this request (every upstream model call and tool run).
    extra = {**extra, "calls": calltrace.snapshot(), "server_ms": calltrace.now_ms()}
    return content, headers, extra, tool_calls


async def engine_response(engine: str, payload: dict[str, Any]):
    message, system, tool_results = _split_request(payload["messages"],
                                                   allow_tool_results=engine == "clinical")
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = payload.get("model") or engine

    if not payload.get("stream"):
        content, headers, extra, tool_calls = await _run_traced(engine, message, system, payload, tool_results)
        msg: dict[str, Any] = {"role": "assistant", "content": content or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return JSONResponse({
            "id": completion_id, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "dual_lobe": extra,
        }, headers=_latin1(headers))

    def sse(delta: dict[str, Any], finish: str | None = None, **more) -> str:
        chunk = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **more}
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    async def events():
        # Text that the engine produces live (clinical: A's final answer) is sent as
        # it arrives; keep-alive comments hold the connection open while the lobes
        # work; the meter / run details ride the last chunk.
        queue: asyncio.Queue = asyncio.Queue()

        async def on_delta(text: str) -> None:
            await queue.put(text)

        job = asyncio.ensure_future(_run_traced(engine, message, system, payload, tool_results, on_delta=on_delta))
        streamed: list[str] = []
        yield sse({"role": "assistant", "content": ""})
        while not job.done() or not queue.empty():
            if not queue.empty():
                text = queue.get_nowait()
                streamed.append(text)
                yield sse({"content": text})
                continue
            # Wake on new text or on the engine finishing, whichever comes first.
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter, job}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                text = getter.result()
                streamed.append(text)
                yield sse({"content": text})
                continue
            getter.cancel()
            if not done:
                yield ": dual-lobe working\n\n"
        try:
            content, headers, extra, tool_calls = job.result()
        except Exception as exc:  # noqa: BLE001
            detail = exc.detail if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
            yield sse({"content": ("\n\n" if streamed else "") + f"[dual-lobe error: {detail}]"}, "stop")
            yield "data: [DONE]\n\n"
            return
        sent = "".join(streamed)
        rest = content[len(sent):] if content.startswith(sent) else ("" if sent else content)
        if rest:
            yield sse({"content": rest})
        if tool_calls:
            yield sse({"tool_calls": [{"index": i, **tc} for i, tc in enumerate(tool_calls)]})
        meter = {k[len("X-Dual-Lobe-"):].lower().replace("-", "_"): v for k, v in headers.items()}
        yield sse({}, "tool_calls" if tool_calls else "stop", dual_lobe={**extra, **meter})
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers=_latin1(
        {"X-Dual-Lobe-Engine": engine, "X-Dual-Lobe-Streaming": "live",
         "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}))
