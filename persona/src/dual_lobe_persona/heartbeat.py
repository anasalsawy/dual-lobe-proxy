"""Heartbeat: the persona's initiative.

Periodically wakes each persona and asks whether anything from its real
memory is worth reaching out about. The candidates are not invented: they
are the proxy's own memory surfacing (unaddressed asides, mood shifts,
revision beats) injected into the agent's context on every call. The agent
composes through the proxy, so B verifies the outreach before it queues —
the persona cannot interrupt you with something it misremembers.
"""
from __future__ import annotations

import asyncio
import os

import structlog

from .outbox import Outbox
from .runtime import PersonaRuntime, persona_slug

LOG = structlog.get_logger(__name__)

HEARTBEAT_SECONDS = float(os.environ.get("DUAL_LOBE_PERSONA_HEARTBEAT_SECONDS", "3600"))
NO_OUTREACH_MARK = "NO_OUTREACH"

_HEARTBEAT_PROMPT = """\
[heartbeat] The user has been quiet for a while. Your memory automatically \
surfaces what matters: unaddressed things you noticed, mood shifts, changes \
of heart. Decide whether anything is genuinely worth reaching out about.

If something is — the user left a question hanging, mentioned something \
important happening today, or you owe them a follow-up — compose ONE short, \
warm, human message. Say it the way a friend who knows them would: brief, \
specific to them, no preamble, no announcing that you are "reaching out".

If nothing is worth interrupting them for, reply with exactly: NO_OUTREACH"""


class PersonaHeartbeat:
    """One heartbeat loop per registered persona key.

    The registry is in-memory: operators register personas via the admin
    endpoint or config at boot (DUAL_LOBE_PERSONA_KEYS, comma-separated).
    Each loop wakes on the heartbeat interval, runs the heartbeat prompt
    through the persona's agent conversation, and queues any outreach to
    the outbox (which enforces quiet hours and daily caps).
    """

    def __init__(self, runtime: PersonaRuntime, channel: str = "default",
                 address: str = "user") -> None:
        self.runtime = runtime
        self.channel = channel
        self.address = address
        self.interval_seconds = HEARTBEAT_SECONDS
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()

    def register(self, persona_key: str) -> None:
        if persona_key in self._tasks:
            return
        self._tasks[persona_key] = asyncio.create_task(self._loop(persona_key))
        LOG.info("heartbeat_registered", persona=persona_slug(persona_key),
                 channel=self.channel, interval=self.interval_seconds)

    def unregister(self, persona_key: str) -> None:
        task = self._tasks.pop(persona_key, None)
        if task:
            task.cancel()

    async def _loop(self, persona_key: str) -> None:
        # Stagger first beats so a fleet of personas does not thunder.
        await asyncio.sleep(HEARTBEAT_SECONDS)
        while not self._stop.is_set():
            try:
                await self.beat(persona_key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.warning("heartbeat_failed", persona=persona_slug(persona_key),
                            error=type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                continue

    async def beat(self, persona_key: str) -> dict:
        """Run one heartbeat for one persona. Returns what happened."""
        result = await self.runtime.chat(persona_key, _HEARTBEAT_PROMPT, session="heartbeat")
        text = (result.get("content") or "").strip()
        if not text or text.upper().startswith(NO_OUTREACH_MARK):
            LOG.info("heartbeat_quiet", persona=persona_slug(persona_key))
            return {"outreach": False}
        outbox = Outbox(persona_key)
        msg = outbox.append(self.channel, self.address, text, reason="heartbeat")
        if msg is None:
            LOG.info("heartbeat_refused_by_policy", persona=persona_slug(persona_key))
            return {"outreach": False, "refused": True}
        return {"outreach": True, "message_id": msg.id}

    def shutdown(self) -> None:
        self._stop.set()
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()