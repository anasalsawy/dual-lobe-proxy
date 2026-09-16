"""Provider-independent benchmark records and aggregate metrics."""

from __future__ import annotations

from collections.abc import Iterable
from statistics import mean

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    category: str
    prompt: str
    acceptance_criteria: tuple[str, ...] = ()
    expected_scope: dict[str, str] = Field(default_factory=dict)
    tool_fixture: dict[str, object] | None = None


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    design_id: str
    task_id: str
    success: bool
    scope_status: str
    false_success: bool = False
    missed_concerns: int = 0
    false_positive_concerns: int = 0
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float | None = None
    tool_calls: int = 0
    duplicate_side_effects: int = 0
    raw_events_path: str


def aggregate(results: Iterable[RunResult]) -> dict[str, float | int]:
    rows = list(results)
    if not rows:
        return {"count": 0, "success_rate": 0.0, "false_success_rate": 0.0, "mean_latency_ms": 0.0}
    return {
        "count": len(rows),
        "success_rate": mean(row.success for row in rows),
        "false_success_rate": mean(row.false_success for row in rows),
        "mean_latency_ms": mean(row.latency_ms for row in rows),
        "mean_input_tokens": mean(row.input_tokens for row in rows),
        "mean_output_tokens": mean(row.output_tokens for row in rows),
        "duplicate_side_effects": sum(row.duplicate_side_effects for row in rows),
    }
