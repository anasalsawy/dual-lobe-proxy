"""Streaming TTS provider seam.

Synthesis is sentence-granular by contract: the session hands one
sentence at a time and expects audio pieces back immediately — that is
what keeps first-word latency at one-sentence cost instead of
full-reply cost. Configure via DUAL_LOBE_PERSONA_TTS.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator


class Synthesizer(ABC):
    """One synthesis stream for one voice session."""

    sample_rate: int = 24000

    @abstractmethod
    def synthesize(self, text: str) -> Iterator[bytes]:
        """Synthesize one sentence; yields raw audio pieces."""


class NullSynthesizer(Synthesizer):
    """No-op: emits no audio; the client renders text chunks instead."""

    def synthesize(self, text: str) -> Iterator[bytes]:
        return iter(())