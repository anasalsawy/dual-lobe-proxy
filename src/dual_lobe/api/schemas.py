"""Pydantic wire contracts (OpenAI-compatible + dual-lobe extension endpoints)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict

EventKind = str


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "lobe-a"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: Any = None
    tools: list[dict[str, Any]] | None = None
    # Optional host-runtime artifact inventory. This extension is consumed by
    # B only and is never forwarded to the upstream A provider.
    artifacts: list[dict[str, Any]] | None = Field(default=None, max_length=256)
    tool_choice: Any = None
    response_format: Any = None
    seed: int | None = None
    reasoning_effort: Any = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    parallel_tool_calls: bool | None = None
    stream_options: dict[str, Any] | None = None
    max_completion_tokens: int | None = None


class EventIngest(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    run_id: str | None = None
    actor: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=256)


class VerifyRequest(BaseModel):
    claim_id: str
    check: str = "file_exists"
    artifact: str
    needle: str = ""
    cwd: str = "."


class StateResponse(BaseModel):
    run_id: str
    revision: int | None
    pulse: str | None
    payload: dict[str, Any] | None
    claims: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    ledger: str
    registry: str
