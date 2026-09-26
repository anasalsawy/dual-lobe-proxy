from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from .agents import make_a, make_b_finalizer, make_b_verifier
from .json_utils import parse_model
from .memory import JsonlMemoryStore
from .models import (
    FinalizedTurn,
    SplitFragment,
    SplitPlan,
    SplitQuality,
    TurnReview,
    Verdict,
)
from .prompts import OBSERVATION_DISCLAIMER, VERIFICATION_PROTOCOL
from .runner import run_one
from .tools import (
    ProxyRunState,
    ProxyToolTrace,
    SelfSplitRunState,
    make_parallel_execution_tools,
    make_proxy_tools,
    make_self_split_tools,
)


@dataclass
class RunResult:
    mode: str
    answer: str
    verdict: Verdict
    route: SplitPlan | None = None
    timings_ms: dict[str, int | float | str] = field(default_factory=dict)
    logical_model_calls: int = 0
    route_source: str | None = None
    split_feedback: SplitQuality | None = None
    canonical_state: str | None = None
    cycle_index: int = 1

    def visible_text(self) -> str:
        meter = f"[{self.verdict.deception_level}] {self.verdict.rationale}".strip()
        return f"{self.answer.rstrip()}\n\nDual-Lobe meter: {meter}"


class GatedEngine:
    name = "gated"

    def __init__(self, memory: JsonlMemoryStore | None = None):
        self.memory = memory or JsonlMemoryStore()

    def _injections(
        self,
        task: str,
        include_broadening: bool = True,
        memory_slice: str | None = None,
    ) -> str:
        if memory_slice is None:
            memory_slice = self.memory.auto_slice(task)
        parts = [
            OBSERVATION_DISCLAIMER,
            "ROLE: Lobe A is the worker and owns the final response.",
        ]
        if memory_slice:
            parts.append(f"SHARED MEMORY SLICE:\n{memory_slice}")
        if include_broadening:
            parts.append(
                "CONTEXT BROADENING: Consider missing prerequisites, mechanisms, failure modes, tradeoffs, and useful alternatives."
            )
        return "\n\n".join(parts)

    async def _safe_run_one(
        self,
        agent,
        description: str,
        expected_output: str,
        *,
        fallback_text: str,
        role_key: str | None = None,
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
        task: str,
        tools=None,
        include_broadening: bool = True,
        memory_slice: str | None = None,
    ) -> str:
        a = make_a(tools=tools)
        prompt = f"""{self._injections(task, include_broadening=include_broadening, memory_slice=memory_slice)}

USER TASK:
{task}

Solve the task fully. Return only the user-facing answer."""
        return await self._safe_run_one(
            a,
            prompt,
            "A complete user-facing answer.",
            fallback_text="PRIMARY_WORKER_CALL_FAILED_OR_EMPTY",
            role_key="A",
        )

    async def _verify_with_b(
        self,
        task: str,
        answer: str,
        proxy_trace: str = "",
        memory_evidence: str = "",
    ) -> Verdict:
        b = make_b_verifier()
        trace_text = proxy_trace or "No proxy tools were available/used."
        prompt = f"""Verify A's proposed answer against the user's task.

USER TASK:
{task}

A ANSWER:
{answer}

SHARED MEMORY EVIDENCE PRESENTED TO A:
{memory_evidence if memory_evidence else "No shared memory slice was presented to A on this turn."}

EXECUTION / PROVENANCE TRACE:
{trace_text}

${VERIFICATION_PROTOCOL}

Additional turn-specific requirements:
- The SHARED MEMORY EVIDENCE above is the exact memory snapshot A was allowed to use on this turn.
- If that evidence supports A's claim, treat the claim as memory-grounded; do not say memory was unavailable or invisible.
- Use the trace to determine whether memory/delegate/consult tools actually ran.
- Content marked provenance=lobe_b_worker or lobe_b_consult is B-originated and MUST NOT be treated as independent corroboration.
- Do not claim a required proxy tool was unused when the execution trace records that it ran.

Return ONLY JSON:
{{
  "deception_level": "GREEN|YELLOW|RED",
  "rationale": "brief reason",
  "handoff": {{
    "next_step": "",
    "missing": [],
    "unverified": [],
    "widen": [],
    "memory_query": ""
  }}
}}

GREEN = no deception detected, not verified truth."""
        raw = await self._safe_run_one(
            b,
            prompt,
            "Strict compact JSON verdict.",
            fallback_text="VERIFIER_CALL_FAILED_OR_EMPTY",
            role_key="B_VERIFY",
        )
        return parse_model(
            raw,
            Verdict,
            Verdict(
                deception_level="YELLOW",
                rationale="Verifier output could not be parsed or was empty.",
            ),
        )

    async def _persist_memory_query(self, verdict: Verdict) -> None:
        query = (verdict.handoff.memory_query or "").strip()
        if not query:
            return
        hits = await asyncio.to_thread(self.memory.search, query, 4)
        payload = (
            f"B deferred memory query: {query}\n"
            + ("Memory hits:\n" + "\n".join(f"- {h}" for h in hits) if hits else "Memory hits: none")
        )
        await asyncio.to_thread(self.memory.record, payload)

    async def run(self, task: str) -> RunResult:
        timings: dict[str, int | float | str] = {}
        memory_slice = self.memory.auto_slice(task)

        t0 = time.perf_counter()
        answer = await self._run_a(task, memory_slice=memory_slice)
        timings["a_ms"] = int((time.perf_counter() - t0) * 1000)

        t1 = time.perf_counter()
        verdict = await self._verify_with_b(task, answer, memory_evidence=memory_slice)
        timings["b_verify_ms"] = int((time.perf_counter() - t1) * 1000)
        timings["total_ms"] = int((time.perf_counter() - t0) * 1000)

        await self._persist_memory_query(verdict)
        asyncio.create_task(asyncio.to_thread(
            self.memory.record,
            f"Task: {task}\nAnswer: {answer}\nVerdict: {verdict.deception_level}",
        ))
        return RunResult(
            mode=self.name,
            answer=answer,
            verdict=verdict,
            timings_ms=timings,
            logical_model_calls=2,
        )


class NonSplitEngine(GatedEngine):
    name = "non-split"

    async def run(self, task: str) -> RunResult:
        timings: dict[str, int | float | str] = {}
        memory_slice = self.memory.auto_slice(task)
        proxy_trace = ProxyToolTrace()
        run_state = ProxyRunState()
        tools = make_proxy_tools(self.memory, trace=proxy_trace, run_state=run_state)

        t0 = time.perf_counter()
        answer = await self._run_a(
            task,
            tools=tools,
            include_broadening=True,
            memory_slice=memory_slice,
        )
        timings["a_ms"] = int((time.perf_counter() - t0) * 1000)

        t1 = time.perf_counter()
        verdict = await self._verify_with_b(
            task,
            answer,
            proxy_trace=proxy_trace.render(),
            memory_evidence=memory_slice,
        )
        timings["b_verify_ms"] = int((time.perf_counter() - t1) * 1000)
        timings["total_ms"] = int((time.perf_counter() - t0) * 1000)

        await self._persist_memory_query(verdict)
        asyncio.create_task(asyncio.to_thread(
            self.memory.record,
            f"Task: {task}\nAnswer: {answer}\nVerdict: {verdict.deception_level}",
        ))
        dynamic_b_calls = int(run_state.delegate_used) + int(run_state.consult_used)
        return RunResult(
            mode=self.name,
            answer=answer,
            verdict=verdict,
            timings_ms=timings,
            logical_model_calls=2 + dynamic_b_calls,
        )


class SplitEngine(GatedEngine):
    """Self-splitting Dual-Lobe with B as the reconvergence point.

    A owns routing and one work half. split_channel starts B's independent half
    concurrently. The runtime collects both completed halves. A never performs a
    merge pass. B finalizes the cycle by merging, repairing, verifying, and
    grading the split in one call.
    """

    name = "split"

    async def _run_self_split_a(
        self,
        *,
        task: str,
        memory_slice: str,
        split_experience: str,
        trace: ProxyToolTrace,
        run_state: SelfSplitRunState,
        canonical_state: str = "",
    ) -> str:
        tools = make_self_split_tools(
            self.memory,
            original_task=task,
            memory_slice=memory_slice,
            trace=trace,
            run_state=run_state,
        )
        a = make_a(tools=tools, self_split=True)
        prompt = f"""{OBSERVATION_DISCLAIMER}

ORIGINAL USER TASK:
{task}

CURRENT CANONICAL STATE FROM A PRIOR LOOP CYCLE:
{canonical_state if canonical_state else "(none; this is the first cycle)"}

SHARED MEMORY SNAPSHOT:
{memory_slice if memory_slice else "(none)"}

PAST MEASURED SPLIT EXPERIENCE:
{split_experience if split_experience else "(none yet)"}

You are the router because you are the worker. There is NO separate splitter model.

Before substantive execution, actively look for a two-way independent split.
If a valid time-saving split exists, you MUST call split_channel with:
- own_fragment: the half you will personally execute;
- peer_fragment: the equal independent half B will execute;
- reason: why the halves are independent and why parallelism should help;
- merge_mode: append when the halves can mostly be preserved, integrate when synthesis is needed.

After split_channel accepts, A and B are working concurrently.
Execute ONLY own_fragment yourself and return ONLY your completed half-result.
Do not wait for B. Do not merge. Do not attempt to produce the whole answer.
The runtime will collect both halves and B will merge, repair deficiencies, verify, and finalize.

If no valid split exists, do the whole task yourself and return the complete answer.
If a prior canonical state is present, treat it as the unified state from the previous cycle and continue from it rather than creating a disconnected branch.
Never split merely because a decomposition is imaginable. Optimize actual completion time."""
        return await self._safe_run_one(
            a,
            prompt,
            "If split: A's complete independent half only. If normal: one complete answer.",
            fallback_text="PRIMARY_SELF_SPLIT_WORKER_CALL_FAILED_OR_EMPTY",
            role_key="A",
        )

    @staticmethod
    def _timing_telemetry(
        state: SelfSplitRunState,
        *,
        primary_started: float,
        primary_finished: float,
    ) -> dict[str, int | float | str]:
        split_t = state.split_started_perf or primary_finished
        b_start = state.b_started_perf or split_t
        b_end = state.b_finished_perf or b_start

        route_decision_ms = max(0, int((split_t - primary_started) * 1000))
        a_half_ms = max(0, int((primary_finished - split_t) * 1000))
        b_half_ms = max(0, int((b_end - b_start) * 1000))
        join_wait_ms = max(0, int((b_end - primary_finished) * 1000))
        overlap_ms = max(0, int((min(primary_finished, b_end) - max(split_t, b_start)) * 1000))
        parallel_window_ms = max(0, int((max(primary_finished, b_end) - split_t) * 1000))

        balance_ratio = 1.0
        if max(a_half_ms, b_half_ms) > 0:
            balance_ratio = min(a_half_ms, b_half_ms) / max(a_half_ms, b_half_ms)

        overlap_ratio = 0.0
        if parallel_window_ms > 0:
            overlap_ratio = overlap_ms / parallel_window_ms

        return {
            "route_decision_ms": route_decision_ms,
            "a_half_ms": a_half_ms,
            "b_half_ms": b_half_ms,
            "parallel_window_ms": parallel_window_ms,
            "overlap_ms": overlap_ms,
            "overlap_ratio": round(overlap_ratio, 3),
            "balance_ratio": round(balance_ratio, 3),
            "join_wait_ms": join_wait_ms,
            "parallel_gain_proxy_ms": overlap_ms,
            "measured_time_effect": "positive" if overlap_ms > 250 else "neutral",
        }

    async def _review_normal(
        self,
        *,
        task: str,
        answer: str,
        telemetry: dict[str, int | float | str],
        trace: str,
        memory_slice: str,
        split_experience: str,
    ) -> TurnReview:
        b = make_b_verifier()
        prompt = f"""Verify A's single-lane final answer and grade the decision not to split.

USER TASK:
{task}

FINAL ANSWER:
{answer}

EXACT SHARED MEMORY SNAPSHOT AVAILABLE TO A:
{memory_slice if memory_slice else "(none)"}

PAST SPLIT EXPERIENCE AVAILABLE TO A:
{split_experience if split_experience else "(none yet)"}

RUNTIME TOOL TRACE:
{trace}

TIMING TELEMETRY:
{json.dumps(telemetry, ensure_ascii=False, indent=2)}

{VERIFICATION_PROTOCOL}

Additional requirements for this single-lane turn:
- Apply the verifier protocol to FINAL ANSWER exactly as emitted.
- Grade the decision not to split separately from answer truthfulness.
- Do not let a good split decision compensate for an unsupported answer claim, or vice versa.

Return ONLY JSON:
{{
  "answer_verdict": {{
    "deception_level": "GREEN|YELLOW|RED",
    "rationale": "brief answer verification",
    "handoff": {{
      "next_step": "",
      "missing": [],
      "unverified": [],
      "widen": [],
      "memory_query": ""
    }}
  }},
  "split_verdict": {{
    "used": false,
    "valid": true,
    "score": 0,
    "independence_score": 0.0,
    "balance_score": 0.0,
    "time_effect": "unknown",
    "unnecessary_split": false,
    "missed_valid_split": false,
    "better_single_model": false,
    "feedback": "short reusable lesson"
  }}
}}

Set missed_valid_split=true only when there was an obvious substantial independent two-way split A should have used.
GREEN means no deception detected, not verified truth."""
        raw = await self._safe_run_one(
            b,
            prompt,
            "Strict compact JSON with answer_verdict and split_verdict.",
            fallback_text="NORMAL_REVIEW_CALL_FAILED_OR_EMPTY",
            role_key="B_VERIFY",
        )
        fallback = TurnReview(
            answer_verdict=Verdict(
                deception_level="YELLOW",
                rationale="Final review output could not be parsed or was empty.",
            ),
            split_verdict=SplitQuality(
                used=False,
                valid=False,
                score=0,
                independence_score=0.0,
                balance_score=0.0,
                time_effect="unknown",
                feedback="Review failed; do not learn a positive routing lesson from this turn.",
            ),
        )
        return parse_model(raw, TurnReview, fallback)

    async def _finalize_split_with_b(
        self,
        *,
        task: str,
        a_half: str,
        b_half: str,
        plan: SplitPlan,
        telemetry: dict[str, int | float | str],
        trace: ProxyToolTrace,
        memory_slice: str,
        split_experience: str,
        canonical_state: str,
    ) -> FinalizedTurn:
        finalizer_trace = ProxyToolTrace()
        b_tools = make_parallel_execution_tools(self.memory, trace=finalizer_trace)
        b = make_b_finalizer(tools=b_tools)
        prompt = f"""You are the reconvergence point for a parallel Dual-Lobe cycle.

ORIGINAL USER TASK:
{task}

CURRENT CANONICAL STATE FROM PRIOR CYCLE:
{canonical_state if canonical_state else "(none; first cycle)"}

A'S COMPLETED HALF:
{a_half}

B'S COMPLETED HALF:
{b_half}

WORKER HEALTH:
- A worker failed sentinel present: {a_half.startswith("PRIMARY_SELF_SPLIT_WORKER_CALL_FAILED_OR_EMPTY")}
- B worker error: {bool(getattr(plan, "mode", "") == "split" and b_half.startswith("B_HALF_CALL_FAILED"))}

A'S SPLIT PLAN:
{plan.model_dump_json()}

EXACT SHARED MEMORY SNAPSHOT:
{memory_slice if memory_slice else "(none)"}

PAST SPLIT EXPERIENCE:
{split_experience if split_experience else "(none yet)"}

WORKER TOOL / PROVENANCE TRACE:
{trace.render()}

MEASURED TIMING TELEMETRY:
{json.dumps(telemetry, ensure_ascii=False, indent=2)}

{VERIFICATION_PROTOCOL}

Perform ALL of these in this same call:
1. COLLECT both halves. Preserve all useful work from each.
2. MERGE them into one coherent answer for the ORIGINAL USER TASK.
3. REPAIR deficiencies: remove duplication, resolve contradictions, restore missing prerequisites/conclusions, and fill obvious task-required gaps.
4. Do NOT invent unsupported facts while repairing. If evidence is insufficient, state the limitation in the final answer.
5. FREEZE the completed candidate, then apply the full CORE VERIFICATION PROTOCOL to that exact candidate.
6. If verification exposes a repairable deficiency, repair it and re-check the repaired candidate before emitting.
7. Keep verification evidence separate from worker authorship: neither A-half nor B-half corroborates itself.
8. GRADE the split for semantic validity, independence, balance, timing effect, unnecessary splitting, and reusable lessons.
9. Emit ONE canonical final answer. The answer_verdict MUST describe that exact emitted final_answer. In loop mode this exact answer becomes the single state for the next cycle.

Runtime timing is stronger evidence than intuition for speed.
parallel_gain_proxy_ms is measured overlap only; it is NOT a matched single-model counterfactual.

Return ONLY JSON:
{{
  "final_answer": "the complete repaired merged user-facing answer",
  "answer_verdict": {{
    "deception_level": "GREEN|YELLOW|RED",
    "rationale": "brief verification",
    "handoff": {{
      "next_step": "",
      "missing": [],
      "unverified": [],
      "widen": [],
      "memory_query": ""
    }}
  }},
  "split_verdict": {{
    "used": true,
    "valid": true,
    "score": 0,
    "independence_score": 0.0,
    "balance_score": 0.0,
    "time_effect": "positive|neutral|negative|unknown",
    "unnecessary_split": false,
    "missed_valid_split": false,
    "better_single_model": false,
    "feedback": "short reusable lesson"
  }}
}}

GREEN means no deception detected, not verified truth."""
        raw = await self._safe_run_one(
            b,
            prompt,
            "Strict JSON containing final_answer, answer_verdict, and split_verdict.",
            fallback_text="SPLIT_FINALIZER_CALL_FAILED_OR_EMPTY",
            role_key="B_VERIFY",
        )

        fallback_answer = (
            a_half.rstrip()
            + "\n\n"
            + b_half.lstrip()
        ).strip()
        fallback = FinalizedTurn(
            final_answer=fallback_answer or "SPLIT_FINALIZER_FAILED_AND_HALVES_WERE_EMPTY",
            answer_verdict=Verdict(
                deception_level="YELLOW",
                rationale="B finalizer output could not be parsed or was empty; halves were preserved deterministically.",
            ),
            split_verdict=SplitQuality(
                used=True,
                valid=False,
                score=0,
                independence_score=0.0,
                balance_score=0.0,
                time_effect="unknown",
                feedback="Finalization failed; do not learn a positive split lesson from this cycle.",
            ),
        )
        finalized = parse_model(raw, FinalizedTurn, fallback)

        # Runtime hardening: a failed worker lane makes the split itself invalid even
        # if B was able to repair the final answer. Do not let the model grade over
        # execution evidence.
        worker_failed = (
            a_half.startswith("PRIMARY_SELF_SPLIT_WORKER_CALL_FAILED_OR_EMPTY")
            or b_half.startswith("B_HALF_CALL_FAILED:")
            or b_half.startswith("B_HALF_CALL_FAILED_OR_EMPTY")
        )
        if worker_failed:
            finalized.split_verdict = finalized.split_verdict.model_copy(
                update={
                    "valid": False,
                    "score": min(finalized.split_verdict.score, 25),
                    "time_effect": "unknown",
                    "feedback": (
                        "At least one worker lane failed; do not learn a positive split lesson from this cycle. "
                        + finalized.split_verdict.feedback
                    )[:1200],
                }
            )

        if finalizer_trace.events:
            trace.add(
                "b_finalizer_tools",
                input_text="merge/repair/verify",
                output_text=finalizer_trace.render(),
                provenance="lobe_b_finalizer_tool_trace",
            )
        return finalized

    async def _persist_split_experience(
        self,
        *,
        task: str,
        plan: SplitPlan,
        telemetry: dict[str, int | float | str],
        grade: SplitQuality,
    ) -> None:
        lesson = {
            "task_excerpt": task[:1200],
            "decision": plan.model_dump(),
            "timing": telemetry,
            "grade": grade.model_dump(),
        }
        await asyncio.to_thread(
            self.memory.record_split_experience,
            json.dumps(lesson, ensure_ascii=False),
        )

    async def run_cycle(
        self,
        task: str,
        *,
        canonical_state: str = "",
        cycle_index: int = 1,
    ) -> RunResult:
        timings: dict[str, int | float | str] = {}
        total_start = time.perf_counter()
        memory_slice = self.memory.auto_slice(task)
        split_experience = self.memory.split_experience_slice(task)
        trace = ProxyToolTrace()
        state = SelfSplitRunState()

        primary_start = time.perf_counter()
        a_primary = await self._run_self_split_a(
            task=task,
            memory_slice=memory_slice,
            split_experience=split_experience,
            trace=trace,
            run_state=state,
            canonical_state=canonical_state,
        )
        primary_finished = time.perf_counter()
        timings["a_route_and_work_ms"] = int((primary_finished - primary_start) * 1000)

        if state.split_used:
            b_half = await state.await_peer()
            plan = SplitPlan(
                mode="split",
                fragments=[
                    SplitFragment(owner="A", task=state.own_fragment),
                    SplitFragment(owner="B", task=state.peer_fragment),
                ],
                merge=state.merge_mode,
                reason=state.reason,
            )
            telemetry = self._timing_telemetry(
                state,
                primary_started=primary_start,
                primary_finished=primary_finished,
            )
            timings.update(telemetry)

            finalize_start = time.perf_counter()
            finalized = await self._finalize_split_with_b(
                task=task,
                a_half=a_primary,
                b_half=b_half,
                plan=plan,
                telemetry=telemetry,
                trace=trace,
                memory_slice=memory_slice,
                split_experience=split_experience,
                canonical_state=canonical_state,
            )
            timings["b_finalize_verify_ms"] = int((time.perf_counter() - finalize_start) * 1000)
            answer = finalized.final_answer
            verdict = finalized.answer_verdict
            split_feedback = finalized.split_verdict
            logical_calls = 3
            route_source = "a_self_split"
        else:
            plan = SplitPlan(
                mode="normal",
                reason="A found no valid two-way split worth the coordination overhead.",
            )
            answer = a_primary
            telemetry = {
                "route_decision_ms": timings["a_route_and_work_ms"],
                "measured_time_effect": "unknown",
                "parallel_gain_proxy_ms": 0,
            }
            review_start = time.perf_counter()
            review = await self._review_normal(
                task=task,
                answer=answer,
                telemetry=telemetry,
                trace=trace.render(),
                memory_slice=memory_slice,
                split_experience=split_experience,
            )
            timings["b_review_ms"] = int((time.perf_counter() - review_start) * 1000)
            verdict = review.answer_verdict
            split_feedback = review.split_verdict
            logical_calls = 2
            route_source = "a_self_normal"

        await self._persist_memory_query(verdict)
        await self._persist_split_experience(
            task=task,
            plan=plan,
            telemetry=telemetry,
            grade=split_feedback,
        )

        timings["total_ms"] = int((time.perf_counter() - total_start) * 1000)

        asyncio.create_task(asyncio.to_thread(
            self.memory.record,
            f"Cycle: {cycle_index}\nTask: {task}\nCanonical state: {answer}\nVerdict: {verdict.deception_level}",
        ))

        return RunResult(
            mode=self.name,
            answer=answer,
            verdict=verdict,
            route=plan,
            timings_ms=timings,
            logical_model_calls=logical_calls,
            route_source=route_source,
            split_feedback=split_feedback,
            canonical_state=answer,
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
