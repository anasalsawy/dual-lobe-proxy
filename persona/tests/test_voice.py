"""Tests for the realtime voice protocol: turn flow, sentence chunking, barge-in."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import dual_lobe_persona.api as api
from dual_lobe_persona.voice import VoiceSession, sentence_chunks


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.next_reply = "First sentence here. Second one follows. And a tail"

    async def chat(self, persona_key: str, message: str, session: str = "default") -> dict:
        self.calls.append(message)
        return {"content": self.next_reply, "tool_actions": 0, "duration_seconds": 1.2}


class MemorySocket:
    """Minimal WebSocket stand-in exercising VoiceSession directly."""

    def __init__(self) -> None:
        self.sent: list[dict | bytes] = []

    async def send(self, obj: dict) -> None:
        self.sent.append(obj)

    async def send_audio(self, chunk: bytes) -> None:
        self.sent.append(chunk)

    async def feed_text(self, text: str) -> None:
        await self.session.handle_frame(json.dumps({"type": "text", "text": text}))

    async def feed_audio_start(self) -> None:
        await self.session.handle_frame(json.dumps({"type": "audio.start"}))

    async def feed_audio_stop(self) -> None:
        await self.session.handle_frame(json.dumps({"type": "audio.stop"}))


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_PERSONA_SANDBOX_ROOT", str(tmp_path))
    rt = FakeRuntime()
    s = VoiceSession(rt, "persona-key-1")
    sock = MemorySocket()
    s.send = sock.send  # type: ignore[method-assign]
    s.send_audio = sock.send_audio  # type: ignore[method-assign]
    sock.session = s
    return s, sock, rt


def test_sentence_chunks_split():
    complete, remainder = sentence_chunks("One sentence. Two sentences! Three? A tail")
    assert complete == ["One sentence.", "Two sentences!", "Three?"]
    assert remainder == "A tail"


def test_sentence_chunks_all_complete():
    complete, remainder = sentence_chunks("Only one.")
    assert complete == ["Only one."]
    assert remainder == ""


def test_text_frame_runs_turn_and_streams_sentences(session):
    s, sock, rt = session
    asyncio.run(s.handle_frame(json.dumps({"type": "text", "text": "hey there"})))
    # Wait for the turn task
    asyncio.run(_drain(s))
    kinds = [m["type"] for m in sock.sent if isinstance(m, dict)]
    assert "transcript.final" in kinds
    # "First sentence here. Second one follows. And a tail"
    # = 2 complete sentences + 1 remainder = 3 speakable chunks
    assert kinds.count("reply.chunk") == 3
    assert kinds.index("audio.start") < kinds.index("reply.chunk")
    assert kinds[-1] == "audio.stop"
    assert rt.calls[0].endswith("hey there")  # time context prepended, message intact


async def _drain(session: VoiceSession) -> None:
    if session.turn_task:
        await session.turn_task


def test_barge_in_interrupts_speech(session):
    s, sock, rt = session
    s.speaking = True
    asyncio.run(s.handle_frame(json.dumps({"type": "audio.start"})))
    kinds = [m["type"] for m in sock.sent if isinstance(m, dict)]
    assert "interrupted" in kinds
    assert s.speaking is False


def test_no_asr_provider_ignores_audio_chunks(session):
    s, sock, rt = session
    asyncio.run(s.handle_frame(b"\x00\x01\x02"))
    asyncio.run(s.handle_frame(json.dumps({"type": "audio.stop"}))
                if False else s.handle_frame(json.dumps({"type": "audio.stop"})))
    # No transcript.final because no transcriber
    kinds = [m["type"] for m in sock.sent if isinstance(m, dict)]
    assert "transcript.final" not in kinds
    assert rt.calls == []


def test_empty_reply_closes_cleanly(session):
    s, sock, rt = session
    rt.next_reply = "   "
    asyncio.run(s.handle_frame(json.dumps({"type": "text", "text": "hi"})))
    asyncio.run(_drain(s))
    kinds = [m["type"] for m in sock.sent if isinstance(m, dict)]
    assert kinds[-1] == "audio.stop"


def test_voice_websocket_requires_auth():
    with TestClient(api.app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/v1/persona/voice"):
                pass