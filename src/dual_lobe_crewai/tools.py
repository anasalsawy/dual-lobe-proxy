from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Type

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
            self.events.append({"tool": tool, "input": input_text[:1200], "output": output_text[:2400], "provenance": provenance})

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
            try: box["value"] = asyncio.run(coro)
            except Exception as exc: err["exc"] = exc
        thread = threading.Thread(target=worker, daemon=True); thread.start(); thread.join()
        if "exc" in err: raise err["exc"]
        return box.get("value", "")
    return asyncio.run(coro)


class MemorySearchInput(BaseModel):
    query: str = Field(..., description="Short deterministic memory search query.")


class ProxyMemorySearchTool(BaseTool):
    name: str = "proxy_memory_search"
    description: str = "Search the shared deterministic memory store and return matching notes."
    args_schema: Type[BaseModel] = MemorySearchInput
    store: JsonlMemoryStore
    trace: ProxyToolTrace
    def _run(self, query: str) -> str:
        hits = self.store.search(query, limit=4)
        result = ("\n\n".join(hits) if hits else "No memory hits.")[:2600]
        self.trace.add(self.name, input_text=query, output_text=result, provenance="shared_durable_memory")
        return result


class DelegateInput(BaseModel):
    task: str = Field(..., description="Bounded work to offload to Lobe B.")


class ProxyDelegateTool(BaseTool):
    name: str = "proxy_delegate"
    description: str = "Offload one bounded piece of real work to Lobe B. Use at most once per run."
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
            return await run_one(b, f"Execute this delegated subtask independently.\n\nSUBTASK:\n{task}\n\nReturn concise substance only. Do not mention routing or grading.", "A concise worker result that Lobe A can silently absorb.", role_key="B_WORKER")
        result = str(_run_coro_sync(go()))[:12000]
        self.trace.add(self.name, input_text=task, output_text=result, provenance="lobe_b_worker")
        return result


class ConsultInput(BaseModel):
    question: str = Field(..., description="A focused question for Lobe B.")


class ProxyConsultTool(BaseTool):
    name: str = "proxy_consult"
    description: str = "Ask Lobe B one focused advisory question. Use at most once per run."
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
            return await run_one(b, f"Answer this focused advisory question for Lobe A.\n\nQUESTION:\n{question}\n\nBe concise. Do not take over the whole task.", "A short advisory answer.", role_key="B_WORKER")
        result = str(_run_coro_sync(go()))[:10000]
        self.trace.add(self.name, input_text=question, output_text=result, provenance="lobe_b_consult")
        return result


def make_proxy_tools(store: JsonlMemoryStore, trace: ProxyToolTrace | None = None, run_state: ProxyRunState | None = None):
    trace = trace or ProxyToolTrace(); run_state = run_state or ProxyRunState()
    return [
        ProxyMemorySearchTool(store=store, trace=trace),
        ProxyDelegateTool(trace=trace, run_state=run_state),
        ProxyConsultTool(trace=trace, run_state=run_state),
    ]
