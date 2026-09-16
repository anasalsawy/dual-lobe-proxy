"""Provider-independent benchmark records and aggregate metrics."""

from __future__ import annotations

from collections.abc import Iterable
from statistics import mean
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkTask(BaseModel):
    """A deterministic task definition with explicit acceptance scope."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    acceptance_criteria: tuple[str, ...] = ()
    expected_scope: dict[str, str] = Field(default_factory=dict)
    tool_fixture: dict[str, Any] | None = None


class RunResult(BaseModel):
    """Normalized result for one design/task execution."""

    model_config = ConfigDict(extra="forbid")

    design_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    success: bool
    scope_status: str = Field(min_length=1)
    false_success: bool = False
    missed_concerns: int = Field(default=0, ge=0)
    false_positive_concerns: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    duplicate_side_effects: int = Field(default=0, ge=0)
    raw_events_path: str = Field(min_length=1)


def aggregate(results: Iterable[RunResult]) -> dict[str, float | int]:
    """Aggregate results without hiding empty-input or cost semantics."""

    rows = list(results)
    if not rows:
        return {
            "count": 0,
            "success_rate": 0.0,
            "false_success_rate": 0.0,
            "mean_latency_ms": 0.0,
            "mean_input_tokens": 0.0,
            "mean_output_tokens": 0.0,
            "mean_estimated_cost": 0.0,
            "duplicate_side_effects": 0,
        }
    costs = [row.estimated_cost for row in rows if row.estimated_cost is not None]
    return {
        "count": len(rows),
        "success_rate": mean(row.success for row in rows),
        "false_success_rate": mean(row.false_success for row in rows),
        "mean_latency_ms": mean(row.latency_ms for row in rows),
        "mean_input_tokens": mean(row.input_tokens for row in rows),
        "mean_output_tokens": mean(row.output_tokens for row in rows),
        "mean_estimated_cost": mean(costs) if costs else 0.0,
        "duplicate_side_effects": sum(row.duplicate_side_effects for row in rows),
    }
