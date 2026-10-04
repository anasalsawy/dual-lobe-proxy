"""OpenAI-compatible facade for the persona agent runtime.

A thin public endpoint that turns chat requests into persona agent runs.
The persona's identity is its API key (same semantics as the proxy), the
key is forwarded to the proxy as the LLM credential, and every reply has
already passed through B's verification by the time the client sees it.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field

from .runtime import PersonaRuntime, persona_slug, persona_workspace_dir
from .outbox import Outbox
from .profile import load_profile
from .timesense import TimeSense

LOG = structlog.get_logger(__name__)

MAX_MESSAGE_BYTES = int(os.environ.get("DUAL_LOBE_PERSONA_MAX_MESSAGE_BYTES", "65536"))
REQUEST_TIMEOUT = float(os.environ.get("DUAL_LOBE_PERSONA_TIMEOUT", "300"))
MODEL_NAME = os.environ.get("DUAL_LOBE_PERSONA_MODEL_NAME", "sawii/dl-persona")


class ChatMessage(BaseModel):
    role: str
    content: str = ""


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DI seam: tests may pre-inject a runtime; production gets a real one.
    if getattr(app.state, "runtime", None) is None:
        app.state.runtime = PersonaRuntime()
    heartbeat_enabled = os.environ.get("DUAL_LOBE_PERSONA_HEARTBEAT_ENABLED", "true").lower() == "true"
    if getattr(app.state, "heartbeat", None) is None:
        app.state.heartbeat = None
        if heartbeat_enabled:
            from .heartbeat import PersonaHeartbeat

            app.state.heartbeat = PersonaHeartbeat(app.state.runtime)
    LOG.info("persona_runtime_started", proxy=os.environ.get("DUAL_LOBE_PROXY_URL", "http://localhost:8801/v1"),
             heartbeat=heartbeat_enabled)
    try:
        yield
    finally:
        if app.state.heartbeat is not None:
            app.state.heartbeat.shutdown()
        app.state.runtime.shutdown()


app = FastAPI(title="dual-lobe persona runtime", version="0.1.0", lifespan=lifespan)


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    key = authorization.split(" ", 1)[1].strip()
    if not key:
        raise HTTPException(status_code=401, detail="Empty bearer token")
    return key


def _last_user_message(messages: list[ChatMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user" and msg.content.strip():
            return msg.content
    raise HTTPException(status_code=400, detail="No user message in request")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "sawii"}]}


class InboundMessage(BaseModel):
    channel: str = "default"
    from_: str = Field(alias="from", default="user")
    text: str


class HeartbeatRegister(BaseModel):
    channel: str = "default"
    address: str = "user"


@app.post("/v1/persona/inbound")
async def persona_inbound(
    body: InboundMessage,
    request: Request,
    authorization: str | None = Header(default=None),
    x_dl_session: str | None = Header(default=None),
) -> dict:
    """Inbound messages from any channel bridge (WhatsApp, email, SMS...).

    The bridge authenticates with the persona key. The message runs
    through the persona's agent conversation; the reply lands in the
    outbox addressed to the same channel so the bridge can deliver it.
    """
    persona_key = _bearer(authorization)
    session = (x_dl_session or body.channel).strip() or "default"
    try:
        result = await asyncio.wait_for(
            request.app.state.runtime.chat(persona_key, body.text, session),
            REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Persona agent timed out") from None
    except Exception as exc:  # noqa: BLE001
        LOG.warning("persona_inbound_failed", persona=persona_slug(persona_key),
                    error=type(exc).__name__)
        raise HTTPException(status_code=502, detail="Persona agent failed") from None
    outbox = Outbox(persona_key)
    msg = outbox.append(body.channel, body.from_, result["content"], reason="inbound-reply")
    return {
        "reply": result["content"],
        "queued": msg is not None,
        "message_id": msg.id if msg else None,
    }


@app.get("/v1/persona/outbox")
async def persona_outbox(
    request: Request,
    authorization: str | None = Header(default=None),
    drain: bool = False,
    limit: int = 10,
) -> list[dict]:
    """Pending outbound messages for a channel bridge to deliver.

    ``drain=true`` marks the returned messages sent; a bridge that fails
    delivery can re-read with drain=false for retry visibility.
    """
    persona_key = _bearer(authorization)
    outbox = Outbox(persona_key)
    rows = outbox.drain(limit=limit) if drain else outbox.pending()
    return [
        {"id": r.id, "channel": r.channel, "to": r.to, "text": r.text,
         "created_at": r.created_at, "reason": r.reason}
        for r in rows
    ]


@app.post("/v1/persona/heartbeat")
async def persona_heartbeat(
    request: Request,
    body: HeartbeatRegister | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    """Register this persona for the initiative loop.

    The persona wakes on its heartbeat interval, checks whether anything
    from its real memory is worth reaching out about, and queues at most
    one message per beat (subject to quiet hours and the daily cap).
    """
    persona_key = _bearer(authorization)
    hb = request.app.state.heartbeat
    if hb is None:
        raise HTTPException(status_code=503, detail="Heartbeat not enabled")
    channel = body.channel if body else "default"
    address = body.address if body else "user"
    # One heartbeat config per persona: re-register replaces the old one.
    hb.register(persona_key)
    return {"registered": True, "interval_seconds": hb.interval_seconds}


@app.post("/v1/persona/beat")
async def persona_beat_now(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Run one heartbeat immediately (testing/operator trigger)."""
    persona_key = _bearer(authorization)
    hb = request.app.state.heartbeat
    if hb is None:
        raise HTTPException(status_code=503, detail="Heartbeat not enabled")
    return await hb.beat(persona_key)


@app.get("/v1/persona/profile")
async def get_profile(request: Request, authorization: str | None = Header(default=None)) -> dict:
    persona_key = _bearer(authorization)
    workspace = persona_workspace_dir(persona_key)
    return {"profile": load_profile(workspace)}


class ProfileUpdate(BaseModel):
    profile: str


@app.put("/v1/persona/profile")
async def put_profile(
    body: ProfileUpdate,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Replace the persona's profile (who they are, how they talk).

    The next conversation build picks it up; live conversations keep
    their old prompt until evicted or restarted.
    """
    persona_key = _bearer(authorization)
    if not body.profile.strip():
        raise HTTPException(status_code=400, detail="Profile must not be empty")
    workspace = persona_workspace_dir(persona_key)
    (workspace / "profile.md").write_text(body.profile)
    return {"updated": True}


@app.get("/v1/persona/clock")
async def persona_clock(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """The persona's clock: seconds since last exchange and last turn duration."""
    persona_key = _bearer(authorization)
    return TimeSense(persona_key).clock_state()


@app.websocket("/v1/persona/voice")
async def persona_voice(ws: WebSocket) -> None:
    """Realtime voice conversation (see voice.py for the frame protocol).

    Auth: persona key as bearer header or ?key= query param.
    """
    from .voice import serve_voice

    await serve_voice(ws, ws.app.state.runtime)


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_dl_session: str | None = Header(default=None),
) -> dict:
    started = time.monotonic()
    persona_key = _bearer(authorization)
    # The runtime never validates keys itself: the proxy is the authority.
    # Structurally-bogus keys are rejected fast; everything else is judged
    # by the proxy during the first agent LLM call (fail closed upstream).

    if body.stream:
        raise HTTPException(
            status_code=400,
            detail="Streaming is not supported by the persona runtime yet",
        )

    message = _last_user_message(body.messages)
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise HTTPException(status_code=413, detail="Message too large")

    session = (x_dl_session or "default").strip() or "default"

    try:
        result = await asyncio.wait_for(
            request.app.state.runtime.chat(persona_key, message, session),
            REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        LOG.warning("persona_chat_timeout", persona=persona_slug(persona_key))
        raise HTTPException(status_code=504, detail="Persona agent timed out") from None
    except Exception as exc:  # noqa: BLE001
        # Upstream auth failures and LLM errors surface as 502; never leak
        # internals or key material to the client.
        LOG.warning("persona_chat_failed", persona=persona_slug(persona_key), error=type(exc).__name__)
        raise HTTPException(status_code=502, detail="Persona agent failed") from None

    LOG.info(
        "persona_chat",
        persona=persona_slug(persona_key),
        session=session,
        actions=result["tool_actions"],
        status=200,
        latency=round(time.monotonic() - started, 3),
    )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result["content"]},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "dl_meta": {"tool_actions": result.get("tool_actions", 0),
                    "duration_seconds": result.get("duration_seconds")},
    }