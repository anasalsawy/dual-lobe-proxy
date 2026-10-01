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
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from ...core.settings import get_settings
from ...provider import calltrace
from ...provider.adapters import NormalizedRequest, response_dict
from .. import common
from ..common import AgentPause, ProxyToolTrace, Tool, client_tool_names, extract_json_object, run_agent, stream_text
from ..split.memory import JsonlMemoryStore
from ..split.tools import memory_search_tool
from ..split.models import Verdict, Handoff
from .access import tools_for_a, tools_for_b, tool_name
from .models import ExecutionReport, Plan, PlanContract
from .privacy import EphemeralTokenVault, PrivacyGuard, PrivacyReceipt, ProviderPrivacyPolicy
from .prompts import (DIRECT_VERIFIER_SYSTEM, build_direct_verification_prompt, build_execution_prompt,
                      build_final_review_prompt, build_revision_prompt, build_a_work_prompt)
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
    "Role: Lobe A — Reasoning and User-Facing Intelligence\n"
    "Work normally: answer directly when execution is unnecessary, or use the create_execution_plan function "
    "to hand B a complete plan when execution is needed. After B executes, review B's report and communicate "
    "the verified result to the user. You own reasoning, planning, and final review; B owns execution."
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
    plan: Plan | None
    plan_revision: int
    plan_sha256: str
    execution_report: str
    delegated_results: str
    answer: str
    verdict: Verdict | None = None
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
    contract: PlanContract | None
    client_tools: list[dict[str, Any]]
    a_client_tools: list[dict[str, Any]]
    phase: str
    memory_slice: str
    memory_entries: int
    timings: dict[str, int | float]
    started: float
    a_messages: list[dict[str, Any]] = field(default_factory=list)
    sensitive_context_present: bool = False
    b_messages: list[dict[str, Any]] | None = None
    pending_ids: list[str] = field(default_factory=list)
    b_rounds: int = 0
    tool_nudged: bool = False
    expires: float = 0.0


@dataclass
class _AWorkResult:
    answer: str | None = None
    plan: Plan | None = None
    tool_calls: list[dict[str, Any]] | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)


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

    async def _a_work(self, *, query: str, memory_slice: str,
                      client_tools: list[dict[str, Any]], a_client_tools: list[dict[str, Any]],
                      history: list[dict[str, Any]] | None = None,
                      sensitive_context_present: bool = False) -> _AWorkResult:
        """A answers normally, or signals B handoff by calling create_execution_plan."""
        b_tool_descriptions = [
            f"{tool_name(item)}: {(item.get('function') or {}).get('description') or 'No description supplied.'}"
            for item in client_tools if tool_name(item)
        ]
        b_tool_descriptions.extend(f"{tool.name}: {tool.description}" for tool in self.execution_tools)
        a_tool_descriptions = [
            f"{tool_name(item)}: {(item.get('function') or {}).get('description') or 'No description supplied.'}"
            for item in a_client_tools if tool_name(item)
        ]
        prompt = build_a_work_prompt(query=query, patient_context="", available_tools=b_tool_descriptions,
                                     a_tools=a_tool_descriptions, memory_slice="",
                                     sensitive_context_present=sensitive_context_present)
        plan_tool = {"type": "function", "function": {
            "name": "create_execution_plan",
            "description": "Hand a complete execution plan to Lobe B. Call only when this task needs execution.",
            "parameters": {"type": "object", "properties": {
                "goal": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "steps": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "action": {"type": "string"},
                    "parallelizable": {"type": "boolean"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                }, "required": ["id", "action"]}},
                "success_condition": {"type": "string"},
                "research_context": {"type": "array", "items": {"type": "string"},
                                     "description": "Questions or facts B should research and summarize with sources; do not claim to have browsed."},
            }, "required": ["goal", "steps", "success_condition"]}}
        }
        messages = history or [
            {"role": "system", "content": PLANNER_AGENT_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        available_tools = [plan_tool, *a_client_tools]
        with calltrace.stage("A-work"):
            response = await common.get_registry().adapter("lobe-a").buffered(NormalizedRequest(
                messages=messages,
                max_tokens=self._a_tokens(),
                timeout=get_settings().a_timeout, tools=available_tools, tool_choice="auto"))
        data = response_dict(response)
        message = ((data.get("choices") or [{}])[0].get("message") or {})
        calls = message.get("tool_calls") or []
        if not calls:
            answer = str(message.get("content") or "").strip()
            if not answer:
                raise RuntimeError("lobe-a returned neither a direct answer nor an execution plan")
            return _AWorkResult(answer=answer, messages=messages)
        names = [tool_name(call) for call in calls]
        if "create_execution_plan" in names:
            if len(calls) != 1:
                raise RuntimeError("A must return either a plan or safe tool calls, not both")
            args = (calls[0].get("function") or {}).get("arguments") or "{}"
            plan_data = args if isinstance(args, dict) else extract_json_object(str(args))
            return _AWorkResult(plan=Plan.model_validate(plan_data), messages=messages)
        allowed = {tool_name(tool) for tool in a_client_tools}
        unexpected = sorted(set(names) - allowed)
        if unexpected:
            raise PermissionError(f"lobe-a requested tools outside its clinical permission set: {unexpected}")
        tagged_calls = []
        for call in calls:
            tagged = dict(call)
            tagged["id"] = "clinicalA_" + str(call.get("id") or uuid.uuid4().hex)[-48:]
            tagged_calls.append(tagged)
        history_messages = list(messages) + [{"role": "assistant", "content": message.get("content"),
                                               "tool_calls": tagged_calls}]
        return _AWorkResult(tool_calls=tagged_calls, messages=history_messages)

    async def _revise_plan(self, *, query: str, patient_context: str, contract: PlanContract, concern: str,
                           evidence: str) -> Plan:
        with calltrace.stage("A-revise"):
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

    async def _verify_direct_answer(self, *, query: str, answer: str, memory_slice: str) -> Verdict:
        """Have B assess A's unchanged direct answer; missing/invalid B output is never GREEN."""
        fallback = Verdict(deception_level="YELLOW",
                           rationale="B verification failed or returned an invalid review; answer remains unverified.",
                           handoff=Handoff(unverified=["A's direct answer could not be independently verified"]))
        try:
            with calltrace.stage("B-verify"):
                raw = await self._call(alias=CLINICAL_B_ALIAS, system=DIRECT_VERIFIER_SYSTEM,
                                       prompt=build_direct_verification_prompt(query=query, answer=answer,
                                                                               memory_slice=memory_slice),
                                       max_tokens=min(clinical_b_tokens(), 1200))
            parsed = extract_json_object(raw)
            return Verdict(deception_level=parsed.get("deception_level"),
                           rationale=str(parsed.get("rationale") or "B returned no rationale."),
                           handoff=Handoff(missing=parsed.get("missing") or [],
                                           unverified=parsed.get("unverified") or [],
                                           proof_requests=parsed.get("proof_requests") or []))
        except Exception:  # noqa: BLE001
            LOG.warning("clinical direct-answer verification failed", exc_info=True)
            return fallback

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
        all_tools = [t for t in (client_tools or []) if isinstance(t, dict) and t.get("function")]
        a_client_tools = tools_for_a(all_tools)
        client_tools = tools_for_b(all_tools)
        vault = self.privacy_guard.new_vault()
        sanitized_query, sanitized_context, receipt = self.privacy_guard.prepare(
            query=query, patient_context=patient_context, vault=vault)
        sensitive_context_present = bool(sanitized_context.strip() or receipt.direct_identifier_types)
        memory_hits = await asyncio.to_thread(self.memory.search, sanitized_query, 4, include_split_experience=False)
        memory_slice = "\n\n".join(f"- {x}" for x in memory_hits)[:4000]

        trace = ProxyToolTrace()
        delegate_state = ExecutionDelegateState()
        try:
            t = time.perf_counter()
            a_work = await self._a_work(query=sanitized_query, memory_slice="", client_tools=client_tools,
                                        a_client_tools=a_client_tools,
                                        sensitive_context_present=sensitive_context_present)
            timings["a_work_ms"] = int((time.perf_counter() - t) * 1000)
        except BaseException:
            delegate_state.close()
            vault.destroy_key()
            raise

        state = _RunState(query=query, patient_context=patient_context, sanitized_query=sanitized_query,
                          sanitized_context=sanitized_context, receipt=receipt, vault=vault, trace=trace,
                          delegate_state=delegate_state, contract=None, client_tools=client_tools,
                          a_client_tools=a_client_tools, phase="a_work", memory_slice="",
                          memory_entries=len(memory_hits), timings=timings, started=started,
                          a_messages=a_work.messages, sensitive_context_present=sensitive_context_present)
        if a_work.tool_calls:
            _park(state, a_work.tool_calls)
            return ClinicalRunResult(
                sanitized_query=sanitized_query, sanitized_patient_context=sanitized_context, plan=None,
                plan_revision=0, plan_sha256="", execution_report="", delegated_results="", answer="",
                timings_ms=dict(timings), logical_model_calls=1, privacy_receipt=receipt,
                tool_calls=a_work.tool_calls, memory_entries_used=len(memory_hits))

        if a_work.plan is None:
            # A answered normally without a plan; B independently verifies that exact answer.
            try:
                answer = str(a_work.answer or "").strip()
                if on_delta is not None:
                    await on_delta(answer)
                t = time.perf_counter()
                verdict = await self._verify_direct_answer(query=sanitized_query, answer=answer,
                                                           memory_slice=memory_slice)
                if verdict.deception_level == "GREEN" and (verdict.handoff.missing or verdict.handoff.unverified
                                                            or verdict.handoff.proof_requests):
                    verdict = verdict.model_copy(update={
                        "deception_level": "YELLOW",
                        "rationale": ("Material evidence gaps remain unresolved; GREEN is not allowed. "
                                      + verdict.rationale)[:1600],
                    })
                timings["b_verify_ms"] = int((time.perf_counter() - t) * 1000)
                if local_delivery is not None:
                    local_delivery(vault.rehydrate_text(answer))
                plan = Plan(goal=sanitized_query[:200] or "answer", steps=[{"id": "S1", "action": "answer directly"}],
                            success_condition="user question answered")
                self._remember(sanitized_query=sanitized_query, plan=plan, summary="", answer=answer, vault=vault)
                result = ClinicalRunResult(
                    sanitized_query=sanitized_query, sanitized_patient_context=sanitized_context, plan=plan,
                    plan_revision=0, plan_sha256="", execution_report="", delegated_results="", answer=answer,
                    timings_ms=timings, logical_model_calls=2, privacy_receipt=receipt, verdict=verdict,
                    memory_entries_used=len(memory_hits))
            finally:
                vault.destroy_key()
            result.privacy_receipt = self.privacy_guard.finalized_receipt(receipt, vault)
            result.timings_ms["total_ms"] = int((time.perf_counter() - started) * 1000)
            return result

        if a_work.plan is None:  # Defensive runtime guard.
            delegate_state.close()
            vault.destroy_key()
            raise RuntimeError("lobe-a selected execution without a plan")
        plan = a_work.plan
        contract = PlanContract(plan)
        trace.add("plan_frozen", input_text=f"revision={contract.revision}", output_text=contract.current_json(),
                  provenance="deterministic_plan_contract")
        state.contract = contract
        state.phase = "b_execution"
        state.memory_slice = ""
        return await self._continue(state, on_delta=on_delta, local_delivery=local_delivery)

    async def resume_clinical(self, state: _RunState, tool_results: dict[str, str], *, on_delta=None,
                              local_delivery: Callable[[str], None] | None = None) -> ClinicalRunResult:
        """Continue a paused run with the client's tool results."""
        if state.phase == "a_work":
            messages = list(state.a_messages)
            for cid in state.pending_ids:
                content = tool_results.get(cid, "TOOL_RESULT_MISSING: the client returned no result for this call.")
                safe_content, _ = self.privacy_guard.sanitize(content, vault=state.vault)
                messages.append({"role": "tool", "tool_call_id": cid, "content": safe_content})
                state.trace.add("a_safe_tool_result", input_text=f"tool_call_id={cid}", output_text=safe_content,
                                provenance="client_tool_execution")
            state.pending_ids = []
            t = time.perf_counter()
            try:
                a_work = await self._a_work(query=state.sanitized_query, memory_slice="",
                                            client_tools=state.client_tools, a_client_tools=state.a_client_tools,
                                            history=messages,
                                            sensitive_context_present=state.sensitive_context_present)
                state.timings["a_work_ms"] += int((time.perf_counter() - t) * 1000)
                if a_work.tool_calls:
                    state.a_messages = a_work.messages
                    _park(state, a_work.tool_calls)
                    return ClinicalRunResult(
                        sanitized_query=state.sanitized_query,
                        sanitized_patient_context=state.sanitized_context, plan=None, plan_revision=0,
                        plan_sha256="", execution_report="", delegated_results="", answer="",
                        timings_ms=dict(state.timings), logical_model_calls=2, privacy_receipt=state.receipt,
                        tool_calls=a_work.tool_calls, memory_entries_used=state.memory_entries)
                if a_work.plan is not None:
                    state.contract = PlanContract(a_work.plan)
                    state.phase = "b_execution"
                    state.trace.add("plan_frozen", input_text="revision=0",
                                    output_text=state.contract.current_json(),
                                    provenance="deterministic_plan_contract")
                    return await self._continue(state, on_delta=on_delta, local_delivery=local_delivery)
                answer = str(a_work.answer or "").strip()
                verdict = await self._verify_direct_answer(query=state.sanitized_query, answer=answer,
                                                           memory_slice="")
                if verdict.deception_level == "GREEN" and (verdict.handoff.missing or verdict.handoff.unverified
                                                            or verdict.handoff.proof_requests):
                    verdict = verdict.model_copy(update={"deception_level": "YELLOW"})
                if on_delta is not None:
                    await on_delta(answer)
                if local_delivery is not None:
                    local_delivery(state.vault.rehydrate_text(answer))
                placeholder = Plan(goal=state.sanitized_query[:200] or "answer", steps=[
                    {"id": "S1", "action": "answer directly"}], success_condition="user question answered")
                result = ClinicalRunResult(
                    sanitized_query=state.sanitized_query, sanitized_patient_context=state.sanitized_context,
                    plan=placeholder, plan_revision=0, plan_sha256="", execution_report="", delegated_results="",
                    answer=answer, verdict=verdict, timings_ms=state.timings, logical_model_calls=3,
                    privacy_receipt=state.receipt, memory_entries_used=state.memory_entries)
                return result
            finally:
                if state.phase != "a_work" or not state.pending_ids:
                    _release(state)
        messages = list(state.b_messages or [])
        for cid in state.pending_ids:
            content = tool_results.get(cid, "TOOL_RESULT_MISSING: the client returned no result for this call.")
            messages.append({"role": "tool", "tool_call_id": cid, "content": content})
            state.trace.add("client_tool_result", input_text=f"tool_call_id={cid}", output_text=content,
                            provenance="client_tool_execution")
        state.b_messages = messages
        return await self._continue(state, on_delta=on_delta, local_delivery=local_delivery)

    @staticmethod
    def _planned_tools_not_called(state: _RunState) -> list[str]:
        """Client tools the plan names that B has not called in this run."""
        plan_text = state.contract.current_json()
        named = [n for n in client_tool_names(state.client_tools) if n and n in plan_text]
        called = {e.input_text.split(" ", 1)[0] for e in state.trace.snapshot_from(0) if e.name == "client_tool_call"}
        return sorted(n for n in named if n not in called)

    async def _continue(self, state: _RunState, *, on_delta, local_delivery) -> ClinicalRunResult:
        """Run (or resume) B's execution; pause again for client tools or finish with A's review."""
        contract, trace = state.contract, state.trace
        if contract is None:
            raise RuntimeError("B execution cannot start without A's plan contract")

        async def planner_callback(concern: str, evidence: str) -> Plan:
            safe_concern, _ = self.privacy_guard.sanitize(concern, vault=state.vault)
            safe_evidence, _ = self.privacy_guard.sanitize(evidence, vault=state.vault)
            return await self._revise_plan(query=state.sanitized_query, patient_context="",
                                           contract=contract, concern=safe_concern, evidence=safe_evidence)

        parked = False
        try:
            server_tools = self._server_tools(trace)
            tools = make_b_tools(contract=contract, planner_callback=planner_callback, raw_query=state.query,
                                 raw_patient_context=state.patient_context, execution_tools=server_tools,
                                 trace=trace, run_state=state.delegate_state)
            prompt = (build_execution_prompt(query=state.query, patient_context=state.patient_context,
                                             current_plan_json=contract.current_json(), revision=contract.revision)
                      + _memory_block(state.memory_slice))
            system_b = B_EXECUTOR_SYSTEM + (CLIENT_TOOLS_NOTE if state.client_tools else "")
            history = state.b_messages
            while True:
                t = time.perf_counter()
                with calltrace.stage("B-execute"):
                    outcome = await run_agent(
                        alias=CLINICAL_B_ALIAS, system=system_b, prompt=prompt, max_tokens=clinical_b_tokens(),
                        timeout=get_settings().a_timeout, tools=tools, client_tools=state.client_tools,
                        history=history)
                state.b_rounds += 1
                state.timings["b_execute_ms"] = (state.timings.get("b_execute_ms", 0)
                                                 + int((time.perf_counter() - t) * 1000))
                if isinstance(outcome, AgentPause):
                    break
                missing = self._planned_tools_not_called(state)
                if not missing or state.tool_nudged:
                    break
                # Code-enforced: the plan needs client tools B never called. Send B back once.
                state.tool_nudged = True
                nudge = (f"The current plan requires these tools, but you did not call them: {', '.join(missing)}. "
                         "Call them now to get the real data. Do not report steps as completed without the "
                         "tool results.")
                trace.add("tool_use_enforced", input_text=", ".join(missing), output_text=nudge,
                          provenance="deterministic_plan_contract")
                base = history or [{"role": "system", "content": system_b}, {"role": "user", "content": prompt}]
                history = base + [{"role": "assistant", "content": str(outcome or "")},
                                  {"role": "user", "content": nudge}]

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
            # Raw page/database/tool output stays on B's side. A receives only
            # B's concise summary, deterministic completion statuses, and
            # tool/event names without arguments or outputs.
            safe_report_text, report_types = guard.sanitize_trace(
                json.dumps({"plan_revision": report.plan_revision,
                            "steps": [{"id": step.id, "status": step.status}
                                      for step in report.steps],
                            "summary": report.summary[:2000],
                            "open_issue": report.open_issue[:1000]}, ensure_ascii=False), vault=vault)
            safe_delegated, delegated_types = guard.sanitize_trace(delegated_results_raw, vault=vault)
            events = trace.snapshot_from(0)
            safe_trace = json.dumps([{"event": e.name, "provenance": e.provenance} for e in events],
                                    ensure_ascii=False)
            trace_phi_types: set[str] = set()
            for source in (execution_report_raw, delegated_results_raw, trace.render()):
                _, found = guard.sanitize_trace(source, vault=vault)
                trace_phi_types.update(found)
            receipt = replace(state.receipt, direct_identifier_types=tuple(sorted(
                set(state.receipt.direct_identifier_types) | set(report_types) | set(delegated_types)
                | trace_phi_types)), token_count=vault.token_count)

            t = time.perf_counter()
            with calltrace.stage("A-review"):
                final_answer = await self._answer(build_final_review_prompt(
                query=state.sanitized_query, patient_context="",
                final_plan_json=contract.current_json(), plan_revision=contract.revision,
                execution_report=safe_report_text,
                delegated_results=("B collected delegated results and included relevant findings in its safe summary."
                                   if delegated_results_raw else ""), trace_text=safe_trace)
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
                execution_report=safe_report_text, delegated_results="", answer=final_answer,
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
