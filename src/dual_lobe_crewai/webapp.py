from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from dual_lobe_crewai.engines import DualLobeEngine

MODEL_ID = os.getenv("DUAL_LOBE_MODEL_ID") or os.getenv("RAILWAY_SERVICE_NAME") or "dual-lobe-proxy"

app = FastAPI(title="Dual-Lobe Proxy", version="1.0")


class ChatRequest(BaseModel):
    message: str
    loop_cycles: int = 1


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "dual-lobe-proxy"}


@app.post("/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    engine = DualLobeEngine()
    if req.loop_cycles > 1:
        result = (await engine.run_loop(req.message, cycles=req.loop_cycles))[-1]
    else:
        result = await engine.run(req.message)
    return {
        "answer": result.answer,
        "visible_text": result.visible_text(),
        "verdict": result.verdict.model_dump(),
        "challenges": result.challenges,
        "intent_risks": result.intent_risks,
        "overlooked_context": result.overlooked_context,
        "timings_ms": result.timings_ms,
        "logical_model_calls": result.logical_model_calls,
    }


# --- OpenAI-compatible API -------------------------------------------------
# A thin translator: it passes the user's latest message to the engine unchanged and
# returns exactly what the engine produced. It does not add prompts, rewrite history,
# or pretend to support features the engine does not have.

import asyncio as _asyncio
import json as _json
import time as _time
import uuid as _uuid

from fastapi import Header as _Header, HTTPException as _HTTPException
from fastapi.responses import JSONResponse as _JSONResponse, StreamingResponse as _StreamingResponse
from pydantic import ConfigDict as _ConfigDict

MODEL_ID = os.getenv("DUAL_LOBE_MODEL_ID") or os.getenv("RAILWAY_SERVICE_NAME") or "dual-lobe"
_HEARTBEAT_SECONDS = float(os.getenv("DUAL_LOBE_STREAM_HEARTBEAT_SECONDS", "10"))


class OpenAIMessage(BaseModel):
    model_config = _ConfigDict(extra="allow")
    role: str
    content: Any = None


class OpenAIChatRequest(BaseModel):
    model_config = _ConfigDict(extra="allow")
    model: str | None = None
    messages: list[OpenAIMessage]
    stream: bool = False


def _message_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def _check_api_key(authorization: str | None) -> None:
    expected = os.getenv("DUAL_LOBE_SERVER_API_KEY")
    if expected and authorization != f"Bearer {expected}":
        raise _HTTPException(status_code=401, detail="Invalid or missing API key.")


@app.get("/v1/models")
async def list_models(authorization: str | None = _Header(default=None)) -> dict[str, Any]:
    _check_api_key(authorization)
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "dual-lobe"}]}


def _engine_request(req: OpenAIChatRequest) -> ChatRequest:
    turns = [m for m in req.messages if m.role not in {"system", "developer"}]
    if not turns or turns[-1].role != "user":
        raise _HTTPException(
            status_code=400,
            detail="The last message must have role 'user'. Tool-result messages are not supported by this engine.",
        )
    message = _message_text(turns[-1].content)
    if os.getenv("DUAL_LOBE_SERVICE_MODE", "normal").lower().strip() == "clinical":
        # The clinical engine takes the patient record as its own input; the system message carries it.
        system = "\n\n".join(_message_text(m.content) for m in req.messages if m.role in {"system", "developer"}).strip()
        return ChatRequest(message=message, patient_context=system)
    return ChatRequest(message=message)


def _completion(out: dict[str, Any], ignored: list[str], completion_id: str, created: int) -> dict[str, Any]:
    content = str(out.get("visible_text") or out.get("answer") or "")
    extra = {k: v for k, v in out.items() if k not in {"answer", "visible_text"}}
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": MODEL_ID,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "dual_lobe": {**extra, "ignored_request_fields": ignored},
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: OpenAIChatRequest, authorization: str | None = _Header(default=None)):
    _check_api_key(authorization)
    engine_req = _engine_request(req)
    ignored = sorted((req.model_extra or {}).keys())
    headers = {"X-Dual-Lobe-Ignored": ",".join(ignored)} if ignored else {}
    completion_id = f"chatcmpl-{_uuid.uuid4().hex}"
    created = int(_time.time())

    if not req.stream:
        out = await chat(engine_req)
        return _JSONResponse(_completion(out, ignored, completion_id, created), headers=headers)

    async def events():
        task = _asyncio.create_task(chat(engine_req))
        while True:
            done, _ = await _asyncio.wait({task}, timeout=_HEARTBEAT_SECONDS)
            if done:
                break
            # SSE comment: keeps the connection alive; clients ignore it.
            yield ": dual-lobe working\n\n"
        base = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID}
        try:
            full = _completion(task.result(), ignored, completion_id, created)
            content = full["choices"][0]["message"]["content"]
            first = {"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}
            yield f"data: {_json.dumps({**base, 'choices': [first]})}\n\n"
            last = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "dual_lobe": full["dual_lobe"]}
            yield f"data: {_json.dumps(last, default=str)}\n\n"
        except Exception as exc:
            yield f"data: {_json.dumps({'error': {'message': f'{type(exc).__name__}: {exc}'}})}\n\n"
        yield "data: [DONE]\n\n"

    return _StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={**headers, "X-Dual-Lobe-Streaming": "final-answer-only"},
    )


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("dual_lobe_crewai.webapp:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
