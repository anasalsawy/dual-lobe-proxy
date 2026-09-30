"""Model 2 engine (ported from the CrewAI ClinicalDualLobeEngine, no CrewAI).

A owns reasoning, planning, plan revision, final review, and the user-facing
answer; it has no tools and sees only privacy-sanitized input. B is local, owns
tool/data execution, can challenge A at any time, and may parallelize
independent plan steps. The current A-authored plan is held by deterministic
code as the execution contract; B cannot silently rewrite it.

Tools: B gets the client's tools (the OpenAI ``tools`` in the request). When B
calls one, the run pauses: the tool calls go back to the client, which runs
them and sends the results in the next request; the run then resumes where B
stopped, with the same plan contract, privacy vault and B conversation. B also
has a server-side ``memory_search`` tool.

Long-term memory: a JSONL store on the volume. A reads a relevant slice when
planning and reviewing; B reads it and can search it. Only sanitized text is
written (A's answer, the plan, the sanitized execution summary), never the raw
patient record.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from ...core.settings import get_settings
from ..common import AgentPause, ProxyToolTrace, Tool, run_agent, stream_text
from ..split.memory import JsonlMemoryStore
from ..split.tools import memory_search_tool
from .models import ExecutionReport, Plan, PlanContract
from .privacy import EphemeralTokenVault, PrivacyGuard, PrivacyReceipt, ProviderPrivacyPolicy
from .prompts import build_direct_prompt, build_execution_prompt, build_final_review_prompt, build_plan_prompt, build_revision_prompt
from .tools import (
    B_EXECUTOR_SYSTEM,
    CLINICAL_B_ALIAS,
    ExecutionDelegateState,
    clinical_b_tokens,
    collect_all_execution_results,
    make_b_tools,
)

LOG = logging.getLogger("dual_lobe.engines.clinical")

PLANNER_AGENT_SYSTEM = (
    "Role: Lobe A — Planner and User-Facing Intelligence\n"
    "Goal: Understand the user's goal, create or revise the complete plan, and judge whether execution actually "
    "fulfilled it.\n\n"
    "You are the reasoning lobe. You do not own execution tools. You think, plan, revise when challenged by the "
    "execution lobe, and communicate the final result to the user."
)

CLIENT_TOOLS_NOTE = (
    "\nCLIENT TOOLS: the tools listed with this request run on the user's own system and return real data. "
    "When a plan step needs data or an action one of them provides, call it instead of guessing or saying you "
    "cannot. Never invent a tool result."
)

PENDING_TTL_SECONDS = 1800
_BACKGROUND: set[asyncio.Future] = set()


def memory_store() -> JsonlMemoryStore:
    path = os.getenv("DUAL_LOBE_CLINICAL_MEMORY_PATH") or (
        "/data/clinical_memory.jsonl" if Path("/data").is_dir() else ".clinical_memory.jsonl")
    return JsonlMemoryStore(path)


def _memory_block(slice_text: str) -> str:
    if not slice_text:
        return ""
    return ("\n\nLONG-TERM MEMORY (sanitized notes from earlier runs; background context only, "
            "not evidence about the current patient):\n" + slice_text)


@dataclass
class ClinicalRunResult:
    sanitized_query: str
    sanitized_patient_context: str
    plan: Plan
    plan_revision: int
    plan_sha256: str
    execution_report: str
    delegated_results: str
    answer: str
    trace_event_count: int = 0
    trace_sha256: str = ""
    timings_ms: dict[str, int | float] = field(default_factory=dict)
    logical_model_calls: int = 0
    privacy_receipt: PrivacyReceipt | None = None
    # Set when B paused for client tools: return these to the client.
    tool_calls: list[dict[str, Any]] | None = None
    memory_entries_used: int = 0


@dataclass
class _RunState:
    """Everything a paused run needs to resume after the client runs its tools."""

    query: str
    patient_context: str
    sanitized_query: str
    sanitized_context: str
    receipt: PrivacyReceipt
    vault: EphemeralTokenVault
    trace: ProxyToolTrace
    delegate_state: ExecutionDelegateState
    contract: PlanContract
    client_tools: list[dict[str, Any]]
    memory_slice: str
    memory_entries: int
    timings: dict[str, int | float]
    started: float
    b_messages: list[dict[str, Any]] | None = None
    pending_ids: list[str] = field(default_factory=list)
    b_rounds: int = 0
    expires: float = 0.0


_PENDING: dict[str, _RunState] = {}
_PENDING_LOCK = threading.Lock()


def _release(state: _RunState) -> None:
    state.delegate_state.close()
    state.vault.destroy_key()


def _park(state: _RunState, calls: list[dict[str, Any]]) -> None:
    state.pending_ids = [str(c.get("id")) for c in calls]
    state.expires = time.monotonic() + PENDING_TTL_SECONDS
    with _PENDING_LOCK:
        now = time.monotonic()
        for key, old in list(_PENDING.items()):
            if old.expires < now:
                _PENDING.pop(key, None)
                _release(old)
        for cid in state.pending_ids:
            _PENDING[cid] = state


def take_pending(tool_call_ids: list[str]) -> _RunState | None:
    """Claim the paused run these tool results belong to (once)."""
    with _PENDING_LOCK:
        state = next((_PENDING[c] for c in tool_call_ids if c in _PENDING), None)
        if state is None:
            return None
        for cid in state.pending_ids:
            _PENDING.pop(cid, None)
        if state.expires < time.monotonic():
            _release(state)
            return None
        return state


class ClinicalDualLobeEngine:
    name = "clinical-planner-executor"

    def __init__(self, *, execution_tools: list[Tool] | None = None, privacy_guard: PrivacyGuard | None = None,
                 provider_privacy_policy: ProviderPrivacyPolicy | None = None,
                 memory: JsonlMemoryStore | None = None) -> None:
        self.execution_tools = list(execution_tools or [])
        self.privacy_guard = privacy_guard or PrivacyGuard(provider_privacy_policy)
        self.memory = memory or memory_store()

    async def _call(self, *, alias: str, system: str, prompt: str, max_tokens: int,
                    tools: list[Tool] | None = None) -> str:
        text = str(await run_agent(alias=alias, system=system, prompt=prompt, max_tokens=max_tokens,
                                   timeout=get_settings().a_timeout, tools=tools) or "").strip()
        if not text:
            raise RuntimeError(f"{alias} returned an empty response")
        return text

    @staticmethod
    def _a_tokens() -> int:
        return int(os.getenv("DUAL_LOBE_A_MAX_TOKENS", "8000"))

    async def _make_plan(self, *, query: str, patient_context: str, memory_slice: str,
                         client_tools: list[dict[str, Any]]) -> Plan:
        # Plans are compact JSON; a small budget keeps A's first call fast.
        tool_names = ", ".join(sorted({(t.get("function") or {}).get("name", "") for t in client_tools}))
        raw = await self._call(alias="lobe-a", system=PLANNER_AGENT_SYSTEM,
                               prompt=build_plan_prompt(query=query, patient_context=patient_context)
                               + _memory_block(memory_slice)
                               + (f"\n\nTOOLS B CAN USE DURING EXECUTION: {tool_names}. Plan steps that use them "
                                  "where they provide the needed data." if tool_names else "")
                               + "\nKeep the plan compact: the fewest steps that achieve the goal, short actions.",
                               max_tokens=int(os.getenv("DUAL_LOBE_CLINICAL_PLAN_MAX_TOKENS", "1500")))
        return Plan.from_text(raw)

    async def _revise_plan(self, *, query: str, patient_context: str, contract: PlanContract, concern: str,
                           evidence: str) -> Plan:
        raw = await self._call(alias="lobe-a", system=PLANNER_AGENT_SYSTEM, prompt=build_revision_prompt(
            query=query, patient_context=patient_context, current_plan_json=contract.current_json(),
            concern=concern, evidence=evidence), max_tokens=self._a_tokens())
        return Plan.from_text(raw)

    async def _answer(self, prompt: str, on_delta) -> str:
        """A's user-facing call; streamed to the client when on_delta is given."""
        if on_delta is None:
            return await self._call(alias="lobe-a", system=PLANNER_AGENT_SYSTEM, prompt=prompt,
                                    max_tokens=self._a_tokens())
        text = (await stream_text(alias="lobe-a", system=PLANNER_AGENT_SYSTEM, prompt=prompt,
                                  max_tokens=self._a_tokens(), timeout=get_settings().a_timeout,
                                  on_delta=on_delta)).strip()
        if not text:
            raise RuntimeError("lobe-a returned an empty response")
        return text

    def _server_tools(self, trace: ProxyToolTrace) -> list[Tool]:
        return [*self.execution_tools, memory_search_tool(self.memory, trace)]

    def _remember(self, *, sanitized_query: str, plan: Plan, summary: str, answer: str,
                  vault: EphemeralTokenVault) -> None:
        """Write one sanitized long-term memory entry after the response (never raw patient data)."""
        text = (f"Task: {sanitized_query}\nPlan goal: {plan.goal}\n"
                + (f"Execution: {summary[:1500]}\n" if summary else "")
                + f"Answer: {answer[:3000]}")
        safe, _ = self.privacy_guard.sanitize(text, vault=vault)

        async def write() -> None:
            try:
                await asyncio.to_thread(self.memory.record, safe)
            except Exception:  # noqa: BLE001
                LOG.warning("clinical memory write failed", exc_info=True)

        task = asyncio.ensure_future(write())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

    async def run_clinical(self, *, query: str, patient_context: str,
                           local_delivery: Callable[[str], None] | None = None, on_delta=None,
                           client_tools: list[dict[str, Any]] | None = None) -> ClinicalRunResult:
        started = time.perf_counter()
        timings: dict[str, int | float] = {}
        client_tools = [t for t in (client_tools or []) if isinstance(t, dict) and t.get("function")]
        vault = self.privacy_guard.new_vault()
        sanitized_query, sanitized_context, receipt = self.privacy_guard.prepare(
            query=query, patient_context=patient_context, vault=vault)
        memory_hits = await asyncio.to_thread(self.memory.search, sanitized_query, 4, include_split_experience=False)
        memory_slice = "\n\n".join(f"- {x}" for x in memory_hits)[:4000]

        if not patient_context.strip() and not self.execution_tools and not client_tools:
            # Fast path: no patient data and no tools, so there is nothing for B to
            # execute. A answers directly (still from the sanitized query).
            try:
                t = time.perf_counter()
                answer = await self._answer(build_direct_prompt(sanitized_query) + _memory_block(memory_slice),
                                            on_delta)
                timings["a_answer_ms"] = int((time.perf_counter() - t) * 1000)
                if local_delivery is not None:
                    local_delivery(vault.rehydrate_text(answer))
                plan = Plan(goal=sanitized_query[:200] or "answer", steps=[{"id": "S1", "action": "answer directly"}],
                            success_condition="user question answered")
                self._remember(sanitized_query=sanitized_query, plan=plan, summary="", answer=answer, vault=vault)
                result = ClinicalRunResult(
                    sanitized_query=sanitized_query, sanitized_patient_context=sanitized_context, plan=plan,
                    plan_revision=0, plan_sha256="", execution_report="", delegated_results="", answer=answer,
                    timings_ms=timings, logical_model_calls=1, privacy_receipt=receipt,
                    memory_entries_used=len(memory_hits))
            finally:
                vault.destroy_key()
            result.privacy_receipt = self.privacy_guard.finalized_receipt(receipt, vault)
            result.timings_ms["total_ms"] = int((time.perf_counter() - started) * 1000)
            return result

        trace = ProxyToolTrace()
        delegate_state = ExecutionDelegateState()
        try:
            t = time.perf_counter()
            plan = await self._make_plan(query=sanitized_query, patient_context=sanitized_context,
                                         memory_slice=memory_slice, client_tools=client_tools)
            timings["a_plan_ms"] = int((time.perf_counter() - t) * 1000)
        except BaseException:
            delegate_state.close()
            vault.destroy_key()
            raise
        contract = PlanContract(plan)
        trace.add("plan_frozen", input_text=f"revision={contract.revision}", output_text=contract.current_json(),
                  provenance="deterministic_plan_contract")
        state = _RunState(query=query, patient_context=patient_context, sanitized_query=sanitized_query,
                          sanitized_context=sanitized_context, receipt=receipt, vault=vault, trace=trace,
                          delegate_state=delegate_state, contract=contract, client_tools=client_tools,
                          memory_slice=memory_slice, memory_entries=len(memory_hits), timings=timings,
                          started=started)
        return await self._continue(state, on_delta=on_delta, local_delivery=local_delivery)

    async def resume_clinical(self, state: _RunState, tool_results: dict[str, str], *, on_delta=None,
                              local_delivery: Callable[[str], None] | None = None) -> ClinicalRunResult:
        """Continue a paused run with the client's tool results."""
        messages = list(state.b_messages or [])
        for cid in state.pending_ids:
            content = tool_results.get(cid, "TOOL_RESULT_MISSING: the client returned no result for this call.")
            messages.append({"role": "tool", "tool_call_id": cid, "content": content})
            state.trace.add("client_tool_result", input_text=f"tool_call_id={cid}", output_text=content,
                            provenance="client_tool_execution")
        state.b_messages = messages
        return await self._continue(state, on_delta=on_delta, local_delivery=local_delivery)

    async def _continue(self, state: _RunState, *, on_delta, local_delivery) -> ClinicalRunResult:
        """Run (or resume) B's execution; pause again for client tools or finish with A's review."""
        contract, trace = state.contract, state.trace

        async def planner_callback(concern: str, evidence: str) -> Plan:
            return await self._revise_plan(query=state.sanitized_query, patient_context=state.sanitized_context,
                                           contract=contract, concern=concern, evidence=evidence)

        parked = False
        try:
            server_tools = self._server_tools(trace)
            tools = make_b_tools(contract=contract, planner_callback=planner_callback, raw_query=state.query,
                                 raw_patient_context=state.patient_context, execution_tools=server_tools,
                                 trace=trace, run_state=state.delegate_state)
            prompt = (build_execution_prompt(query=state.query, patient_context=state.patient_context,
                                             current_plan_json=contract.current_json(), revision=contract.revision)
                      + _memory_block(state.memory_slice))
            t = time.perf_counter()
            outcome = await run_agent(
                alias=CLINICAL_B_ALIAS, system=B_EXECUTOR_SYSTEM + (CLIENT_TOOLS_NOTE if state.client_tools else ""),
                prompt=prompt, max_tokens=clinical_b_tokens(), timeout=get_settings().a_timeout, tools=tools,
                client_tools=state.client_tools, history=state.b_messages)
            state.b_rounds += 1
            state.timings["b_execute_ms"] = state.timings.get("b_execute_ms", 0) + int((time.perf_counter() - t) * 1000)

            if isinstance(outcome, AgentPause):
                state.b_messages = outcome.messages
                for call in outcome.calls:
                    fn = call.get("function") or {}
                    trace.add("client_tool_call", input_text=f"{fn.get('name')} {fn.get('arguments')}",
                              output_text="sent to the client for execution", provenance="b_client_tool_request")
                _park(state, outcome.calls)
                parked = True
                return ClinicalRunResult(
                    sanitized_query=state.sanitized_query, sanitized_patient_context=state.sanitized_context,
                    plan=contract.current, plan_revision=contract.revision, plan_sha256=contract.fingerprint(),
                    execution_report="", delegated_results="", answer="", timings_ms=dict(state.timings),
                    logical_model_calls=1 + state.b_rounds + contract.revision + state.delegate_state.child_count,
                    tool_calls=outcome.calls, memory_entries_used=state.memory_entries)

            execution_report_raw = str(outcome or "").strip()
            if not execution_report_raw:
                raise RuntimeError(f"{CLINICAL_B_ALIAS} returned an empty response")
            delegated_results_raw = await collect_all_execution_results(state.delegate_state, trace)
            report = ExecutionReport.from_text(execution_report_raw)
            report.assert_matches_contract(contract)

            vault, guard = state.vault, self.privacy_guard
            safe_report, _ = guard.sanitize_trace(execution_report_raw, vault=vault)
            safe_delegated, _ = guard.sanitize_trace(delegated_results_raw, vault=vault)
            safe_trace, trace_phi_types = guard.sanitize_trace(trace.render(), vault=vault)
            receipt = replace(state.receipt, direct_identifier_types=tuple(sorted(
                set(state.receipt.direct_identifier_types) | set(trace_phi_types))), token_count=vault.token_count)

            t = time.perf_counter()
            final_answer = await self._answer(build_final_review_prompt(
                query=state.sanitized_query, patient_context=state.sanitized_context,
                final_plan_json=contract.current_json(), plan_revision=contract.revision,
                execution_report=safe_report, delegated_results=safe_delegated, trace_text=safe_trace)
                + _memory_block(state.memory_slice), on_delta)
            state.timings["a_review_ms"] = int((time.perf_counter() - t) * 1000)

            if local_delivery is not None:
                local_delivery(vault.rehydrate_text(final_answer))
            summary, _ = guard.sanitize(report.summary or "", vault=vault)
            self._remember(sanitized_query=state.sanitized_query, plan=contract.current, summary=summary,
                           answer=final_answer, vault=vault)

            result = ClinicalRunResult(
                sanitized_query=state.sanitized_query, sanitized_patient_context=state.sanitized_context,
                plan=contract.current, plan_revision=contract.revision, plan_sha256=contract.fingerprint(),
                execution_report=safe_report, delegated_results=safe_delegated, answer=final_answer,
                trace_event_count=len(trace.snapshot_from(0)),
                trace_sha256=hashlib.sha256(safe_trace.encode("utf-8")).hexdigest(), timings_ms=state.timings,
                logical_model_calls=2 + state.b_rounds + contract.revision + state.delegate_state.child_count,
                privacy_receipt=receipt, memory_entries_used=state.memory_entries)
        finally:
            if not parked:
                _release(state)

        result.privacy_receipt = self.privacy_guard.finalized_receipt(result.privacy_receipt, state.vault)
        result.timings_ms["total_ms"] = int((time.perf_counter() - state.started) * 1000)
        return result
