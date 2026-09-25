from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

from .agents import make_a, make_b_verifier, make_b_worker, make_splitter
from .json_utils import parse_model
from .memory import JsonlMemoryStore
from .models import Verdict, SplitPlan, SplitFragment
from .prompts import OBSERVATION_DISCLAIMER
from .runner import run_one
from .tools import make_proxy_tools, ProxyToolTrace, ProxyRunState


@dataclass
class RunResult:
    mode: str
    answer: str
    verdict: Verdict
    route: SplitPlan | None = None
    timings_ms: dict[str, int] = field(default_factory=dict)
    logical_model_calls: int = 0
    route_source: str | None = None

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
            a, prompt, "A complete user-facing answer.",
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
        timings = {}
        memory_slice = self.memory.auto_slice(task)

        t0 = time.perf_counter()
        answer = await self._run_a(task, memory_slice=memory_slice)
        timings["a_ms"] = int((time.perf_counter() - t0) * 1000)

        t1 = time.perf_counter()
        verdict = await self._verify_with_b(
            task,
            answer,
            memory_evidence=memory_slice,
        )
        timings["b_verify_ms"] = int((time.perf_counter() - t1) * 1000)
        timings["total_ms"] = int((time.perf_counter() - t0) * 1000)

        await self._persist_memory_query(verdict)
        asyncio.create_task(asyncio.to_thread(self.memory.record, f"Task: {task}\nAnswer: {answer}\nVerdict: {verdict.deception_level}"))
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
        timings = {}
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
        asyncio.create_task(asyncio.to_thread(self.memory.record, f"Task: {task}\nAnswer: {answer}\nVerdict: {verdict.deception_level}"))
        dynamic_b_calls = int(run_state.delegate_used) + int(run_state.consult_used)
        return RunResult(
            mode=self.name,
            answer=answer,
            verdict=verdict,
            timings_ms=timings,
            logical_model_calls=2 + dynamic_b_calls,
        )


class SplitEngine(NonSplitEngine):
    name = "split"

    async def _route(self, task: str, memory_slice: str | None = None) -> SplitPlan:
        if memory_slice is None:
            memory_slice = self.memory.auto_slice(task)
        splitter = make_splitter()
        raw = await self._safe_run_one(
            splitter,
            f"""Route this task. DO NOT solve it.

TASK:
{task}

MEMORY SLICE:
{memory_slice}

Return ONLY JSON:
{{
  "mode": "normal|split",
  "fragments": [
    {{"owner":"A","task":"..."}},
    {{"owner":"B","task":"..."}}
  ],
  "merge": "append|integrate",
  "start": "short coordination note",
  "reason": "short routing rationale"
}}

Rules:
- At most two fragments.
- Fragments must be independently executable.
- Decide semantically whether parallel execution is genuinely useful. Do not estimate numeric latency, milliseconds, token counts, or cost.
- Choose NORMAL when the task is fundamentally one coherent reasoning chain, tightly sequential, too small to benefit from parallel work, or would require heavy cross-dependence between halves.
- Actively search for a valid two-way decomposition before choosing NORMAL. A task that appears indivisible may still be split by component, perspective, hypothesis, evidence source, search space, solution strategy, verification method, or other orthogonal dimension. Choose SPLIT whenever two substantial independently executable fragments can produce useful progress in parallel and later be merged without either fragment requiring the other's intermediate output. Choose NORMAL only when no useful independent two-way decomposition can be found.""",
            "Strict routing JSON only.",
            fallback_text="SPLITTER_ROUTE_CALL_FAILED_OR_EMPTY",
            role_key="SPLITTER",
        )
        return parse_model(raw, SplitPlan, SplitPlan(mode="normal", reason="ROUTING_PARSE_FAILURE: splitter output was empty, truncated, or invalid JSON"))

    def _accept_split(self, plan: SplitPlan) -> bool:
        if plan.mode != "split":
            return False
        if len(plan.fragments) != 2:
            return False
        owners = {f.owner for f in plan.fragments}
        return owners == {"A", "B"}

    async def _run_half(
        self,
        owner: str,
        fragment: str,
        task: str,
        run_state: ProxyRunState,
        memory_slice: str,
    ) -> tuple[str, str]:
        shared = f"{OBSERVATION_DISCLAIMER}\n\nSHARED MEMORY SLICE:\n{memory_slice}" if memory_slice else OBSERVATION_DISCLAIMER

        trace = ProxyToolTrace()
        if owner == "A":
            agent = make_a(tools=make_proxy_tools(self.memory, trace=trace, run_state=run_state))
        else:
            agent = make_b_worker()

        result = await self._safe_run_one(
            agent,
            f"""{shared}

ORIGINAL USER TASK:
{task}

YOUR INDEPENDENT FRAGMENT:
{fragment}

Do only this fragment. Do not wait for the other worker and do not assume its output.""",
            "A self-contained fragment result.",
            fallback_text=f"{owner}_HALF_CALL_FAILED_OR_EMPTY",
            role_key="A" if owner == "A" else "B_WORKER",
        )
        if owner == "B":
            return result, "B-HALF provenance=lobe_b_worker_fragment"
        return result, trace.render()

    async def _merge_with_a(self, task: str, plan: SplitPlan, a_half: str, b_half: str, provenance: str, run_state: ProxyRunState) -> str:
        a = make_a(tools=make_proxy_tools(self.memory, run_state=run_state), merge=True)
        merge_instruction = (
            "Append the two self-contained halves with only minimal de-duplication."
            if plan.merge == "append"
            else "Integrate the two halves into one coherent final answer, resolving overlap and contradictions."
        )
        return await self._safe_run_one(
            a,
            f"""You are continuing the same task after parallel worker fan-out.

ORIGINAL USER TASK:
{task}

A-HALF:
{a_half}

B-HALF:
{b_half}

PROVENANCE / TOOL TRACE:
{provenance}

MERGE MODE: {plan.merge}
{merge_instruction}

Return only the final user-facing answer. Do not mention the split or internal workers.""",
            "One final merged user-facing answer.",
            fallback_text="MERGE_CALL_FAILED_OR_EMPTY",
            role_key="A_MERGE",
        )

    async def _verify_with_splitter(
        self,
        task: str,
        answer: str,
        provenance: str,
        memory_evidence: str,
    ) -> Verdict:
        splitter = make_splitter()
        raw = await self._safe_run_one(
            splitter,
            f"""You routed this task but did not perform task work. Now independently verify the merged output.

USER TASK:
{task}

MERGED ANSWER:
{answer}

SHARED MEMORY EVIDENCE AVAILABLE TO THE SPLIT WORKERS:
{memory_evidence if memory_evidence else "No shared memory slice was available on this turn."}

PROVENANCE / TOOL TRACE:
{provenance}

Treat B-half and any lobe_b_worker/lobe_b_consult material as contributed work, not independent corroboration.

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

GREEN = no deception detected, not verified truth.""",
            "Strict compact JSON verdict.",
            fallback_text="SPLITTER_VERIFY_CALL_FAILED_OR_EMPTY",
            role_key="SPLITTER",
        )
        return parse_model(
            raw,
            Verdict,
            Verdict(
                deception_level="YELLOW",
                rationale="Splitter-verifier output could not be parsed or was empty.",
            ),
        )

    async def run(self, task: str) -> RunResult:
        timings = {}
        total_start = time.perf_counter()
        memory_slice = self.memory.auto_slice(task)

        t_route = time.perf_counter()
        plan = await self._route(task, memory_slice=memory_slice)
        timings["splitter_route_ms"] = int((time.perf_counter() - t_route) * 1000)

        if not self._accept_split(plan):
            base = await super().run(task)
            base.mode = self.name
            base.route = plan
            base.route_source = (
                "fallback"
                if plan.reason.startswith("ROUTING_PARSE_FAILURE:")
                else "semantic"
            )
            base.logical_model_calls += 1
            base.timings_ms["splitter_route_ms"] = timings["splitter_route_ms"]
            base.timings_ms["split_path"] = 0
            return base

        frag_a = next(f for f in plan.fragments if f.owner == "A")
        frag_b = next(f for f in plan.fragments if f.owner == "B")
        run_state = ProxyRunState()

        t_halves = time.perf_counter()
        a_result, b_result = await asyncio.gather(
            self._run_half("A", frag_a.task, task, run_state, memory_slice),
            self._run_half("B", frag_b.task, task, run_state, memory_slice),
        )
        a_half, a_trace = a_result
        b_half, b_trace = b_result
        provenance = f"A-HALF TOOL TRACE:\n{a_trace}\n\nB-HALF TRACE:\n{b_trace}"
        timings["parallel_halves_ms"] = int((time.perf_counter() - t_halves) * 1000)

        t_merge = time.perf_counter()
        answer = await self._merge_with_a(task, plan, a_half, b_half, provenance, run_state)
        timings["merge_ms"] = int((time.perf_counter() - t_merge) * 1000)

        t_verify = time.perf_counter()
        verdict = await self._verify_with_splitter(
            task,
            answer,
            provenance,
            memory_slice,
        )
        timings["splitter_verify_ms"] = int((time.perf_counter() - t_verify) * 1000)

        timings["split_path"] = 1
        timings["total_ms"] = int((time.perf_counter() - total_start) * 1000)

        asyncio.create_task(asyncio.to_thread(self.memory.record, f"Task: {task}\nAnswer: {answer}\nVerdict: {verdict.deception_level}"))
        dynamic_b_calls = int(run_state.delegate_used) + int(run_state.consult_used)
        return RunResult(
            mode=self.name,
            answer=answer,
            verdict=verdict,
            route=plan,
            timings_ms=timings,
            logical_model_calls=5 + dynamic_b_calls,
            route_source="semantic",
        )
