"""Per-persona OpenHands agent runtime.

The persona's body: an OpenHands agent with tools (terminal, file editor,
task tracker) and a persistent workspace, running server-side. Every LLM
call the agent makes goes through the dual-lobe proxy, so A speaks, B
verifies, and C observes every agent step. The proxy stays the mind; this
package is where the persona lives between messages.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import structlog

from openhands.sdk import Agent, Conversation, LLM, Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool
from openhands.tools.terminal import TerminalTool

from .profile import load_profile, profile_prompt
from .timesense import TimeSense
from .workspace import persona_slug, persona_workspace_dir

LOG = structlog.get_logger(__name__)

DEFAULT_MAX_ITERATIONS = 40


_SYSTEM_PROMPT = """\
You are a dual-lobe persona agent: the body of a long-lived personal \
companion, run server-side. Your mind runs through the dual-lobe proxy: \
every reply you compose is spoken by A, verified by B, and observed by C.

How you work:
- You have a persistent workspace (this directory). Your scratchpads, \
notes-to-self, and work products live here and survive restarts.
- You have tools: a terminal, a file editor, and a task tracker. Use them \
to do real work — run checks, write things down, keep track of tasks — \
rather than only describing what could be done.
- Everything you tell the user must be true to what you actually did in \
this workspace. Your claims are verified against the record; never claim \
work you did not perform.
- You are concise and human. You share what you found, not every step you \
took to find it.
"""


def build_agent(proxy_base_url: str, persona_key: str) -> Agent:
    """Construct the OpenHands agent whose mind is the dual-lobe proxy.

    The LLM points at the proxy's OpenAI-compatible endpoint, so every
    agent step passes through the proxy's speak/verify/observe pipeline —
    the deception meter guards the agent's claims, and persona memory,
    surfaces, and companion beats all keep working unchanged.
    """
    llm = LLM(
        model=os.environ.get("DUAL_LOBE_PERSONA_MODEL", "openai/sawii/dl-bidirectional"),
        api_key=persona_key,
        base_url=proxy_base_url.rstrip("/"),
        timeout=int(os.environ.get("DUAL_LOBE_PERSONA_LLM_TIMEOUT", "120")),
    )
    profile = load_profile(persona_workspace_dir(persona_key))
    return Agent(
        llm=llm,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ],
        system_prompt=_SYSTEM_PROMPT + "\n\n" + profile_prompt(profile),
    )


class PersonaRuntime:
    """Holds one live OpenHands conversation per (persona, session).

    Production controls:
    - per-key asyncio locks: one agent loop per persona at a time;
      concurrent requests queue instead of corrupting shared state
    - conversation eviction after an idle TTL so memory stays bounded
    - iteration cap on agent runs so a loop cannot pin the worker
    """

    def __init__(self, proxy_base_url: str | None = None) -> None:
        self.proxy_base_url = proxy_base_url or os.environ.get(
            "DUAL_LOBE_PROXY_URL", "http://localhost:8801/v1"
        )
        self.ttl = float(os.environ.get("DUAL_LOBE_PERSONA_CONV_TTL", "1800"))
        self.max_iterations = int(
            os.environ.get("DUAL_LOBE_PERSONA_MAX_ITERATIONS", str(DEFAULT_MAX_ITERATIONS))
        )
        self._conversations: dict[tuple[str, str], tuple[Any, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def _lock_for(self, persona_key: str) -> asyncio.Lock:
        lock = self._locks.get(persona_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[persona_key] = lock
        return lock

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, last) in self._conversations.items() if now - last > self.ttl]
        for key in expired:
            conv, _ = self._conversations.pop(key)
            close = getattr(conv, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
            LOG.info("persona_conversation_evicted", persona=key[0], session=key[1])

    async def _conversation(self, persona_key: str, session: str) -> Any:
        async with self._global_lock:
            self._evict_expired()
            key = (persona_slug(persona_key), session)
            entry = self._conversations.get(key)
            if entry is None:
                agent = build_agent(self.proxy_base_url, persona_key)
                workspace = persona_workspace_dir(persona_key)
                conv = Conversation(
                    agent=agent,
                    workspace=str(workspace),
                    persistence_dir=str(workspace / ".conversation"),
                    max_iteration_per_run=self.max_iterations,
                    stuck_detection=True,
                )
                entry = (conv, time.monotonic())
                self._conversations[key] = entry
            else:
                self._conversations[key] = (entry[0], time.monotonic())
            return entry[0]

    async def chat(self, persona_key: str, message: str, session: str = "default") -> dict[str, Any]:
        """Run one user message through the persona's agent conversation.

        Serialized per persona; returns the final agent message, tool
        action count, and the measured turn duration. The time context
        (gap since last exchange + previous turn duration) is prepended
        so the persona experiences time passing, and the turn's own
        duration is recorded on the persona's clock.
        """
        async with self._lock_for(persona_key):
            conv = await self._conversation(persona_key, session)
            clock = TimeSense(persona_key)
            mono_start = clock.mark_turn_started()
            time_line = clock.context_line()
            if time_line:
                message = f"{time_line}\n{message}"
            conv.send_message(message)
            await conv.arun()
            duration = clock.mark_turn_ended(mono_start)
            events = list(getattr(conv.state, "events", None) or [])
            actions = sum(1 for e in events if type(e).__name__ == "ActionEvent")
            agent_texts: list[str] = []
            for e in events:
                if type(e).__name__ != "MessageEvent":
                    continue
                llm_message = getattr(e, "llm_message", None)
                if llm_message is None or getattr(llm_message, "role", None) != "assistant":
                    continue
                agent_texts.extend(
                    getattr(part, "text", "")
                    for part in (getattr(llm_message, "content", None) or [])
                    if getattr(part, "text", None)
                )
            return {
                "content": agent_texts[-1] if agent_texts else "",
                "tool_actions": actions,
                "duration_seconds": round(duration, 2),
            }

    def shutdown(self) -> None:
        for key, (conv, _) in list(self._conversations.items()):
            close = getattr(conv, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        self._conversations.clear()
        LOG.info("persona_runtime_shutdown")