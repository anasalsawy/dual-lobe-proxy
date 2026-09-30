from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Literal
from pydantic import BaseModel, Field, model_validator


class PlanStep(BaseModel):
    id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    parallelizable: bool = False
    depends_on: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    goal: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(min_length=1)
    success_condition: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self):
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step ids must be unique")
        known = set(ids)
        for step in self.steps:
            unknown = [x for x in step.depends_on if x not in known]
            if unknown:
                raise ValueError(f"{step.id} depends on unknown steps: {unknown}")
            if step.id in step.depends_on:
                raise ValueError(f"{step.id} cannot depend on itself")

        graph = {step.id: list(step.depends_on) for step in self.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visited:
                return
            if node in visiting:
                raise ValueError("plan dependencies must be acyclic")
            visiting.add(node)
            for dep in graph[node]:
                visit(dep)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)
        return self

    @classmethod
    def from_text(cls, raw: str) -> "Plan":
        return cls.model_validate(_json_object(raw))


class ExecutionStepResult(BaseModel):
    id: str
    status: Literal["completed", "failed", "not_run"]
    result: str = ""
    evidence: str = ""


class ExecutionReport(BaseModel):
    plan_revision: int = 0
    steps: list[ExecutionStepResult] = Field(default_factory=list)
    summary: str = ""
    open_issue: str = ""

    @classmethod
    def from_text(cls, raw: str) -> "ExecutionReport":
        return cls.model_validate(_json_object(raw))

    def assert_matches_contract(self, contract: "PlanContract") -> None:
        current_ids = {step.id for step in contract.current.steps}
        reported_ids = [step.id for step in self.steps]
        if len(reported_ids) != len(set(reported_ids)):
            raise RuntimeError("B execution report contains duplicate plan step ids")
        reported = set(reported_ids)
        unknown = reported - current_ids
        if unknown:
            raise RuntimeError(f"B reported steps outside the current plan: {sorted(unknown)}")
        if self.plan_revision != contract.revision:
            raise RuntimeError(
                f"B execution report used plan revision {self.plan_revision}, "
                f"but current plan revision is {contract.revision}"
            )
        missing = current_ids - reported
        if missing:
            raise RuntimeError(f"B execution report is missing plan steps: {sorted(missing)}")


class PlanContract:
    """Deterministic holder of the current A-authored plan.

    B can read it and ask A to revise it. Only a new Plan returned by A can
    replace the current contract.
    """

    def __init__(self, plan: Plan):
        self._lock = threading.Lock()
        self._current = plan
        self._revision = 0
        self._history: list[tuple[int, Plan, str]] = [(0, plan, "initial")]

    @property
    def current(self) -> Plan:
        with self._lock:
            return self._current.model_copy(deep=True)

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def revise(self, new_plan: Plan, *, reason: str) -> int:
        with self._lock:
            self._revision += 1
            self._current = new_plan.model_copy(deep=True)
            self._history.append((self._revision, self._current, str(reason or "")))
            return self._revision

    def current_json(self) -> str:
        with self._lock:
            return json.dumps(self._current.model_dump(), ensure_ascii=False, sort_keys=True)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.current_json().encode("utf-8")).hexdigest()

    def step(self, step_id: str) -> PlanStep:
        with self._lock:
            for step in self._current.steps:
                if step.id == step_id:
                    return step.model_copy(deep=True)
        raise KeyError(step_id)


def _json_object(raw: str) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value
