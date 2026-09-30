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


def _split_request(messages: list[dict]) -> tuple[str, str]:
    turns = [m for m in messages if m.get("role") not in {"system", "developer"}]
    if not turns or turns[-1].get("role") != "user":
        raise HTTPException(400, "The last message must have role 'user'. "
                                 "Tool-result messages are not supported by this engine.")
    system = "\n\n".join(_text(m.get("content")) for m in messages
                         if m.get("role") in {"system", "developer"}).strip()
    return _text(turns[-1].get("content")), system


def _latin1(headers: dict[str, Any]) -> dict[str, str]:
    # HTTP header values must be latin-1; B's rationale often is not (dashes, curly quotes).
    return {k: str(v).encode("latin-1", "replace").decode("latin-1") for k, v in headers.items()}


async def _run(engine: str, message: str, system: str, on_delta=None) -> tuple[str, dict[str, str], dict]:
    """Run one engine; return (content, headers, dual_lobe extra)."""
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
        return result.visible_text(), headers, extra
    if engine == "clinical":
        from .clinical.engine import ClinicalDualLobeEngine

        _assert_clinical_b_local()
        # The clinical engine takes the patient record as its own input; the system message carries it.
        result = await ClinicalDualLobeEngine().run_clinical(query=message, patient_context=system,
                                                             on_delta=on_delta)
        headers["X-Dual-Lobe-Plan-Revision"] = str(result.plan_revision)
        headers["X-Dual-Lobe-Model-Calls"] = str(result.logical_model_calls)
        receipt = asdict(result.privacy_receipt) if result.privacy_receipt else None
        extra = {"mode": "clinical", "plan": result.plan.model_dump(), "plan_revision": result.plan_revision,
                 "plan_sha256": result.plan_sha256, "timings_ms": result.timings_ms,
                 "logical_model_calls": result.logical_model_calls,
                 "privacy_receipt": json.loads(json.dumps(receipt, default=str)) if receipt else None}
        return result.answer, headers, extra
    raise HTTPException(500, f"unknown engine {engine}")


async def engine_response(engine: str, payload: dict[str, Any]):
    message, system = _split_request(payload["messages"])
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = payload.get("model") or engine

    if not payload.get("stream"):
        content, headers, extra = await _run(engine, message, system)
        return JSONResponse({
            "id": completion_id, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
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

        job = asyncio.ensure_future(_run(engine, message, system, on_delta=on_delta))
        streamed: list[str] = []
        yield sse({"role": "assistant", "content": ""})
        while not job.done() or not queue.empty():
            try:
                text = await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                if not job.done():
                    yield ": dual-lobe working\n\n"
                continue
            streamed.append(text)
            yield sse({"content": text})
        try:
            content, headers, extra = job.result()
        except Exception as exc:  # noqa: BLE001
            detail = exc.detail if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
            yield sse({"content": ("\n\n" if streamed else "") + f"[dual-lobe error: {detail}]"}, "stop")
            yield "data: [DONE]\n\n"
            return
        sent = "".join(streamed)
        rest = content[len(sent):] if content.startswith(sent) else ("" if sent else content)
        if rest:
            yield sse({"content": rest})
        meter = {k[len("X-Dual-Lobe-"):].lower().replace("-", "_"): v for k, v in headers.items()}
        yield sse({}, "stop", dual_lobe={**extra, **meter})
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers=_latin1(
        {"X-Dual-Lobe-Engine": engine, "X-Dual-Lobe-Streaming": "live",
         "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}))
