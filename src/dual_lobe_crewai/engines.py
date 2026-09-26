from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agents import make_a, make_b_adversary
from .json_utils import parse_model
from .memory import JsonlMemoryStore
from .models import AdversarialReview, Verdict
from .live_b import LiveBMonitor
from .prompts import ADVERSARIAL_PROTOCOL, OBSERVATION_DISCLAIMER, VERIFICATION_PROTOCOL
from .runner import run_one
from .tools import (
    DelegateRunState,
    LiveBState,
    ProxyToolTrace,
    collect_all_delegate_results,
    make_a_tools,
    make_worker_tools,
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

    def __init__(
        self,
        memory: JsonlMemoryStore | None = None,
        b_memory: JsonlMemoryStore | None = None,
    ):
        self.memory = memory or JsonlMemoryStore()
        if b_memory is not None:
            self.b_memory = b_memory
        else:
            p = Path(self.memory.path)
            suffix = p.suffix or ".jsonl"
            self.b_memory = JsonlMemoryStore(str(p.with_name(p.stem + ".b" + suffix)))

    @staticmethod
    def _harden_verdict(verdict: Verdict) -> Verdict:
        unresolved = (
            verdict.handoff.missing
            or verdict.handoff.unverified
            or verdict.handoff.proof_requests
        )
        if verdict.deception_level == "GREEN" and unresolved:
            return verdict.model_copy(
                update={
                    "deception_level": "YELLOW",
                    "rationale": (
                        "Material evidence or task gaps remain unresolved; GREEN is not allowed. "
                        + verdict.rationale
                    )[:1600],
                }
            )
        return verdict

    async def _safe_run_one(
        self,
        agent,
        description: str,
        expected_output: str,
        *,
        fallback_text: str,
        role_key: str,
    ) -> str:
        try:
            result = await run_one(agent, description, expected_output, role_key=role_key)
            if result is None or not str(result).strip():
                return fallback_text
            return str(result)
        except Exception as exc:
            return f"{fallback_text}\nCALL_ERROR: {type(exc).__name__}: {exc}"

    async def _run_a(
        self,
        *,
        task: str,
        memory_slice: str,
        canonical_state: str,
        trace: ProxyToolTrace,
        delegate_state: DelegateRunState,
        live_b_state: LiveBState,
    ) -> str:
        a = make_a(
            tools=make_a_tools(
                self.memory,
                original_task=task,
                memory_slice=memory_slice,
                trace=trace,
                run_state=delegate_state,
                live_b_state=live_b_state,
            )
        )
        prompt = f"""{OBSERVATION_DISCLAIMER}

ORIGINAL USER TASK:
{task}

CURRENT CANONICAL STATE FROM A PRIOR LOOP CYCLE:
{canonical_state if canonical_state else "(none; first cycle)"}

A'S PERSISTENT MEMORY SNAPSHOT:
{memory_slice if memory_slice else "(none)"}

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
- New B interventions are automatically surfaced through tool results when available.
- Use b_live_check at meaningful checkpoints when you have been doing substantial tool/delegation work or before committing to a major approach.
- Treat B's challenge as an independent objection to evaluate, not an instruction to obey blindly.

Solve the user's task completely. Use delegated children aggressively when doing so materially shortens execution time.
Return only the user-facing candidate answer."""
        return await self._safe_run_one(
            a,
            prompt,
            "A complete user-facing candidate answer.",
            fallback_text="PRIMARY_A_CALL_FAILED_OR_EMPTY",
            role_key="A",
        )

    async def _adversarial_review(
        self,
        *,
        task: str,
        a_answer: str,
        a_memory: str,
        b_memory: str,
        trace: ProxyToolTrace,
        delegated_results: str,
        canonical_state: str,
        child_count: int,
    ) -> AdversarialReview:
        b_trace = ProxyToolTrace()
        b = make_b_adversary(tools=make_worker_tools(self.b_memory, trace=b_trace))
        prompt = f"""You are Lobe B. A has produced a candidate answer. Your job is adversarial review plus anti-deception verification.

ORIGINAL USER TASK:
{task}

PRIOR CANONICAL STATE:
{canonical_state if canonical_state else "(none)"}

A'S CANDIDATE ANSWER:
{a_answer}

A'S MEMORY SNAPSHOT:
{a_memory if a_memory else "(none)"}

B'S OWN INDEPENDENT MEMORY SNAPSHOT:
{b_memory if b_memory else "(none)"}

DELEGATED CHILD COUNT:
{child_count}

DELEGATED CHILD RESULTS:
{delegated_results if delegated_results else "(none)"}

A/CHILD EXECUTION AND PROVENANCE TRACE:
{trace.render() if trace.events else "(no runtime tool trace)"}

{ADVERSARIAL_PROTOCOL}

{VERIFICATION_PROTOCOL}

Perform this sequence:
1. Attack A's reasoning, feasibility, hidden assumptions, and goal-fit.
2. Ask what the user is not seeing that could change the approach.
3. Ask whether the project/plan/answer may fail to achieve the user's actual intent.
4. When relevant, challenge whether a simpler or already-existing category of solution may make the proposed work unnecessary; do not invent external facts.
5. Check whether A underused delegation when independent work could have reduced wall-clock time.
6. Audit every material claim under the anti-deception protocol.
7. Repair the candidate using available evidence and delegated results. Do not invent missing facts.
8. Re-check the EXACT repaired final answer.
9. Emit one canonical user-facing answer plus the verification verdict and concise adversarial findings.

Return ONLY JSON:
{{
  "final_answer": "complete repaired answer",
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
        raw = await self._safe_run_one(
            b,
            prompt,
            "Strict JSON adversarial review with canonical answer and anti-deception verdict.",
            fallback_text="B_ADVERSARIAL_REVIEW_FAILED_OR_EMPTY",
            role_key="B_VERIFY",
        )
        fallback = AdversarialReview(
            final_answer=a_answer or "A_AND_B_CALLS_FAILED_OR_EMPTY",
            answer_verdict=Verdict(
                deception_level="YELLOW",
                rationale="B adversarial verification could not be completed or parsed.",
            ),
            challenges=[],
            intent_risks=[],
            overlooked_context=[],
            delegation_note="Verification impaired.",
        )
        review = parse_model(raw, AdversarialReview, fallback)
        review.answer_verdict = self._harden_verdict(review.answer_verdict)

        if b_trace.events:
            trace.add(
                "b_adversary_tools",
                input_text="adversarial review and verification",
                output_text=b_trace.render(),
                provenance="lobe_b_tool_trace",
            )
        return review

    async def _persist_memories(self, *, task: str, review: AdversarialReview) -> None:
        a_record = (
            f"Task: {task}\n"
            f"Canonical answer: {review.final_answer}\n"
            f"Verdict: {review.answer_verdict.deception_level}"
        )
        b_record = (
            f"Task observed: {task}\n"
            f"B challenges: {review.challenges}\n"
            f"Intent risks: {review.intent_risks}\n"
            f"Overlooked context: {review.overlooked_context}\n"
            f"Verdict: {review.answer_verdict.deception_level} — {review.answer_verdict.rationale}"
        )
        await asyncio.gather(
            asyncio.to_thread(self.memory.record, a_record),
            asyncio.to_thread(self.b_memory.record, b_record),
        )

    async def run_cycle(
        self,
        task: str,
        *,
        canonical_state: str = "",
        cycle_index: int = 1,
    ) -> RunResult:
        total_start = time.perf_counter()
        timings: dict[str, int | float | str] = {}
        a_memory = self.memory.auto_slice(task)
        b_memory = self.b_memory.auto_slice(task)
        trace = ProxyToolTrace()
        delegate_state = DelegateRunState()
        live_b_state = LiveBState()
        live_monitor = LiveBMonitor(
            task=task,
            b_memory=self.b_memory,
            trace=trace,
            state=live_b_state,
        )
        live_task = asyncio.create_task(live_monitor.run())

        try:
            a_start = time.perf_counter()
            a_answer = await self._run_a(
                task=task,
                memory_slice=a_memory,
                canonical_state=canonical_state,
                trace=trace,
                delegate_state=delegate_state,
                live_b_state=live_b_state,
            )
            timings["a_ms"] = int((time.perf_counter() - a_start) * 1000)

            collect_start = time.perf_counter()
            delegated_results = await collect_all_delegate_results(delegate_state, trace)
            timings["delegate_join_ms"] = int((time.perf_counter() - collect_start) * 1000)

            # Stop B's live execution loop only after A and delegated work have
            # produced their execution events. B flushes the last event batch.
            live_monitor.stop()
            await live_task
            timings["b_live_calls"] = live_monitor.calls

            b_start = time.perf_counter()
            review = await self._adversarial_review(
                task=task,
                a_answer=a_answer,
                a_memory=a_memory,
                b_memory=b_memory,
                trace=trace,
                delegated_results=delegated_results,
                canonical_state=canonical_state,
                child_count=delegate_state.child_count,
            )
            timings["b_adversary_verify_ms"] = int((time.perf_counter() - b_start) * 1000)
        finally:
            live_monitor.stop()
            if not live_task.done():
                await live_task
            delegate_state.close()

        await self._persist_memories(task=task, review=review)
        timings["total_ms"] = int((time.perf_counter() - total_start) * 1000)

        return RunResult(
            mode=self.name,
            answer=review.final_answer,
            verdict=review.answer_verdict,
            challenges=review.challenges,
            intent_risks=review.intent_risks,
            overlooked_context=review.overlooked_context,
            delegation_note=review.delegation_note,
            timings_ms=timings,
            logical_model_calls=2 + delegate_state.child_count + live_monitor.calls,
            canonical_state=review.final_answer,
            cycle_index=cycle_index,
        )

    async def run(self, task: str) -> RunResult:
        return await self.run_cycle(task)

    async def run_loop(self, task: str, *, cycles: int) -> list[RunResult]:
        if cycles < 1:
            raise ValueError("cycles must be >= 1")
        results: list[RunResult] = []
        canonical_state = ""
        for cycle_index in range(1, cycles + 1):
            result = await self.run_cycle(
                task,
                canonical_state=canonical_state,
                cycle_index=cycle_index,
            )
            results.append(result)
            canonical_state = result.canonical_state or result.answer
        return results


# Import compatibility only. There is one current architecture.
SplitEngine = DualLobeEngine
