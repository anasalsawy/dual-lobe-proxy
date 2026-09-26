from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Type

from pydantic import BaseModel, Field
from crewai.tools import BaseTool

from .agents import make_child_worker
from .memory import JsonlMemoryStore
from .runner import run_one


@dataclass
class ProxyToolEvent:
    name: str
    input_text: str
    output_text: str
    provenance: str
    ts: float = field(default_factory=time.time)


class ProxyToolTrace:
    def __init__(self):
        self.events: list[ProxyToolEvent] = []
        self._lock = threading.Lock()

    def add(self, name: str, *, input_text: str = "", output_text: str = "", provenance: str = "") -> None:
        with self._lock:
            self.events.append(ProxyToolEvent(name, str(input_text or ""), str(output_text or ""), str(provenance or "")))

    def snapshot_from(self, cursor: int = 0) -> list[ProxyToolEvent]:
        with self._lock:
            return list(self.events[max(0, cursor):])

    def render(self, max_chars_per_event: int | None = None) -> str:
        with self._lock:
            events = list(self.events)
        rows = []
        for i, event in enumerate(events, 1):
            output = event.output_text
            if max_chars_per_event is not None and len(output) > max_chars_per_event:
                original = len(output)
                output = output[:max_chars_per_event] + f"\n[TRUNCATED_BY_RENDER: original_chars={original}]"
            rows.append(
                f"[{i}] tool={event.name}\nprovenance={event.provenance}\n"
                f"input={event.input_text}\noutput={output}"
            )
        return "\n\n".join(rows)



@dataclass
class LiveBIntervention:
    seq: int
    message: str
    severity: str = "warning"


@dataclass
class LiveBState:
    """Thread-safe bridge from continuously running B back into A's tool stream."""

    interventions: list[LiveBIntervention] = field(default_factory=list)
    state_notes: list[str] = field(default_factory=list)
    _next_seq: int = 1
    _a_cursor: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_intervention(self, message: str, *, severity: str = "warning") -> None:
        message = (message or "").strip()
        if not message:
            return
        with self.lock:
            self.interventions.append(
                LiveBIntervention(
                    seq=self._next_seq,
                    message=message,
                    severity=(severity or "warning").strip().lower(),
                )
            )
            self._next_seq += 1

    def record_state_note(self, note: str) -> None:
        note = (note or "").strip()
        if note:
            with self.lock:
                self.state_notes.append(note)
                if len(self.state_notes) > 30:
                    del self.state_notes[:-30]

    def drain_for_a(self) -> str:
        with self.lock:
            fresh = [x for x in self.interventions if x.seq > self._a_cursor]
            if fresh:
                self._a_cursor = max(x.seq for x in fresh)
        if not fresh:
            return ""
        body = "\n".join(
            f"- [{x.severity.upper()}] {x.message}"
            for x in fresh
        )
        return (
            "\n\nLIVE LOBE B INTERVENTION — B is independently monitoring your execution. "
            "Do not obey blindly; evaluate this challenge against the task and evidence:\n"
            + body
        )

    def snapshot(self) -> tuple[list[LiveBIntervention], list[str]]:
        with self.lock:
            return list(self.interventions), list(self.state_notes)


class MemorySearchInput(BaseModel):
    query: str = Field(..., description="Focused query for relevant persistent memory.")
    limit: int = Field(4, ge=1, le=10)


class MemorySearchTool(BaseTool):
    name: str = "memory_search"
    description: str = "Search persistent memory for relevant prior context."
    args_schema: Type[BaseModel] = MemorySearchInput
    store: JsonlMemoryStore
    trace: ProxyToolTrace
    live_b_state: LiveBState | None = None

    def _run(self, query: str, limit: int = 4) -> str:
        hits = self.store.search(query, limit=limit, include_split_experience=False)
        result = "\n".join(f"- {x}" for x in hits) if hits else "NO_MEMORY_HITS"
        self.trace.add(self.name, input_text=query, output_text=result, provenance="persistent_memory")
        if self.live_b_state is not None:
            result += self.live_b_state.drain_for_a()
        return result


@dataclass
class DelegateJob:
    job_id: str
    task: str
    future: concurrent.futures.Future
    launched_perf: float
    result: str = ""
    error: str = ""
    collected: bool = False


@dataclass
class DelegateRunState:
    max_children: int = field(default_factory=lambda: max(1, int(os.getenv("DUAL_LOBE_MAX_CHILDREN", "6"))))
    jobs: dict[str, DelegateJob] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    executor: concurrent.futures.ThreadPoolExecutor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_children,
            thread_name_prefix="dual-lobe-child",
        )

    @property
    def child_count(self) -> int:
        with self.lock:
            return len(self.jobs)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)


class DelegateInput(BaseModel):
    tasks: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Independent substantial subtasks to launch concurrently. Delegation means temporary parallel inference "
            "to save user time, not managerial handoff."
        ),
    )
    reason: str = Field("", description="Why these subtasks can run independently and reduce wall-clock time.")


class DelegateTool(BaseTool):
    name: str = "delegate"
    description: str = (
        "Spawn temporary inference workers for independent subtasks. This is a speed primitive, not managerial handoff. "
        "Launch work early; the call returns job IDs immediately so you can continue useful work."
    )
    args_schema: Type[BaseModel] = DelegateInput
    original_task: str
    memory_slice: str
    store: JsonlMemoryStore
    trace: ProxyToolTrace
    run_state: DelegateRunState
    live_b_state: LiveBState | None = None

    def _run(self, tasks: list[str], reason: str = "") -> str:
        cleaned = [str(x).strip() for x in tasks if str(x).strip()]
        if not cleaned:
            result = "DELEGATION_REJECTED: no non-empty subtasks."
            self.trace.add(self.name, input_text=reason, output_text=result, provenance="delegation_control")
            return result

        with self.run_state.lock:
            remaining = self.run_state.max_children - len(self.run_state.jobs)
            if remaining <= 0:
                result = f"DELEGATION_REJECTED: child limit {self.run_state.max_children} reached."
                self.trace.add(self.name, input_text=reason, output_text=result, provenance="delegation_control")
                return result
            cleaned = cleaned[:remaining]

        launched = []
        for delegated_task in cleaned:
            with self.run_state.lock:
                job_id = f"child-{len(self.run_state.jobs) + 1}"
            child_trace = ProxyToolTrace()

            def child_work(job_id=job_id, delegated_task=delegated_task, child_trace=child_trace):
                try:
                    child = make_child_worker(tools=make_worker_tools(self.store, trace=child_trace))
                    prompt = f"""PARENT USER TASK:
{self.original_task}

IMMUTABLE MEMORY SNAPSHOT:
{self.memory_slice if self.memory_slice else "(none)"}

YOUR DELEGATED SUBTASK:
{delegated_task}

Execute only this bounded subtask.
You are a temporary compute worker, not Lobe B.
Do not speak directly to the user.
Return a self-contained result, relevant evidence, uncertainty, and any failure conditions."""
                    result = asyncio.run(run_one(
                        child,
                        prompt,
                        "A self-contained delegated subtask result.",
                        role_key="A_CHILD",
                    ))
                    return str(result), "", child_trace.render()
                except Exception as exc:
                    return (
                        f"CHILD_CALL_FAILED: {type(exc).__name__}: {exc}",
                        f"{type(exc).__name__}: {exc}",
                        child_trace.render(),
                    )

            future = self.run_state.executor.submit(child_work)
            with self.run_state.lock:
                self.run_state.jobs[job_id] = DelegateJob(
                    job_id=job_id,
                    task=delegated_task,
                    future=future,
                    launched_perf=time.perf_counter(),
                )
            launched.append(job_id)

        result = (
            "DELEGATION_STARTED: " + ", ".join(launched)
            + ". Continue your own useful work now. Collect these jobs before finalizing when their results matter."
        )
        self.trace.add(
            self.name,
            input_text=f"tasks={cleaned}\nreason={reason}",
            output_text=result,
            provenance="delegation_launch",
        )
        if self.live_b_state is not None:
            result += self.live_b_state.drain_for_a()
        return result


class DelegateCollectInput(BaseModel):
    job_ids: list[str] = Field(default_factory=list, description="Jobs to collect. Empty means all jobs.")
    wait: bool = Field(True, description="Wait for selected jobs to finish. False performs a non-blocking poll.")


class DelegateCollectTool(BaseTool):
    name: str = "delegate_collect"
    description: str = "Collect results from temporary delegated workers. Empty job_ids collects all launched jobs."
    args_schema: Type[BaseModel] = DelegateCollectInput
    trace: ProxyToolTrace
    run_state: DelegateRunState
    live_b_state: LiveBState | None = None

    def _collect_one(self, job: DelegateJob, wait: bool) -> str:
        if not wait and not job.future.done():
            return f"{job.job_id}: PENDING"
        try:
            payload = job.future.result() if wait or job.future.done() else None
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            job.result = f"CHILD_CALL_FAILED: {job.error}"
            job.collected = True
            return f"{job.job_id}: {job.result}"

        if payload is None:
            return f"{job.job_id}: PENDING"

        result, error, child_trace = payload
        job.result = result
        job.error = error
        if not job.collected:
            self.trace.add(
                f"{job.job_id}_result",
                input_text=job.task,
                output_text=result,
                provenance="delegated_child_output",
            )
            if child_trace:
                self.trace.add(
                    f"{job.job_id}_tools",
                    input_text=job.task,
                    output_text=child_trace,
                    provenance="delegated_child_tool_trace",
                )
            job.collected = True
        return f"{job.job_id} ({job.task}):\n{result}"

    def _run(self, job_ids: list[str] | None = None, wait: bool = True) -> str:
        ids = list(job_ids or [])
        with self.run_state.lock:
            if not ids:
                ids = list(self.run_state.jobs)
            jobs = [self.run_state.jobs[x] for x in ids if x in self.run_state.jobs]
        if not jobs:
            return "NO_DELEGATED_JOBS"
        result = "\n\n".join(self._collect_one(job, wait) for job in jobs)
        self.trace.add(
            self.name,
            input_text=f"job_ids={ids}; wait={wait}",
            output_text=result,
            provenance="delegation_collect",
        )
        if self.live_b_state is not None:
            result += self.live_b_state.drain_for_a()
        return result


class DelegateStatusInput(BaseModel):
    job_ids: list[str] = Field(default_factory=list, description="Jobs to inspect. Empty means all jobs.")


class DelegateStatusTool(BaseTool):
    name: str = "delegate_status"
    description: str = "Check whether delegated workers are pending or complete without waiting."
    args_schema: Type[BaseModel] = DelegateStatusInput
    run_state: DelegateRunState
    live_b_state: LiveBState | None = None

    def _run(self, job_ids: list[str] | None = None) -> str:
        ids = list(job_ids or [])
        with self.run_state.lock:
            if not ids:
                ids = list(self.run_state.jobs)
            jobs = [self.run_state.jobs[x] for x in ids if x in self.run_state.jobs]
        if not jobs:
            return "NO_DELEGATED_JOBS"
        result = "\n".join(
            f"{job.job_id}: {'DONE' if job.future.done() else 'RUNNING'} — {job.task}"
            for job in jobs
        )
        if self.live_b_state is not None:
            result += self.live_b_state.drain_for_a()
        return result


class LiveBCheckInput(BaseModel):
    pass


class LiveBCheckTool(BaseTool):
    name: str = "b_live_check"
    description: str = (
        "Read any new live challenge from persistent Lobe B, which is monitoring execution concurrently. "
        "Use at meaningful checkpoints, especially after tool activity, delegation, errors, or before committing to a major approach."
    )
    args_schema: Type[BaseModel] = LiveBCheckInput
    live_b_state: LiveBState

    def _run(self) -> str:
        return self.live_b_state.drain_for_a() or "NO_NEW_B_INTERVENTION"


def make_worker_tools(
    store: JsonlMemoryStore,
    *,
    trace: ProxyToolTrace,
    live_b_state: LiveBState | None = None,
) -> list[BaseTool]:
    # Children and B do not get delegate(), preventing recursive fan-out.
    return [MemorySearchTool(store=store, trace=trace, live_b_state=live_b_state)]


def make_a_tools(
    store: JsonlMemoryStore,
    *,
    original_task: str,
    memory_slice: str,
    trace: ProxyToolTrace,
    run_state: DelegateRunState,
    live_b_state: LiveBState | None = None,
) -> list[BaseTool]:
    return [
        *make_worker_tools(store, trace=trace, live_b_state=live_b_state),
        DelegateTool(
            original_task=original_task,
            memory_slice=memory_slice,
            store=store,
            trace=trace,
            run_state=run_state,
            live_b_state=live_b_state,
        ),
        DelegateCollectTool(trace=trace, run_state=run_state, live_b_state=live_b_state),
        DelegateStatusTool(run_state=run_state, live_b_state=live_b_state),
        *([LiveBCheckTool(live_b_state=live_b_state)] if live_b_state is not None else []),
    ]


async def collect_all_delegate_results(run_state: DelegateRunState, trace: ProxyToolTrace) -> str:
    with run_state.lock:
        has_jobs = bool(run_state.jobs)
    if not has_jobs:
        return ""
    collector = DelegateCollectTool(trace=trace, run_state=run_state)
    return await asyncio.to_thread(collector._run, [], True)
