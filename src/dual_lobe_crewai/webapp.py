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


class OpenAIMessage(BaseModel):
    role: str
    content: Any = None


class OpenAIChatRequest(BaseModel):
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
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_api_key(authorization)
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "dual-lobe"}]}


@app.post("/v1/chat/completions")
async def chat_completions(req: OpenAIChatRequest, authorization: str | None = Header(default=None)):
    _check_api_key(authorization)
    system = "\n\n".join(_message_text(m.content) for m in req.messages if m.role in {"system", "developer"}).strip()
    turns = [m for m in req.messages if m.role not in {"system", "developer"}]
    if not turns or turns[-1].role != "user":
        raise HTTPException(status_code=400, detail="The last message must have role 'user'.")

    message = _message_text(turns[-1].content)
    history = "\n".join(f"{m.role}: {_message_text(m.content)}" for m in turns[:-1])
    if history:
        message = f"Conversation so far:\n{history}\n\nCurrent message:\n{message}"
    if system:
        message = f"Instructions:\n{system}\n\n{message}"

    out = await chat(ChatRequest(message=message))
    # visible_text carries A's answer plus B's Dual-Lobe meter line.
    answer = str(out.get("visible_text") or out.get("answer") or "")
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if req.stream:
        def events():
            base = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID}
            first = {"index": 0, "delta": {"role": "assistant", "content": answer}, "finish_reason": None}
            yield f"data: {json.dumps({**base, 'choices': [first]})}\n\n"
            yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": MODEL_ID,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("dual_lobe_crewai.webapp:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
