from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from .agents import make_a, make_b_verifier
from .json_utils import parse_model
from .memory import JsonlMemoryStore
from .models import (
    SplitFragment,
    SplitPlan,
    SplitQuality,
    TurnReview,
    Verdict,
)
from .prompts import OBSERVATION_DISCLAIMER
from .runner import run_one
from .tools import (
    ProxyRunState,
    ProxyToolTrace,
    SelfSplitRunState,
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

Important:
- The SHARED MEMORY EVIDENCE above is the exact memory snapshot A was allowed to use on this turn.
- If that evidence supports A's claim, treat the claim as memory-grounded.
- Use the trace to determine whether memory/delegate/consult tools actually ran.
- B-originated worker/consult content is contributed work, not independent corroboration.

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
    """Self-splitting Dual-Lobe.

    There is no dedicated splitter model. A receives the task first and is itself
    the router. If it finds a valid split, split_channel launches B in a background
    lane and returns immediately so A can execute its own half concurrently.
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
- merge_mode: append when possible, integrate only when necessary.

After split_channel accepts, A and B are working concurrently.
Execute ONLY own_fragment yourself. Finish your own half first.
Then call collect_split_result and pass your completed half as own_result.
That tool returns B's concurrently computed half into THIS SAME active A task.
Absorb both halves and then produce the complete final user-facing answer.
There is NO separate merge run.

If no valid split exists, do the whole task yourself and return the complete final answer.
Never split merely because a decomposition is imaginable. Optimize actual completion time."""
        return await self._safe_run_one(
            a,
            prompt,
            "One complete final user-facing answer. If split_channel was used, collect_split_result must be used before finalizing.",
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
        collect_start = state.collect_started_perf or primary_finished
        collect_end = state.collect_finished_perf or collect_start

        route_decision_ms = max(0, int((split_t - primary_started) * 1000))
        a_half_ms = max(0, int((collect_start - split_t) * 1000))
        b_half_ms = max(0, int((b_end - b_start) * 1000))
        collect_wait_ms = max(0, int((collect_end - collect_start) * 1000))
        a_finalize_ms = max(0, int((primary_finished - collect_end) * 1000))
        overlap_ms = max(0, int((min(collect_start, b_end) - max(split_t, b_start)) * 1000))
        parallel_window_ms = max(0, int((max(collect_start, b_end) - split_t) * 1000))

        balance_ratio = 1.0
        if max(a_half_ms, b_half_ms) > 0:
            balance_ratio = min(a_half_ms, b_half_ms) / max(a_half_ms, b_half_ms)

        overlap_ratio = 0.0
        if parallel_window_ms > 0:
            overlap_ratio = overlap_ms / parallel_window_ms

        # This is measured overlap, not a true matched single-model counterfactual.
        parallel_gain_proxy_ms = overlap_ms
        measured = "positive" if overlap_ms > 250 else "neutral"

        return {
            "route_decision_ms": route_decision_ms,
            "a_half_ms": a_half_ms,
            "b_half_ms": b_half_ms,
            "parallel_window_ms": parallel_window_ms,
            "overlap_ms": overlap_ms,
            "overlap_ratio": round(overlap_ratio, 3),
            "balance_ratio": round(balance_ratio, 3),
            "collect_wait_ms": collect_wait_ms,
            "a_finalize_ms": a_finalize_ms,
            "merge_ms": 0,
            "parallel_gain_proxy_ms": parallel_gain_proxy_ms,
            "measured_time_effect": measured,
            "same_turn_collect": bool(state.collected),
        }

    async def _review_self_split(
        self,
        *,
        task: str,
        answer: str,
        plan: SplitPlan,
        telemetry: dict[str, int | float | str],
        trace: str,
        memory_slice: str,
        split_experience: str,
    ) -> TurnReview:
        b = make_b_verifier()
        used = plan.mode == "split"
        prompt = f"""Verify the final answer AND grade A's split decision.

USER TASK:
{task}

FINAL ANSWER:
{answer}

EXACT SHARED MEMORY SNAPSHOT AVAILABLE TO A:
{memory_slice if memory_slice else "(none)"}

PAST SPLIT EXPERIENCE AVAILABLE TO A:
{split_experience if split_experience else "(none yet)"}

A'S SPLIT DECISION:
{plan.model_dump_json()}

RUNTIME TOOL / SPLIT TRACE:
{trace}

MEASURED TIMING TELEMETRY:
{json.dumps(telemetry, ensure_ascii=False, indent=2)}

Rules for the split grade:
- used MUST equal {str(used).lower()}.
- If a split was used but same_turn_collect is false, the split execution contract failed: valid=false and score must be low.
- If split was used, judge whether both halves were substantial, independent, and sensibly balanced.
- If no split was used, set missed_valid_split=true only if an obvious substantial independent two-way split existed.
- Runtime telemetry outranks intuition for speed.
- same_turn_collect must be true on a successful split.
- overlap_ms is the measured period during which A and B were both working.
- collect_wait_ms is how long A had to wait after finishing its own half.
- a_finalize_ms is A's same-turn time after B's result was injected to produce the whole final answer.
- parallel_gain_proxy_ms equals overlap_ms. It is a concurrency proxy, NOT a true matched single-model counterfactual.
- unnecessary_split=true when the task should have stayed single-lane.
- better_single_model=true when evidence indicates the split likely prolonged completion.
- score rates the routing/decomposition decision itself from 0 to 100.
- feedback must be a short reusable lesson A can apply to similar tasks.

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
    "used": {str(used).lower()},
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
}}"""
        raw = await self._safe_run_one(
            b,
            prompt,
            "Strict compact JSON with answer_verdict and split_verdict.",
            fallback_text="SELF_SPLIT_REVIEW_CALL_FAILED_OR_EMPTY",
            role_key="B_VERIFY",
        )

        fallback = TurnReview(
            answer_verdict=Verdict(
                deception_level="YELLOW",
                rationale="Final review output could not be parsed or was empty.",
            ),
            split_verdict=SplitQuality(
                used=used,
                valid=False,
                score=0,
                independence_score=0.0,
                balance_score=0.0,
                time_effect="unknown",
                feedback="Review failed; do not learn a positive routing lesson from this turn.",
            ),
        )
        return parse_model(raw, TurnReview, fallback)

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

    async def run(self, task: str) -> RunResult:
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
        )
        primary_finished = time.perf_counter()
        timings["a_route_and_work_ms"] = int((primary_finished - primary_start) * 1000)

        logical_calls = 1

        if state.split_used:
            # The successful path collects B back into the SAME active A CrewAI task.
            # Awaiting here is cleanup only if A violated the contract; it never creates
            # another A inference and never injects B after A has already finished.
            if state.future is not None and not state.future.done():
                await state.await_peer()

            plan = SplitPlan(
                mode="split",
                fragments=[
                    SplitFragment(owner="A", task=state.own_fragment),
                    SplitFragment(owner="B", task=state.peer_fragment),
                ],
                merge=state.merge_mode,
                reason=state.reason,
            )

            answer = a_primary
            telemetry = self._timing_telemetry(
                state,
                primary_started=primary_start,
                primary_finished=primary_finished,
            )
            timings.update(telemetry)
            route_source = "a_self_split"
            logical_calls += 1  # nested concurrent B worker

        else:
            plan = SplitPlan(
                mode="normal",
                reason="A found no valid two-way split worth the coordination/merge overhead.",
            )
            answer = a_primary
            telemetry = {
                "route_decision_ms": timings["a_route_and_work_ms"],
                "measured_time_effect": "unknown",
                "parallel_gain_proxy_ms": 0,
            }
            route_source = "a_self_normal"

        review_start = time.perf_counter()
        review = await self._review_self_split(
            task=task,
            answer=answer,
            plan=plan,
            telemetry=telemetry,
            trace=trace.render(),
            memory_slice=memory_slice,
            split_experience=split_experience,
        )
        timings["b_review_ms"] = int((time.perf_counter() - review_start) * 1000)
        logical_calls += 1

        await self._persist_memory_query(review.answer_verdict)
        await self._persist_split_experience(
            task=task,
            plan=plan,
            telemetry=telemetry,
            grade=review.split_verdict,
        )

        timings["total_ms"] = int((time.perf_counter() - total_start) * 1000)

        asyncio.create_task(asyncio.to_thread(
            self.memory.record,
            f"Task: {task}\nAnswer: {answer}\nVerdict: {review.answer_verdict.deception_level}",
        ))

        return RunResult(
            mode=self.name,
            answer=answer,
            verdict=review.answer_verdict,
            route=plan,
            timings_ms=timings,
            logical_model_calls=logical_calls,
            route_source=route_source,
            split_feedback=review.split_verdict,
        )
