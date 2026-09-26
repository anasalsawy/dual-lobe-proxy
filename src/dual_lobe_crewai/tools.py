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
class ProxyRunState:
    delegate_used: bool = False
    consult_used: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def claim(self, kind: str) -> bool:
        with self.lock:
            attr = f"{kind}_used"
            if getattr(self, attr):
                return False
            setattr(self, attr, True)
            return True


@dataclass
class ProxyToolTrace:
    events: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, tool: str, *, input_text: str, output_text: str, provenance: str) -> None:
        with self.lock:
            self.events.append({
                "tool": tool,
                "input": input_text[:2000],
                "output": output_text[:4000],
                "provenance": provenance,
            })

    def render(self) -> str:
        with self.lock:
            events = list(self.events)
        if not events:
            return "No proxy tools were used."
        return "\n".join(
            f"{i}. tool={e['tool']} provenance={e['provenance']}\n   input={e['input']}\n   output={e['output']}"
            for i, e in enumerate(events, 1)
        )


def _run_coro_sync(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        box, err = {}, {}
        def worker():
            try:
                box["value"] = asyncio.run(coro)
            except Exception as exc:
                err["exc"] = exc
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join()
        if "exc" in err:
            raise err["exc"]
        return box.get("value", "")
    return asyncio.run(coro)


class MemorySearchInput(BaseModel):
    query: str = Field(..., description="Short deterministic memory search query.")


class ProxyMemorySearchTool(BaseTool):
    name: str = "proxy_memory_search"
    description: str = "Search the shared deterministic memory store and return matching ordinary task memory."
    args_schema: Type[BaseModel] = MemorySearchInput
    store: JsonlMemoryStore
    trace: ProxyToolTrace

    def _run(self, query: str) -> str:
        hits = self.store.search(query, limit=4, include_split_experience=False)
        result = ("\n\n".join(hits) if hits else "No memory hits.")[:4000]
        self.trace.add(self.name, input_text=query, output_text=result, provenance="shared_durable_memory")
        return result


class DelegateInput(BaseModel):
    task: str = Field(..., description="Bounded work to offload to Lobe B.")


class ProxyDelegateTool(BaseTool):
    name: str = "proxy_delegate"
    description: str = "Legacy Non-Split control tool: offload one bounded piece of work to B, at most once per run."
    args_schema: Type[BaseModel] = DelegateInput
    trace: ProxyToolTrace
    run_state: ProxyRunState

    def _run(self, task: str) -> str:
        if not self.run_state.claim("delegate"):
            result = "proxy_delegate unavailable: per-run cap already used."
            self.trace.add(self.name, input_text=task, output_text=result, provenance="limit_enforcement")
            return result

        async def go():
            b = make_b_worker()
            return await run_one(
                b,
                f"Execute this delegated subtask independently.\n\nSUBTASK:\n{task}\n\nReturn concise substance only.",
                "A concise worker result.",
                role_key="B_WORKER",
            )

        result = str(_run_coro_sync(go()))[:12000]
        self.trace.add(self.name, input_text=task, output_text=result, provenance="lobe_b_worker")
        return result


class ConsultInput(BaseModel):
    question: str = Field(..., description="A focused question for Lobe B.")


class ProxyConsultTool(BaseTool):
    name: str = "proxy_consult"
    description: str = "Legacy Non-Split control tool: ask B one focused advisory question, at most once per run."
    args_schema: Type[BaseModel] = ConsultInput
    trace: ProxyToolTrace
    run_state: ProxyRunState

    def _run(self, question: str) -> str:
        if not self.run_state.claim("consult"):
            result = "proxy_consult unavailable: per-run cap already used."
            self.trace.add(self.name, input_text=question, output_text=result, provenance="limit_enforcement")
            return result

        async def go():
            b = make_b_worker()
            return await run_one(
                b,
                f"Answer this focused advisory question for Lobe A.\n\nQUESTION:\n{question}",
                "A short advisory answer.",
                role_key="B_WORKER",
            )

        result = str(_run_coro_sync(go()))[:10000]
        self.trace.add(self.name, input_text=question, output_text=result, provenance="lobe_b_consult")
        return result


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
                b = make_b_worker()
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
                return state.b_result
            except Exception as exc:
                state.b_error = f"{type(exc).__name__}: {exc}"
                state.b_result = f"B_HALF_CALL_FAILED: {state.b_error}"
                return state.b_result
            finally:
                state.b_finished_perf = time.perf_counter()

        state.future = state.executor.submit(peer_worker)

        result = (
            "SPLIT_ACCEPTED. B is now executing peer_fragment concurrently. "
            "Immediately execute ONLY own_fragment yourself; do not solve B's half and do not wait for B."
        )
        self.trace.add(
            self.name,
            input_text=f"own={own_fragment}\npeer={peer_fragment}\nreason={reason}\nmerge={merge_mode}",
            output_text=result,
            provenance="parallel_split_launch",
        )
        return result


def make_proxy_tools(store: JsonlMemoryStore, trace: ProxyToolTrace | None = None, run_state: ProxyRunState | None = None):
    trace = trace or ProxyToolTrace()
    run_state = run_state or ProxyRunState()
    return [
        ProxyMemorySearchTool(store=store, trace=trace),
        ProxyDelegateTool(trace=trace, run_state=run_state),
        ProxyConsultTool(trace=trace, run_state=run_state),
    ]


def make_self_split_tools(
    store: JsonlMemoryStore,
    *,
    original_task: str,
    memory_slice: str,
    trace: ProxyToolTrace,
    run_state: SelfSplitRunState,
):
    return [
        ProxyMemorySearchTool(store=store, trace=trace),
        SplitChannelTool(
            original_task=original_task,
            memory_slice=memory_slice,
            trace=trace,
            run_state=run_state,
        ),
    ]
