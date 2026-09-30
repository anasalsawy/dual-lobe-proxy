"""A's tools for the split engine: memory, delegation to temporary children, and the live-B channel.

Same behaviour as the CrewAI tools: delegate returns job ids at once and the
children run concurrently while A keeps working; consult_other_lobe is
non-blocking; every tool result carries any fresh live-B intervention.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field

from ...core.settings import get_settings
from ...provider import calltrace
from ..common import ProxyToolTrace, Tool, obj, run_agent
from .memory import JsonlMemoryStore
from .prompts import CHILD_PERSONA


@dataclass
class LiveBIntervention:
    seq: int
    message: str
    severity: str = "warning"


@dataclass
class LiveBState:
    """Bridge from the continuously running B back into A's tool stream."""

    interventions: list[LiveBIntervention] = field(default_factory=list)
    state_notes: list[str] = field(default_factory=list)
    consultation_requests: list[dict[str, str | int]] = field(default_factory=list)
    _next_seq: int = 1
    _next_consult_seq: int = 1
    _a_cursor: int = 0
    _b_consult_cursor: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_intervention(self, message: str, *, severity: str = "warning") -> None:
        message = (message or "").strip()
        if not message:
            return
        with self.lock:
            self.interventions.append(LiveBIntervention(
                seq=self._next_seq, message=message, severity=(severity or "warning").strip().lower()))
            self._next_seq += 1

    def record_state_note(self, note: str) -> None:
        note = (note or "").strip()
        if note:
            with self.lock:
                self.state_notes.append(note)
                if len(self.state_notes) > 30:
                    del self.state_notes[:-30]

    def post_consultation(self, *, blocker: str, what_i_tried: str = "", what_i_need: str = "",
                          current_hypothesis: str = "") -> int:
        with self.lock:
            seq = self._next_consult_seq
            self._next_consult_seq += 1
            self.consultation_requests.append({
                "seq": seq,
                "blocker": (blocker or "").strip(),
                "what_i_tried": (what_i_tried or "").strip(),
                "what_i_need": (what_i_need or "").strip(),
                "current_hypothesis": (current_hypothesis or "").strip(),
            })
            return seq

    def drain_consultations_for_b(self) -> list[dict[str, str | int]]:
        with self.lock:
            fresh = [dict(x) for x in self.consultation_requests if int(x.get("seq", 0)) > self._b_consult_cursor]
            if fresh:
                self._b_consult_cursor = max(int(x["seq"]) for x in fresh)
        return fresh

    def drain_for_a(self) -> str:
        with self.lock:
            fresh = [x for x in self.interventions if x.seq > self._a_cursor]
            if fresh:
                self._a_cursor = max(x.seq for x in fresh)
        if not fresh:
            return ""
        body = "\n".join(f"- [{x.severity.upper()}] {x.message}" for x in fresh)
        return (
            "\n\nLIVE LOBE B INTERVENTION — B is independently monitoring your execution. "
            "Do not obey blindly; evaluate this challenge against the task and evidence:\n" + body
        )


@dataclass
class DelegateJob:
    job_id: str
    task: str
    future: asyncio.Task
    launched_perf: float
    result: str = ""
    error: str = ""
    collected: bool = False


@dataclass
class DelegateRunState:
    max_children: int = field(default_factory=lambda: max(1, int(os.getenv("DUAL_LOBE_MAX_CHILDREN", "6"))))
    jobs: dict[str, DelegateJob] = field(default_factory=dict)

    @property
    def child_count(self) -> int:
        return len(self.jobs)

    def close(self) -> None:
        for job in self.jobs.values():
            if not job.future.done():
                job.future.cancel()


def _child_tokens() -> int:
    return int(os.getenv("DUAL_LOBE_CHILD_MAX_TOKENS", "6000"))


def memory_search_tool(store: JsonlMemoryStore, trace: ProxyToolTrace,
                       live_b_state: LiveBState | None = None) -> Tool:
    def run(query: str, limit: int = 4) -> str:
        hits = store.search(query, limit=max(1, min(10, int(limit))), include_split_experience=False)
        result = "\n".join(f"- {x}" for x in hits) if hits else "NO_MEMORY_HITS"
        trace.add("memory_search", input_text=query, output_text=result, provenance="persistent_memory")
        if live_b_state is not None:
            result += live_b_state.drain_for_a()
        return result

    return Tool("memory_search", "Search persistent memory for relevant prior context.",
                obj({"query": {"type": "string", "description": "Focused query for relevant persistent memory."},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]),
                run)


def make_worker_tools(store: JsonlMemoryStore, *, trace: ProxyToolTrace,
                      live_b_state: LiveBState | None = None) -> list[Tool]:
    # Children and B do not get delegate(), preventing recursive fan-out.
    return [memory_search_tool(store, trace, live_b_state)]


def make_a_tools(store: JsonlMemoryStore, *, original_task: str, memory_slice: str, trace: ProxyToolTrace,
                 run_state: DelegateRunState, live_b_state: LiveBState | None = None) -> list[Tool]:
    s = get_settings()

    async def child_work(job_id: str, delegated_task: str) -> tuple[str, str, str]:
        child_trace = ProxyToolTrace()
        prompt = f"""PARENT USER TASK:
{original_task}

IMMUTABLE MEMORY SNAPSHOT:
{memory_slice if memory_slice else "(none)"}

YOUR DELEGATED SUBTASK:
{delegated_task}

Execute only this bounded subtask.
You are a temporary compute worker, not Lobe B.
Do not speak directly to the user.
Return a self-contained result, relevant evidence, uncertainty, and any failure conditions."""
        try:
            with calltrace.stage(f"A-{job_id}"):
                result = await run_agent(
                alias="lobe-a",
                system=("Role: Temporary Delegated Inference Worker\n"
                        "Goal: Execute the assigned independent subtask quickly and return a self-contained result to Lobe A.\n\n"
                        + CHILD_PERSONA),
                prompt=prompt, max_tokens=_child_tokens(), timeout=s.a_timeout,
                tools=make_worker_tools(store, trace=child_trace))
            return result, "", child_trace.render()
        except Exception as exc:  # noqa: BLE001
            return (f"CHILD_CALL_FAILED: {type(exc).__name__}: {exc}", f"{type(exc).__name__}: {exc}",
                    child_trace.render())

    def with_b(result: str) -> str:
        return result + (live_b_state.drain_for_a() if live_b_state is not None else "")

    def delegate(tasks: list[str], reason: str = "") -> str:
        cleaned = [str(x).strip() for x in (tasks or []) if str(x).strip()]
        if not cleaned:
            result = "DELEGATION_REJECTED: no non-empty subtasks."
            trace.add("delegate", input_text=reason, output_text=result, provenance="delegation_control")
            return result
        remaining = run_state.max_children - len(run_state.jobs)
        if remaining <= 0:
            result = f"DELEGATION_REJECTED: child limit {run_state.max_children} reached."
            trace.add("delegate", input_text=reason, output_text=result, provenance="delegation_control")
            return result
        cleaned = cleaned[:remaining]
        launched = []
        for delegated_task in cleaned:
            job_id = f"child-{len(run_state.jobs) + 1}"
            run_state.jobs[job_id] = DelegateJob(job_id=job_id, task=delegated_task,
                                                 future=asyncio.ensure_future(child_work(job_id, delegated_task)),
                                                 launched_perf=time.perf_counter())
            launched.append(job_id)
        result = ("DELEGATION_STARTED: " + ", ".join(launched)
                  + ". Continue your own useful work now. Collect these jobs before finalizing when their results matter.")
        trace.add("delegate", input_text=f"tasks={cleaned}\nreason={reason}", output_text=result,
                  provenance="delegation_launch")
        return with_b(result)

    async def collect_one(job: DelegateJob, wait: bool) -> str:
        if not wait and not job.future.done():
            return f"{job.job_id}: PENDING"
        try:
            result, error, child_trace = await job.future
        except Exception as exc:  # noqa: BLE001
            job.error = f"{type(exc).__name__}: {exc}"
            job.result = f"CHILD_CALL_FAILED: {job.error}"
            job.collected = True
            return f"{job.job_id}: {job.result}"
        job.result, job.error = result, error
        if not job.collected:
            trace.add(f"{job.job_id}_result", input_text=job.task, output_text=result,
                      provenance="delegated_child_output")
            if child_trace:
                trace.add(f"{job.job_id}_tools", input_text=job.task, output_text=child_trace,
                          provenance="delegated_child_tool_trace")
            job.collected = True
        return f"{job.job_id} ({job.task}):\n{result}"

    async def delegate_collect(job_ids: list[str] | None = None, wait: bool = True) -> str:
        ids = list(job_ids or []) or list(run_state.jobs)
        jobs = [run_state.jobs[x] for x in ids if x in run_state.jobs]
        if not jobs:
            return "NO_DELEGATED_JOBS"
        result = "\n\n".join([await collect_one(job, wait) for job in jobs])
        trace.add("delegate_collect", input_text=f"job_ids={ids}; wait={wait}", output_text=result,
                  provenance="delegation_collect")
        return with_b(result)

    def delegate_status(job_ids: list[str] | None = None) -> str:
        ids = list(job_ids or []) or list(run_state.jobs)
        jobs = [run_state.jobs[x] for x in ids if x in run_state.jobs]
        if not jobs:
            return "NO_DELEGATED_JOBS"
        return with_b("\n".join(f"{j.job_id}: {'DONE' if j.future.done() else 'RUNNING'} — {j.task}" for j in jobs))

    tools = [
        *make_worker_tools(store, trace=trace, live_b_state=live_b_state),
        Tool("delegate",
             "Spawn temporary inference workers for independent subtasks. This is a speed primitive, not managerial "
             "handoff. Launch work early; the call returns job IDs immediately so you can continue useful work.",
             obj({"tasks": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                            "description": "Independent substantial subtasks to launch concurrently. Delegation means "
                                           "temporary parallel inference to save user time, not managerial handoff."},
                  "reason": {"type": "string",
                             "description": "Why these subtasks can run independently and reduce wall-clock time."}},
                 ["tasks"]),
             delegate),
        Tool("delegate_collect",
             "Collect results from temporary delegated workers. Empty job_ids collects all launched jobs.",
             obj({"job_ids": {"type": "array", "items": {"type": "string"},
                              "description": "Jobs to collect. Empty means all jobs."},
                  "wait": {"type": "boolean",
                           "description": "Wait for selected jobs to finish. False performs a non-blocking poll."}}),
             delegate_collect),
        Tool("delegate_status", "Check whether delegated workers are pending or complete without waiting.",
             obj({"job_ids": {"type": "array", "items": {"type": "string"},
                              "description": "Jobs to inspect. Empty means all jobs."}}),
             delegate_status),
    ]
    if live_b_state is not None:
        def consult_other_lobe(blocker: str, what_i_tried: str = "", what_i_need: str = "",
                               current_hypothesis: str = "") -> str:
            seq = live_b_state.post_consultation(blocker=blocker, what_i_tried=what_i_tried,
                                                 what_i_need=what_i_need, current_hypothesis=current_hypothesis)
            packet = (f"consultation_id={seq}\nblocker={blocker}\nwhat_i_tried={what_i_tried}\n"
                      f"what_i_need={what_i_need}\ncurrent_hypothesis={current_hypothesis}")
            result = (f"CONSULTATION_POSTED_NONBLOCKING: {seq}. "
                      "Do not wait. Continue useful work; B will answer through the live intervention channel.")
            trace.add("consult_other_lobe", input_text=packet, output_text=result, provenance="a_to_b_consultation")
            return result

        def b_live_check() -> str:
            return live_b_state.drain_for_a() or "NO_NEW_B_INTERVENTION"

        tools += [
            Tool("consult_other_lobe",
                 "Post a NON-BLOCKING consultation to the already-running Lobe B. Use when genuinely stuck, repeating "
                 "a failed approach, uncertain about framing, or before an irreversible choice. This tool returns "
                 "immediately; continue useful work and read B's response later via b_live_check or another tool result.",
                 obj({"blocker": {"type": "string",
                                  "description": "The exact blocker, uncertainty, or decision A wants B to attack."},
                      "what_i_tried": {"type": "string", "description": "Relevant attempts already made; keep concise."},
                      "what_i_need": {"type": "string", "description": "The kind of second-lobe input needed: "
                                                                        "alternate frame, missing fact, failure mode, etc."},
                      "current_hypothesis": {"type": "string", "description": "A's current working hypothesis, if any."}},
                     ["blocker"]),
                 consult_other_lobe),
            Tool("b_live_check",
                 "Read any new live challenge from persistent Lobe B, which is monitoring execution concurrently. "
                 "Use at meaningful checkpoints, especially after tool activity, delegation, errors, or before "
                 "committing to a major approach.",
                 obj({}), b_live_check),
        ]
    return tools


async def collect_all_delegate_results(run_state: DelegateRunState, trace: ProxyToolTrace) -> str:
    if not run_state.jobs:
        return ""
    rows = []
    for job in run_state.jobs.values():
        try:
            result, error, child_trace = await job.future
        except Exception as exc:  # noqa: BLE001
            result, child_trace = f"CHILD_CALL_FAILED: {type(exc).__name__}: {exc}", ""
        if not job.collected:
            trace.add(f"{job.job_id}_result", input_text=job.task, output_text=result,
                      provenance="delegated_child_output")
            if child_trace:
                trace.add(f"{job.job_id}_tools", input_text=job.task, output_text=child_trace,
                          provenance="delegated_child_tool_trace")
            job.collected = True
        rows.append(f"{job.job_id} ({job.task}):\n{result}")
    result = "\n\n".join(rows)
    trace.add("delegate_collect", input_text="job_ids=[]; wait=True", output_text=result,
              provenance="delegation_collect")
    return result
