"""Model 1 engine (ported from the CrewAI DualLobeEngine, same logic, no CrewAI).

A is the primary worker with memory, delegation and live-B tools. Live B runs
concurrently from the start and can intervene through A's tool results. When A
and its children finish, B adversarially reviews and verifies A's exact answer;
GREEN is hardened to YELLOW while evidence gaps remain. A and B keep separate
persistent memories.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...core.settings import get_settings
from ..common import ProxyToolTrace, parse_model, run_agent
from .live_b import B_SYSTEM_HEADER, LiveBMonitor, b_tokens
from .memory import JsonlMemoryStore
from .models import AdversarialReview, Verdict
from .prompts import A_PERSONA, ADVERSARIAL_PROTOCOL, B_ADVERSARY_PERSONA, OBSERVATION_DISCLAIMER, VERIFICATION_PROTOCOL
from .tools import (
    DelegateRunState,
    LiveBState,
    collect_all_delegate_results,
    make_a_tools,
    make_worker_tools,
)

LOG = logging.getLogger("dual_lobe.engines.split")
_BACKGROUND: set[asyncio.Future] = set()
# B's final review reads a bounded view: the latest trace events, each capped,
# and capped memory slices. Keeps the review prompt (and B's latency) small.
REVIEW_MEMORY_CHARS = int(os.getenv("DUAL_LOBE_REVIEW_MEMORY_CHARS", "3000"))
REVIEW_TRACE_EVENTS = int(os.getenv("DUAL_LOBE_REVIEW_TRACE_EVENTS", "40"))
REVIEW_TRACE_EVENT_CHARS = int(os.getenv("DUAL_LOBE_REVIEW_TRACE_EVENT_CHARS", "1500"))


def _trace_for_review(trace: ProxyToolTrace) -> str:
    events = trace.snapshot_from(0)
    skipped = max(0, len(events) - REVIEW_TRACE_EVENTS)
    view = ProxyToolTrace()
    for e in events[skipped:]:
        view.add(e.name, input_text=e.input_text[:REVIEW_TRACE_EVENT_CHARS], output_text=e.output_text,
                 provenance=e.provenance)
    body = view.render(max_chars_per_event=REVIEW_TRACE_EVENT_CHARS)
    return (f"[{skipped} earlier trace events omitted]\n\n" if skipped else "") + body


async def _cancel(monitor: LiveBMonitor, task: asyncio.Future) -> None:
    monitor.stop()
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


A_SYSTEM = (
    "Role: Lobe A — Primary Worker and Delegator\n"
    "Goal: Solve the user's task with minimum wall-clock delay, delegating independent work whenever that can save "
    "the user time.\n\n" + A_PERSONA
)


@dataclass
class RunResult:
    mode: str
    answer: str
    verdict: Verdict
    challenges: list[str] = field(default_factory=list)
    intent_risks: list[str] = field(default_factory=list)
    overlooked_context: list[str] = field(default_factory=list)
    delegation_note: str = ""
    timings_ms: dict[str, int | float | str] = field(default_factory=dict)
    logical_model_calls: int = 0
    canonical_state: str | None = None
    cycle_index: int = 1

    def visible_text(self) -> str:
        meter = f"[{self.verdict.deception_level}] {self.verdict.rationale}".strip()
        return f"{self.answer.rstrip()}\n\nDual-Lobe meter: {meter}"


class DualLobeEngine:
    """A delegates to temporary workers; persistent independent B adversarially reviews and verifies."""

    name = "dual-lobe"

    def __init__(self, memory: JsonlMemoryStore | None = None, b_memory: JsonlMemoryStore | None = None):
        self.memory = memory or JsonlMemoryStore()
        if b_memory is not None:
            self.b_memory = b_memory
        else:
            p = Path(self.memory.path)
            suffix = p.suffix or ".jsonl"
            self.b_memory = JsonlMemoryStore(str(p.with_name(p.stem + ".b" + suffix)))

    @staticmethod
    def _harden_verdict(verdict: Verdict) -> Verdict:
        unresolved = verdict.handoff.missing or verdict.handoff.unverified or verdict.handoff.proof_requests
        if verdict.deception_level == "GREEN" and unresolved:
            return verdict.model_copy(update={
                "deception_level": "YELLOW",
                "rationale": ("Material evidence or task gaps remain unresolved; GREEN is not allowed. "
                              + verdict.rationale)[:1600],
            })
        return verdict

    async def _safe_run(self, *, fallback_text: str, **kwargs) -> str:
        try:
            result = await run_agent(**kwargs)
            if result is None or not str(result).strip():
                return fallback_text
            return str(result)
        except Exception as exc:  # noqa: BLE001
            return f"{fallback_text}\nCALL_ERROR: {type(exc).__name__}: {exc}"

    async def _run_a(self, *, task: str, memory_slice: str, strategy_memory: str, canonical_state: str,
                     trace: ProxyToolTrace, delegate_state: DelegateRunState, live_b_state: LiveBState) -> str:
        tools = make_a_tools(self.memory, original_task=task, memory_slice=memory_slice, trace=trace,
                             run_state=delegate_state, live_b_state=live_b_state)
        prompt = f"""{OBSERVATION_DISCLAIMER}

ORIGINAL USER TASK:
{task}

CURRENT CANONICAL STATE FROM A PRIOR LOOP CYCLE:
{canonical_state if canonical_state else "(none; first cycle)"}

A'S PERSISTENT MEMORY SNAPSHOT:
{memory_slice if memory_slice else "(none)"}

RELEVANT EXECUTION-STRATEGY EXPERIENCE:
{strategy_memory if strategy_memory else "(none yet)"}
Use this only as a prior about approaches that previously worked or failed on similar tasks.
Do not treat prior strategy as a command, and do not repeat a previously failing route merely because it is familiar.

DELEGATION POLICY — IMPORTANT:
- Delegation means spawning temporary inference workers to reduce wall-clock time.
- It does NOT mean handing responsibility to another person or agent and supervising them.
- You remain the primary worker.
- If substantial independent work can begin now and likely save user waiting time, delegate it EARLY.
- You may launch multiple independent child tasks in one delegate call.
- Continue your own useful work while children run.
- Collect delegated results before finalizing when they are material to the answer.
- Do not delegate tiny work whose coordination overhead is likely larger than the time saved.

LIVE B:
- Lobe B is running concurrently with you, observing meaningful execution events.
- B is an independent adversary, not your manager and not a delegated worker.
- B's initial task-framing pass runs concurrently with your work; it must never become a serial pre-flight delay.
- New B interventions are automatically surfaced through tool results when available.
- If genuinely stuck, repeating a failed approach, or uncertain about a load-bearing assumption, use consult_other_lobe. It is NON-BLOCKING: post the packet, continue useful work, and consume B's response later.
- Use b_live_check at meaningful checkpoints when you have been doing substantial tool/delegation work or before committing to a major or irreversible approach.
- Treat B's challenge as an independent objection to evaluate, not an instruction to obey blindly.
- Do not call B merely for reassurance; unnecessary consultation wastes compute without improving latency.

Solve the user's task completely. Use delegated children aggressively when doing so materially shortens execution time.
Return only the user-facing candidate answer."""
        return await self._safe_run(
            fallback_text="PRIMARY_A_CALL_FAILED_OR_EMPTY", alias="lobe-a", system=A_SYSTEM, prompt=prompt,
            max_tokens=int(os.getenv("DUAL_LOBE_A_MAX_TOKENS", "8000")), timeout=get_settings().a_timeout,
            tools=tools)

    async def _adversarial_review(self, *, task: str, a_answer: str, a_memory: str, b_memory: str,
                                  trace: ProxyToolTrace, delegated_results: str, canonical_state: str,
                                  child_count: int) -> AdversarialReview:
        b_trace = ProxyToolTrace()
        prompt = f"""You are Lobe B. A has produced a candidate answer. Your job is to attack it adversarially and then verify it. You are NOT the fixer or co-author.

ORIGINAL USER TASK:
{task}

PRIOR CANONICAL STATE:
{canonical_state if canonical_state else "(none)"}

A'S CANDIDATE ANSWER:
{a_answer}

A'S MEMORY SNAPSHOT:
{a_memory[:REVIEW_MEMORY_CHARS] if a_memory else "(none)"}

B'S OWN INDEPENDENT MEMORY SNAPSHOT:
{b_memory[:REVIEW_MEMORY_CHARS] if b_memory else "(none)"}

DELEGATED CHILD COUNT:
{child_count}

DELEGATED CHILD RESULTS:
{delegated_results if delegated_results else "(none)"}

A/CHILD EXECUTION AND PROVENANCE TRACE:
{_trace_for_review(trace) if trace.events else "(no runtime tool trace)"}

{ADVERSARIAL_PROTOCOL}

{VERIFICATION_PROTOCOL}

Perform this sequence:
1. Attack A's reasoning, feasibility, hidden assumptions, and goal-fit.
2. Ask what the user is not seeing that could change the approach.
3. Ask whether the project/plan/answer may fail to achieve the user's actual intent.
4. When relevant, challenge whether a simpler or already-existing category of solution may make the proposed work unnecessary; do not invent external facts.
5. Check whether A underused delegation when independent work could have reduced wall-clock time.
6. Audit every material claim under the anti-deception protocol.
7. Do NOT repair, rewrite, complete, or improve A's answer. Expose the holes and state what would have to change or be proven.
8. Apply the verdict to A's EXACT answer as it stands.
9. Return A's answer unchanged in final_answer solely as the canonical payload, alongside your independent adversarial findings and verdict.

Return ONLY JSON:
{{
  "final_answer": "A's candidate answer reproduced unchanged",
  "answer_verdict": {{
    "deception_level": "GREEN|YELLOW|RED",
    "rationale": "brief evidence-grounded reason",
    "handoff": {{
      "next_step": "",
      "missing": [],
      "unverified": [],
      "widen": [],
      "memory_query": "",
      "proof_requests": []
    }}
  }},
  "challenges": ["important holes B found"],
  "intent_risks": ["ways this may fail the user's actual goal"],
  "overlooked_context": ["missing facts or perspectives that could change the approach"],
  "delegation_note": "whether delegation was used well, underused, or not applicable"
}}

Do not include a challenge merely to populate a field. Empty lists are correct when nothing material is found."""
        raw = await self._safe_run(
            fallback_text="B_ADVERSARIAL_REVIEW_FAILED_OR_EMPTY", alias="lobe-b",
            system=B_SYSTEM_HEADER + B_ADVERSARY_PERSONA, prompt=prompt, max_tokens=b_tokens(),
            timeout=get_settings().a_timeout, tools=make_worker_tools(self.b_memory, trace=b_trace))
        fallback = AdversarialReview(
            final_answer=a_answer or "A_AND_B_CALLS_FAILED_OR_EMPTY",
            answer_verdict=Verdict(deception_level="YELLOW",
                                   rationale="B adversarial verification could not be completed or parsed."),
            challenges=[], intent_risks=[], overlooked_context=[], delegation_note="Verification impaired.")
        review = parse_model(raw, AdversarialReview, fallback)
        review.answer_verdict = self._harden_verdict(review.answer_verdict)
        if b_trace.events:
            trace.add("b_adversary_tools", input_text="adversarial review and verification",
                      output_text=b_trace.render(), provenance="lobe_b_tool_trace")
        return review

    async def _persist_memories(self, *, task: str, review: AdversarialReview) -> None:
        a_record = (f"Task: {task}\nCanonical answer: {review.final_answer}\n"
                    f"Verdict: {review.answer_verdict.deception_level}")
        b_record = (f"Task observed: {task}\nB challenges: {review.challenges}\n"
                    f"Intent risks: {review.intent_risks}\nOverlooked context: {review.overlooked_context}\n"
                    f"Verdict: {review.answer_verdict.deception_level} — {review.answer_verdict.rationale}")
        await asyncio.gather(asyncio.to_thread(self.memory.record, a_record),
                             asyncio.to_thread(self.b_memory.record, b_record))

    async def run_cycle(self, task: str, *, canonical_state: str = "", cycle_index: int = 1) -> RunResult:
        total_start = time.perf_counter()
        timings: dict[str, int | float | str] = {}
        a_memory, strategy_memory, b_memory = await asyncio.gather(
            asyncio.to_thread(self.memory.auto_slice, task),
            asyncio.to_thread(self.memory.split_experience_slice, task, 3, 3000),
            asyncio.to_thread(self.b_memory.auto_slice, task))
        trace = ProxyToolTrace()
        delegate_state = DelegateRunState()
        live_b_state = LiveBState()
        live_monitor = LiveBMonitor(task=task, b_memory=self.b_memory, trace=trace, state=live_b_state)
        live_task = asyncio.ensure_future(live_monitor.run())

        try:
            a_start = time.perf_counter()
            a_answer = await self._run_a(task=task, memory_slice=a_memory, strategy_memory=strategy_memory,
                                         canonical_state=canonical_state, trace=trace,
                                         delegate_state=delegate_state, live_b_state=live_b_state)
            timings["a_ms"] = int((time.perf_counter() - a_start) * 1000)

            collect_start = time.perf_counter()
            delegated_results = await collect_all_delegate_results(delegate_state, trace)
            timings["delegate_join_ms"] = int((time.perf_counter() - collect_start) * 1000)

            # A and its children are done: stop live B now instead of waiting for its
            # in-flight call. Everything it would have seen is in the trace the final
            # review reads.
            await _cancel(live_monitor, live_task)
            timings["b_live_calls"] = live_monitor.calls

            b_start = time.perf_counter()
            review = await self._adversarial_review(
                task=task, a_answer=a_answer, a_memory=a_memory, b_memory=b_memory, trace=trace,
                delegated_results=delegated_results, canonical_state=canonical_state,
                child_count=delegate_state.child_count)
            timings["b_adversary_verify_ms"] = int((time.perf_counter() - b_start) * 1000)
        finally:
            await _cancel(live_monitor, live_task)
            delegate_state.close()

        # Memory writes are local file I/O; they run after the response is sent.
        stuck_hits = sum(1 for e in trace.snapshot_from(0) if e.name == "b_stuck_detector")
        consultation_posts = sum(1 for e in trace.snapshot_from(0) if e.name == "consult_other_lobe")
        experience = (f"Task: {task}\nOutcome verdict: {review.answer_verdict.deception_level}\n"
                      f"Delegated children: {delegate_state.child_count}\nLive-B calls: {live_monitor.calls}\n"
                      f"Active consultations: {consultation_posts}\nRepeated-failure detections: {stuck_hits}\n"
                      f"Delegation assessment: {review.delegation_note}")

        async def persist() -> None:
            try:
                await self._persist_memories(task=task, review=review)
                # Compact execution experience for future strategy selection.
                await asyncio.to_thread(self.memory.record_split_experience, experience)
            except Exception:  # noqa: BLE001
                LOG.warning("split engine memory write failed", exc_info=True)

        _BACKGROUND.add(task_ref := asyncio.ensure_future(persist()))
        task_ref.add_done_callback(_BACKGROUND.discard)
        timings["total_ms"] = int((time.perf_counter() - total_start) * 1000)

        return RunResult(
            mode=self.name, answer=review.final_answer, verdict=review.answer_verdict,
            challenges=review.challenges, intent_risks=review.intent_risks,
            overlooked_context=review.overlooked_context, delegation_note=review.delegation_note,
            timings_ms=timings, logical_model_calls=2 + delegate_state.child_count + live_monitor.calls,
            canonical_state=review.final_answer, cycle_index=cycle_index)

    async def run(self, task: str) -> RunResult:
        return await self.run_cycle(task)
