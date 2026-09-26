from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field

from .agents import make_b_adversary
from .json_utils import extract_json_object
from .memory import JsonlMemoryStore
from .prompts import ADVERSARIAL_PROTOCOL, OBSERVATION_DISCLAIMER
from .runner import run_one
from .tools import LiveBState, ProxyToolEvent, ProxyToolTrace


@dataclass
class LiveBMonitor:
    """Event-driven persistent B loop that runs concurrently with A.

    B does not spin wastefully when nothing changes. It wakes on meaningful
    runtime events, updates an independent state, and can send concise
    interventions back into A's tool stream while A is still working.
    """

    task: str
    b_memory: JsonlMemoryStore
    trace: ProxyToolTrace
    state: LiveBState
    max_calls: int = field(
        default_factory=lambda: max(1, int(os.getenv("DUAL_LOBE_B_LIVE_MAX_CALLS", "6")))
    )
    poll_seconds: float = field(
        default_factory=lambda: max(0.02, float(os.getenv("DUAL_LOBE_B_LIVE_POLL_SECONDS", "0.08")))
    )

    calls: int = 0
    _cursor: int = 0
    _stop: bool = False

    def stop(self) -> None:
        self._stop = True

    @staticmethod
    def _meaningful(events: list[ProxyToolEvent]) -> list[ProxyToolEvent]:
        # Ignore B's own trace plumbing to avoid self-trigger loops.
        return [
            e for e in events
            if not e.provenance.startswith("lobe_b_")
            and e.name not in {"b_live_observation", "b_live_intervention"}
        ]

    async def _observe(self, events: list[ProxyToolEvent]) -> None:
        if not events or self.calls >= self.max_calls:
            return

        b_memory = self.b_memory.auto_slice(self.task)
        rendered = "\n\n".join(
            f"EVENT {i+1}\nname={e.name}\nprovenance={e.provenance}\n"
            f"input={e.input_text}\noutput={e.output_text}"
            for i, e in enumerate(events)
        )

        prompt = f"""{OBSERVATION_DISCLAIMER}

You are Lobe B running CONTINUOUSLY alongside Lobe A while A is still executing.
This is not the final-answer gate. You are the live adversarial process.

ORIGINAL USER TASK:
{self.task}

B'S OWN PERSISTENT MEMORY:
{b_memory if b_memory else "(none)"}

NEW EXECUTION EVENTS FROM A / ITS DELEGATED CHILDREN:
{rendered}

{ADVERSARIAL_PROTOCOL}

Your live job:
- update your independent understanding of what A is doing;
- detect drift from the user's actual intent while it is happening;
- detect unsupported assumptions before they propagate;
- detect repeated failure, wasted loops, bad delegation, ignored evidence, brittle plans, or missing prerequisites;
- find reasons the current approach may not work;
- surface a missing fact or alternative that would materially change the approach;
- intervene ONLY when A can still benefit from changing course now.

Do not merely summarize events.
Do not invent external facts.
Do not ask the user questions unless the missing information is genuinely decision-critical.
If no intervention is needed, say so.

Return ONLY JSON:
{{
  "intervene": true,
  "severity": "info|warning|critical",
  "message": "concise instruction/challenge to A, or empty when no intervention is needed",
  "state_note": "what B learned for its independent ongoing state"
}}"""

        b = make_b_adversary(tools=None)
        try:
            raw = await run_one(
                b,
                prompt,
                "Strict JSON live adversarial observation.",
                role_key="B_VERIFY",
            )
            data = extract_json_object(raw) or {}
        except Exception as exc:
            self.state.record_state_note(f"Live B observation failed: {type(exc).__name__}: {exc}")
            self.calls += 1
            return

        self.calls += 1
        state_note = str(data.get("state_note") or "").strip()
        if state_note:
            self.state.record_state_note(state_note)
            await asyncio.to_thread(
                self.b_memory.record,
                f"[LIVE_B_STATE]\nTask: {self.task}\n{state_note}",
            )

        if bool(data.get("intervene")):
            message = str(data.get("message") or "").strip()
            severity = str(data.get("severity") or "warning").strip().lower()
            if message:
                self.state.add_intervention(message, severity=severity)
                self.trace.add(
                    "b_live_intervention",
                    input_text="live adversarial observation",
                    output_text=f"{severity.upper()}: {message}",
                    provenance="lobe_b_live_intervention",
                )
        else:
            self.trace.add(
                "b_live_observation",
                input_text="live adversarial observation",
                output_text=state_note or "No intervention needed.",
                provenance="lobe_b_live_observation",
            )

    async def run(self) -> None:
        # Initial wake-up gives B a chance to form an independent task view
        # concurrently with A before any tool event has occurred.
        initial = ProxyToolEvent(
            name="a_started",
            input_text=self.task,
            output_text="A began execution.",
            provenance="runtime_lifecycle",
        )
        await self._observe([initial])

        while not self._stop and self.calls < self.max_calls:
            await asyncio.sleep(self.poll_seconds)
            events = self.trace.snapshot_from(self._cursor)
            if not events:
                continue
            self._cursor += len(events)
            meaningful = self._meaningful(events)
            if meaningful:
                await self._observe(meaningful)

        # Flush any events that landed just before A completed.
        if self.calls < self.max_calls:
            events = self.trace.snapshot_from(self._cursor)
            self._cursor += len(events)
            meaningful = self._meaningful(events)
            if meaningful:
                await self._observe(meaningful)
