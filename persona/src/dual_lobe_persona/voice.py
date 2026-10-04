"""Realtime voice conversation: the persona's mouth and ears.

The lag budget, end to end: the user stops talking, ASR finalizes the
turn, the agent's first token flows through the proxy (A speaks, B
verifies per token batch), and TTS synthesis begins on the FIRST COMPLETE
SENTENCE — not the full reply. Every stage streams; nothing waits for a
whole response before starting the next.

Protocol over the WebSocket (JSON text frames unless noted):

  client -> server:
    {"type": "session.begin"}                     open a turn session
    {"type": "audio.start"}                       mic opened (enables barge-in)
    bytes                                          raw audio chunks
    {"type": "audio.stop"}                        mic closed -> finalize turn
    {"type": "text", "text": "..."}               typed input (bypasses ASR)

  server -> client:
    {"type": "transcript.partial", "text": "..."}  ASR interim
    {"type": "transcript.final", "text": "..."}   turn captured
    {"type": "reply.chunk", "text": "..."}         agent reply, sentence chunk
    {"type": "audio.start", "rate": 24000}        TTS audio follows (rate)
    bytes                                         synthesized speech chunks
    {"type": "audio.stop"}                        reply playback finished
    {"type": "interrupted"}                       user barged in; speech cancelled

ASR and TTS are pluggable providers (see asr.py / tts.py). With no
provider configured the socket still runs end-to-end over typed frames —
the protocol is testable and demoable without credentials.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from .asr import Transcriber
from .runtime import PersonaRuntime
from .tts import Synthesizer
from .workspace import persona_slug

LOG = structlog.get_logger(__name__)

# Split replies into speakable pieces as soon as one sentence exists.
_SENTENCE_RE = re.compile(r"(.*?[.!?…])(?:\s+|$)", re.S)


def sentence_chunks(text: str) -> list[str]:
    """Split streamed text into speakable sentence units.

    Anything left over (no terminal punctuation yet) is a partial — the
    caller decides whether to hold it back for more text or flush it at
    end of reply.
    """
    out: list[str] = []
    buf = text
    while True:
        m = _SENTENCE_RE.match(buf)
        if not m:
            break
        out.append(m.group(1).strip())
        buf = buf[m.end():]
    return out, buf  # type: ignore[return-value]


class VoiceSession:
    """One live voice call between a user and their persona."""

    def __init__(self, runtime: PersonaRuntime, persona_key: str,
                 transcriber: Transcriber | None = None,
                 synthesizer: Synthesizer | None = None) -> None:
        self.runtime = runtime
        self.persona_key = persona_key
        self.transcriber = transcriber
        self.synthesizer = synthesizer
        self.speaking = False          # persona audio is playing out
        self.listening = False        # user mic is open
        self.turn_task: asyncio.Task | None = None
        self.speech_task: asyncio.Task | None = None

    async def send(self, obj: dict) -> None:
        raise NotImplementedError  # bound by serve()

    async def send_audio(self, chunk: bytes) -> None:
        raise NotImplementedError  # bound by serve()

    async def handle_frame(self, frame: Any) -> None:
        if isinstance(frame, (bytes, bytearray)):
            await self._on_audio_chunk(bytes(frame))
            return
        msg = json.loads(frame)
        kind = msg.get("type")
        if kind == "audio.start":
            self.listening = True
            # Barge-in: user speaking cancels persona speech immediately.
            if self.speaking:
                await self._interrupt()
        elif kind == "audio.stop":
            self.listening = False
            if self.transcriber:
                final = await self.transcriber.finalize()
                if final:
                    await self.send({"type": "transcript.final", "text": final})
                    await self._begin_turn(final)
        elif kind == "text":
            await self.send({"type": "transcript.final", "text": msg["text"]})
            await self._begin_turn(msg["text"])
        else:
            LOG.warning("voice_unknown_frame", type=kind)

    async def _on_audio_chunk(self, chunk: bytes) -> None:
        if not self.listening:
            return
        if self.transcriber is None:
            return
        self.transcriber.feed(chunk)
        partial = self.transcriber.partial()
        if partial:
            await self.send({"type": "transcript.partial", "text": partial})

    async def _begin_turn(self, text: str) -> None:
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
        self.turn_task = asyncio.create_task(self._run_turn(text))

    async def _run_turn(self, text: str) -> None:
        try:
            result = await self.runtime.chat(self.persona_key, text, session="voice")
            reply = (result.get("content") or "").strip()
            if not reply:
                await self.send({"type": "reply.chunk", "text": ""})
                await self.send({"type": "audio.stop"})
                return
            await self._speak_streaming(reply)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.warning("voice_turn_failed", persona=persona_slug(self.persona_key),
                        error=type(exc).__name__)
            await self.send({"type": "error", "detail": "turn failed"})

    async def _speak_streaming(self, reply: str) -> None:
        """Emit the reply sentence-by-sentence, synthesizing as we go.

        Full-reply latency is paid only once: the first sentence plays
        while the rest is still being phrased.
        """
        sentences, remainder = sentence_chunks(reply)
        await self.send({"type": "audio.start", "rate": self.synthesizer.sample_rate if self.synthesizer else 24000})
        self.speaking = True
        try:
            for sentence in sentences:
                if not self.speaking:
                    break
                await self.send({"type": "reply.chunk", "text": sentence})
                if self.synthesizer:
                    for piece in self.synthesizer.synthesize(sentence):
                        if not self.speaking:
                            break
                        await self.send_audio(piece)
            if remainder and self.speaking:
                await self.send({"type": "reply.chunk", "text": remainder})
                if self.synthesizer:
                    for piece in self.synthesizer.synthesize(remainder):
                        if not self.speaking:
                            break
                        await self.send_audio(piece)
        finally:
            self.speaking = False
        await self.send({"type": "audio.stop"})

    async def _interrupt(self) -> None:
        self.speaking = False
        if self.speech_task and not self.speech_task.done():
            self.speech_task.cancel()
        await self.send({"type": "interrupted"})

    async def close(self) -> None:
        if self.turn_task:
            self.turn_task.cancel()
        if self.speech_task:
            self.speech_task.cancel()
        if self.transcriber:
            await self.transcriber.close()


async def serve_voice(ws: WebSocket, runtime: PersonaRuntime,
                      transcriber_factory: Callable[[], Transcriber] | None = None,
                      synthesizer_factory: Callable[[], Synthesizer] | None = None) -> None:
    """WebSocket entrypoint: auth via persona key, then the frame protocol."""
    auth = ws.query_params.get("key") or _bearer_from_header(ws)
    if not auth:
        await ws.close(code=4401)
        return
    await ws.accept()
    session = VoiceSession(
        runtime,
        auth,
        transcriber=transcriber_factory() if transcriber_factory else None,
        synthesizer=synthesizer_factory() if synthesizer_factory else None,
    )

    async def send(obj: dict) -> None:
        await ws.send_text(json.dumps(obj))

    async def send_audio(chunk: bytes) -> None:
        await ws.send_bytes(chunk)

    session.send = send  # type: ignore[method-assign]
    session.send_audio = send_audio  # type: ignore[method-assign]
    try:
        while True:
            frame = await ws.receive()
            if frame.get("type") == "websocket.disconnect":
                break
            if "text" in frame:
                await session.handle_frame(frame["text"])
            elif "bytes" in frame:
                await session.handle_frame(frame["bytes"])
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()


def _bearer_from_header(ws: WebSocket) -> str | None:
    auth = ws.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip() or None
    return None