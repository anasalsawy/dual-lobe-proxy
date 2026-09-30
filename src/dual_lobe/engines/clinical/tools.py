"""B's execution tools for the clinical engine: consult_planner, execute_parallel, collect_execution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ...core.settings import get_settings
from ...provider import calltrace
from ..common import ProxyToolTrace, Tool, obj, run_agent
from .models import Plan, PlanContract

CLINICAL_B_ALIAS = "lobe-b-clinical"
B_EXECUTOR_SYSTEM = (
    "Role: Lobe B — Local Executor and Plan Challenger\n"
    "Goal: Execute A's current plan faithfully using local data and tools, challenge the plan whenever reality "
    "makes it invalid, and never silently rewrite A's plan.\n\n"
    "You are the execution lobe. You own tools and local data. You are adversarial toward weak planning but must "
    "cooperate with A because A alone owns the plan and user intent."
)


def clinical_b_tokens() -> int:
    return int(os.getenv("DUAL_LOBE_CLINICAL_B_MAX_TOKENS", "6000"))


@dataclass
class ExecutionJob:
    job_id: str
    step_id: str
    future: asyncio.Task
    launched_perf: float
    collected: bool = False


@dataclass
class ExecutionDelegateState:
    max_children: int = 6
    jobs: dict[str, ExecutionJob] = field(default_factory=dict)

    @property
    def child_count(self) -> int:
        return len(self.jobs)

    def close(self) -> None:
        for job in self.jobs.values():
            if not job.future.done():
                job.future.cancel()


def make_b_tools(*, contract: PlanContract, planner_callback: Callable[[str, str], Awaitable[Plan]],
                 raw_query: str, raw_patient_context: str, execution_tools: list[Tool], trace: ProxyToolTrace,
                 run_state: ExecutionDelegateState) -> list[Tool]:
    async def consult_planner(concern: str, evidence: str = "") -> str:
        old_revision = contract.revision
        old_hash = contract.fingerprint()
        new_plan = await planner_callback(concern, evidence)
        new_hash = hashlib.sha256(json.dumps(new_plan.model_dump(), ensure_ascii=False,
                                             sort_keys=True).encode("utf-8")).hexdigest()
        if new_hash != old_hash:
            revision = contract.revise(new_plan, reason=concern)
            outcome = f"PLAN_REVISED revision={revision}\n{contract.current_json()}"
        else:
            outcome = f"PLAN_UPHELD revision={old_revision}\n{contract.current_json()}"
        trace.add("consult_planner", input_text=f"concern={concern}\nevidence={evidence}", output_text=outcome,
                  provenance="b_to_a_live_channel")
        return outcome

    async def step_work(step, plan_json: str, revision: int) -> str:
        prompt = f"""You are a temporary local execution instance of Lobe B.

USER TASK:
{raw_query}

LOCAL CLINICAL DATA / CONTEXT:
{raw_patient_context}

PLAN REVISION: {revision}
FULL PLAN:
{plan_json}

YOUR ASSIGNED STEP:
{step.id}: {step.action}

Execute only this step. Do not rewrite the plan and do not speak to the user.
Return the actual result and any tool evidence or failure."""
        with calltrace.stage(f"B-parallel-{step.id}"):
            return await run_agent(alias=CLINICAL_B_ALIAS, system=B_EXECUTOR_SYSTEM, prompt=prompt,
                               max_tokens=clinical_b_tokens(), timeout=get_settings().a_timeout,
                               tools=list(execution_tools))

    def execute_parallel(step_ids: list[str]) -> str:
        requested = []
        for step_id in step_ids:
            step = contract.step(step_id)
            if not step.parallelizable:
                return f"PARALLEL_EXECUTION_REJECTED: {step_id} is not marked parallelizable."
            if step.depends_on:
                return (f"PARALLEL_EXECUTION_REJECTED: {step_id} has dependencies {step.depends_on}; "
                        "run it after those dependencies are satisfied.")
            requested.append(step)
        launched: list[str] = []
        for step in requested:
            if len(run_state.jobs) >= run_state.max_children:
                break
            job_id = f"b-child-{len(run_state.jobs) + 1}"
            run_state.jobs[job_id] = ExecutionJob(
                job_id=job_id, step_id=step.id,
                future=asyncio.ensure_future(step_work(step, contract.current_json(), contract.revision)),
                launched_perf=time.perf_counter())
            launched.append(job_id)
        result = ("PARALLEL_EXECUTION_STARTED: " + ", ".join(launched) if launched
                  else "PARALLEL_EXECUTION_REJECTED: no child capacity.")
        trace.add("execute_parallel", input_text="step_ids=" + ",".join(step_ids), output_text=result,
                  provenance="b_parallel_execution")
        return result

    async def collect_execution(job_ids: list[str] | None = None, wait: bool = True) -> str:
        return await collect(run_state, trace, job_ids, wait)

    return [
        *execution_tools,
        Tool("consult_planner",
             "Challenge the current plan during execution. A reviews the concern and returns the complete current "
             "plan. If A revises it, deterministic code replaces the plan contract.",
             obj({"concern": {"type": "string"},
                  "evidence": {"type": "string", "description": "What B observed that makes the plan questionable."}},
                 ["concern"]),
             consult_planner),
        Tool("execute_parallel",
             "Launch independent current-plan steps concurrently on temporary local B execution instances. Only "
             "steps marked parallelizable in A's current plan may be launched.",
             obj({"step_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, ["step_ids"]),
             execute_parallel),
        Tool("collect_execution", "Collect results from B's parallel execution instances.",
             obj({"job_ids": {"type": "array", "items": {"type": "string"}}, "wait": {"type": "boolean"}}),
             collect_execution),
    ]


async def collect(run_state: ExecutionDelegateState, trace: ProxyToolTrace,
                  job_ids: list[str] | None = None, wait: bool = True) -> str:
    ids = list(job_ids or []) or list(run_state.jobs)
    jobs = [run_state.jobs[x] for x in ids if x in run_state.jobs]
    if not jobs:
        return "NO_PARALLEL_EXECUTION_JOBS"
    rows = []
    for job in jobs:
        if not wait and not job.future.done():
            rows.append(f"{job.job_id} ({job.step_id}): PENDING")
            continue
        try:
            result = str(await job.future)
        except Exception as exc:  # noqa: BLE001
            result = f"FAILED: {type(exc).__name__}: {exc}"
        rows.append(f"{job.job_id} ({job.step_id}):\n{result}")
        if not job.collected:
            trace.add(f"{job.job_id}_result", input_text=job.step_id, output_text=result,
                      provenance="b_parallel_execution_result")
            job.collected = True
    return "\n\n".join(rows)


async def collect_all_execution_results(run_state: ExecutionDelegateState, trace: ProxyToolTrace) -> str:
    if not run_state.jobs:
        return ""
    return await collect(run_state, trace, [], True)
