from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from dataclasses import dataclass, field
from typing import Literal, Type

from pydantic import BaseModel, Field
from crewai.tools import BaseTool

from .agents import make_b_worker
from .memory import JsonlMemoryStore
from .runner import run_one


@dataclass
class SelfSplitRunState:
    split_used: bool = False
    own_fragment: str = ""
    peer_fragment: str = ""
    reason: str = ""
    merge_mode: str = "integrate"
    peer_first: bool = False
    split_started_perf: float | None = None
    b_started_perf: float | None = None
    b_finished_perf: float | None = None
    b_result: str = ""
    b_error: str = ""
    future: concurrent.futures.Future | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    executor: concurrent.futures.ThreadPoolExecutor = field(
        default_factory=lambda: concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dual-lobe-b"),
        repr=False,
    )

    def claim_split(
        self,
        *,
        own_fragment: str,
        peer_fragment: str,
        reason: str,
        merge_mode: str,
        peer_first: bool,
    ) -> bool:
        with self.lock:
            if self.split_used:
                return False
            self.split_used = True
            self.own_fragment = own_fragment.strip()
            self.peer_fragment = peer_fragment.strip()
            self.reason = reason.strip()
            self.merge_mode = merge_mode
            self.peer_first = peer_first
            self.split_started_perf = time.perf_counter()
            return True

    async def await_peer(self) -> str:
        if not self.future:
            return ""
        try:
            return await asyncio.to_thread(self.future.result)
        finally:
            self.executor.shutdown(wait=False, cancel_futures=False)


class SplitChannelInput(BaseModel):
    own_fragment: str = Field(..., description="The substantial independent half Lobe A will execute itself.")
    peer_fragment: str = Field(..., description="The substantial independent half Lobe B will execute concurrently.")
    reason: str = Field(..., description="Why these halves are independent and why parallel execution should save time.")
    merge_mode: Literal["append", "integrate"] = Field(
        "integrate",
        description="Use append when halves can be joined directly; integrate only when a synthesis pass is genuinely required.",
    )
    peer_first: bool = Field(False, description="For append mode only, place B's finished half before A's half.")


class SplitChannelTool(BaseTool):
    name: str = "split_channel"
    description: str = (
        "Launch the second independent execution lane. Call exactly once when a valid two-way split exists. "
        "It returns immediately so B works concurrently while A executes own_fragment."
    )
    args_schema: Type[BaseModel] = SplitChannelInput
    original_task: str
    memory_slice: str
    store: JsonlMemoryStore
    trace: ProxyToolTrace
    run_state: SelfSplitRunState

    def _run(
        self,
        own_fragment: str,
        peer_fragment: str,
        reason: str,
        merge_mode: str = "integrate",
        peer_first: bool = False,
    ) -> str:
        if not own_fragment.strip() or not peer_fragment.strip():
            result = "SPLIT_REJECTED: both halves must be non-empty and substantial."
            self.trace.add(self.name, input_text=reason, output_text=result, provenance="split_validation")
            return result

        if not self.run_state.claim_split(
            own_fragment=own_fragment,
            peer_fragment=peer_fragment,
            reason=reason,
            merge_mode=merge_mode,
            peer_first=peer_first,
        ):
            result = "SPLIT_REJECTED: split_channel may be used only once per run."
            self.trace.add(self.name, input_text=reason, output_text=result, provenance="split_validation")
            return result

        state = self.run_state
        original_task = self.original_task
        memory_slice = self.memory_slice

        def peer_worker() -> str:
            state.b_started_perf = time.perf_counter()
            try:
                b_trace = ProxyToolTrace()
                # B gets the same execution/tool plane as A's worker lane.
                # split_channel itself is orchestration control and is intentionally
                # not exposed to B, preventing recursive fan-out.
                b_tools = make_parallel_execution_tools(self.store, trace=b_trace)
                b = make_b_worker(tools=b_tools)
                prompt = f"""ORIGINAL USER TASK:
{original_task}

IMMUTABLE SHARED MEMORY SNAPSHOT:
{memory_slice if memory_slice else "(none)"}

YOUR INDEPENDENT HALF:
{peer_fragment}

Execute only this half. Do not wait for A and do not assume A's intermediate output.
Return a self-contained half-result suitable for later append or merge."""
                result = asyncio.run(run_one(b, prompt, "A complete independent half-result.", role_key="B_WORKER"))
                state.b_result = str(result)
                self.trace.add(
                    "b_parallel_tools",
                    input_text=peer_fragment,
                    output_text=b_trace.render(),
                    provenance="lobe_b_tool_trace",
                )
                return state.b_result
            except Exception as exc:
                state.b_error = f"{type(exc).__name__}: {exc}"
                state.b_result = f"B_HALF_CALL_FAILED: {state.b_error}"
                return state.b_result
            finally:
                state.b_finished_perf = time.perf_counter()

        state.future = state.executor.submit(peer_worker)

        result = (
            "SPLIT_ACCEPTED. A and B are now executing independent halves concurrently. "
            "Complete ONLY own_fragment yourself and return that half-result. "
            "The runtime will collect both completed halves and B will merge, repair, verify, and finalize."
        )
        self.trace.add(
            self.name,
            input_text=f"own={own_fragment}\npeer={peer_fragment}\nreason={reason}\nmerge={merge_mode}",
            output_text=result,
            provenance="parallel_split_launch",
        )
        return result


def make_self_split_tools(
    store: JsonlMemoryStore,
    *,
    original_task: str,
    memory_slice: str,
    trace: ProxyToolTrace,
    run_state: SelfSplitRunState,
):
    return [
        *make_parallel_execution_tools(store, trace=trace),
        SplitChannelTool(
            original_task=original_task,
            memory_slice=memory_slice,
            store=store,
            trace=trace,
            run_state=run_state,
        ),
    ]
