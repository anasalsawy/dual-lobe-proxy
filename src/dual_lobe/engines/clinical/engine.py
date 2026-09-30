"""Model 2 engine (ported from the CrewAI ClinicalDualLobeEngine, same logic, no CrewAI).

A owns reasoning, planning, plan revision, final review, and the user-facing
answer; it has no tools and sees only privacy-sanitized input. B is local, owns
tool/data execution, can challenge A at any time, and may parallelize
independent plan steps. The current A-authored plan is held by deterministic
code as the execution contract; B cannot silently rewrite it.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from ...core.settings import get_settings
from ..common import ProxyToolTrace, Tool, run_agent
from .models import ExecutionReport, Plan, PlanContract
from .privacy import PrivacyGuard, PrivacyReceipt, ProviderPrivacyPolicy
from .prompts import build_execution_prompt, build_final_review_prompt, build_plan_prompt, build_revision_prompt
from .tools import (
    B_EXECUTOR_SYSTEM,
    CLINICAL_B_ALIAS,
    ExecutionDelegateState,
    clinical_b_tokens,
    collect_all_execution_results,
    make_b_tools,
)

PLANNER_AGENT_SYSTEM = (
    "Role: Lobe A — Planner and User-Facing Intelligence\n"
    "Goal: Understand the user's goal, create or revise the complete plan, and judge whether execution actually "
    "fulfilled it.\n\n"
    "You are the reasoning lobe. You do not own execution tools. You think, plan, revise when challenged by the "
    "execution lobe, and communicate the final result to the user."
)


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


class ClinicalDualLobeEngine:
    name = "clinical-planner-executor"

    def __init__(self, *, execution_tools: list[Tool] | None = None, privacy_guard: PrivacyGuard | None = None,
                 provider_privacy_policy: ProviderPrivacyPolicy | None = None) -> None:
        self.execution_tools = list(execution_tools or [])
        self.privacy_guard = privacy_guard or PrivacyGuard(provider_privacy_policy)

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

    async def _make_plan(self, *, query: str, patient_context: str) -> Plan:
        raw = await self._call(alias="lobe-a", system=PLANNER_AGENT_SYSTEM,
                               prompt=build_plan_prompt(query=query, patient_context=patient_context),
                               max_tokens=self._a_tokens())
        return Plan.from_text(raw)

    async def _revise_plan(self, *, query: str, patient_context: str, contract: PlanContract, concern: str,
                           evidence: str) -> Plan:
        raw = await self._call(alias="lobe-a", system=PLANNER_AGENT_SYSTEM, prompt=build_revision_prompt(
            query=query, patient_context=patient_context, current_plan_json=contract.current_json(),
            concern=concern, evidence=evidence), max_tokens=self._a_tokens())
        return Plan.from_text(raw)

    async def _execute(self, *, raw_query: str, raw_patient_context: str, contract: PlanContract,
                       trace: ProxyToolTrace, planner_callback, delegate_state: ExecutionDelegateState) -> str:
        tools = make_b_tools(contract=contract, planner_callback=planner_callback, raw_query=raw_query,
                             raw_patient_context=raw_patient_context, execution_tools=self.execution_tools,
                             trace=trace, run_state=delegate_state)
        return await self._call(alias=CLINICAL_B_ALIAS, system=B_EXECUTOR_SYSTEM, prompt=build_execution_prompt(
            query=raw_query, patient_context=raw_patient_context, current_plan_json=contract.current_json(),
            revision=contract.revision), max_tokens=clinical_b_tokens(), tools=tools)

    async def _final_review(self, *, query: str, patient_context: str, contract: PlanContract,
                            execution_report: str, delegated_results: str, trace_text: str) -> str:
        return await self._call(alias="lobe-a", system=PLANNER_AGENT_SYSTEM, prompt=build_final_review_prompt(
            query=query, patient_context=patient_context, final_plan_json=contract.current_json(),
            plan_revision=contract.revision, execution_report=execution_report,
            delegated_results=delegated_results, trace_text=trace_text), max_tokens=self._a_tokens())

    async def run_clinical(self, *, query: str, patient_context: str,
                           local_delivery: Callable[[str], None] | None = None) -> ClinicalRunResult:
        started = time.perf_counter()
        timings: dict[str, int | float] = {}
        vault = self.privacy_guard.new_vault()
        sanitized_query, sanitized_context, receipt = self.privacy_guard.prepare(
            query=query, patient_context=patient_context, vault=vault)
        trace = ProxyToolTrace()
        delegate_state = ExecutionDelegateState()

        try:
            t = time.perf_counter()
            plan = await self._make_plan(query=sanitized_query, patient_context=sanitized_context)
            timings["a_plan_ms"] = int((time.perf_counter() - t) * 1000)
            contract = PlanContract(plan)
            trace.add("plan_frozen", input_text=f"revision={contract.revision}", output_text=contract.current_json(),
                      provenance="deterministic_plan_contract")

            async def planner_callback(concern: str, evidence: str) -> Plan:
                return await self._revise_plan(query=sanitized_query, patient_context=sanitized_context,
                                               contract=contract, concern=concern, evidence=evidence)

            t = time.perf_counter()
            execution_report_raw = await self._execute(
                raw_query=query, raw_patient_context=patient_context, contract=contract, trace=trace,
                planner_callback=planner_callback, delegate_state=delegate_state)
            timings["b_execute_ms"] = int((time.perf_counter() - t) * 1000)

            delegated_results_raw = await collect_all_execution_results(delegate_state, trace)
            report = ExecutionReport.from_text(execution_report_raw)
            report.assert_matches_contract(contract)

            safe_report, _ = self.privacy_guard.sanitize_trace(execution_report_raw, vault=vault)
            safe_delegated, _ = self.privacy_guard.sanitize_trace(delegated_results_raw, vault=vault)
            safe_trace, trace_phi_types = self.privacy_guard.sanitize_trace(trace.render(), vault=vault)
            receipt = replace(receipt, direct_identifier_types=tuple(sorted(
                set(receipt.direct_identifier_types) | set(trace_phi_types))), token_count=vault.token_count)

            t = time.perf_counter()
            final_answer = await self._final_review(
                query=sanitized_query, patient_context=sanitized_context, contract=contract,
                execution_report=safe_report, delegated_results=safe_delegated, trace_text=safe_trace)
            timings["a_review_ms"] = int((time.perf_counter() - t) * 1000)

            if local_delivery is not None:
                local_delivery(vault.rehydrate_text(final_answer))

            result = ClinicalRunResult(
                sanitized_query=sanitized_query, sanitized_patient_context=sanitized_context,
                plan=contract.current, plan_revision=contract.revision, plan_sha256=contract.fingerprint(),
                execution_report=safe_report, delegated_results=safe_delegated, answer=final_answer,
                trace_event_count=len(trace.snapshot_from(0)),
                trace_sha256=hashlib.sha256(safe_trace.encode("utf-8")).hexdigest(), timings_ms=timings,
                logical_model_calls=3 + contract.revision + delegate_state.child_count, privacy_receipt=receipt)
        finally:
            delegate_state.close()
            vault.destroy_key()

        result.privacy_receipt = self.privacy_guard.finalized_receipt(receipt, vault)
        result.timings_ms["total_ms"] = int((time.perf_counter() - started) * 1000)
        return result
