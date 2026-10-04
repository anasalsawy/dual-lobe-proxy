"""Streaming ASR provider seam.

Feeds audio chunks in, yields interim partials, finalizes a turn on
mic close. Implement one class per provider (Deepgram, OpenAI Realtime,
Whisper streaming, ...); configure via DUAL_LOBE_PERSONA_ASR. With no
provider configured, voice sessions run over typed frames only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Transcriber(ABC):
    """One live transcription stream (one open mic)."""

    @abstractmethod
    def feed(self, chunk: bytes) -> None:
        """Accept one raw audio chunk from the open mic."""

    @abstractmethod
    def partial(self) -> str | None:
        """Best-guess transcript so far, or None when nothing new."""

    @abstractmethod
    async def finalize(self) -> str | None:
        """Close the stream and return the final transcript for the turn."""

    @abstractmethod
    async def close(self) -> None:
        """Release provider resources."""


class NullTranscriber(Transcriber):
    """No-op: typed frames only; audio chunks are ignored."""

    def feed(self, chunk: bytes) -> None:
        return

    def partial(self) -> str | None:
        return None

    async def finalize(self) -> str | None:
        return None

    async def close(self) -> None:
        return