"""Async Chat Completions transport; no sync iterator on the event loop."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any, AsyncIterator

import httpx

CHAT_COMPLETIONS = "chat_completions"
RESPONSES = "responses"
BLOCKED_UNIFIED_KWARGS = {"api_base", "api_key", "base_url", "custom_llm_provider"}
_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # One connection pool per process. No implicit retries or redirects.
        _client = httpx.AsyncClient(follow_redirects=False)
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@dataclass
class ProviderTarget:
    alias: str
    base_url: str
    api_key: str
    model: str
    kind: str = CHAT_COMPLETIONS
    capabilities: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def supports(self, feature: str) -> bool:
        return self.enabled and bool(self.capabilities.get(feature, True))


@dataclass
class NormalizedRequest:
    messages: list[dict[str, Any]]
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    top_p: float | None = None
    stop: Any = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    response_format: Any = None
    seed: Any = None
    timeout: float | None = None
    reasoning_effort: Any = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None

    def to_kwargs(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)
                if f.name != "stream" and getattr(self, f.name) is not None
                and (f.name != "stream_options" or self.stream)}


def response_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return dict(response)
    return response.model_dump(exclude_none=True)


class ChatCompletionsAdapter:
    dialect = CHAT_COMPLETIONS

    def __init__(self, target: ProviderTarget) -> None:
        self.target = target

    def _base_kwargs(self, req: NormalizedRequest) -> dict[str, Any]:
        body = req.to_kwargs()
        body.pop("timeout", None)
        return {"model": self.target.model, **body}

    def _endpoint(self) -> str:
        return self.target.base_url.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.target.api_key}"}

    async def buffered(self, req: NormalizedRequest):
        response = await get_http_client().post(
            self._endpoint(), headers=self._headers(),
            json={**self._base_kwargs(req), "stream": False}, timeout=req.timeout,
        )
        response.raise_for_status()
        return response.json()

    async def stream(self, req: NormalizedRequest) -> AsyncIterator[dict[str, Any]]:
        async with get_http_client().stream(
            "POST", self._endpoint(), headers=self._headers(),
            json={**self._base_kwargs(req), "stream": True}, timeout=req.timeout,
        ) as response:
            response.raise_for_status()
            data_lines: list[str] = []
            event_size = 0
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    value = line[5:].lstrip(" ")
                    event_size += len(value)
                    if event_size > 2 * 1024 * 1024:
                        raise ValueError("upstream SSE event exceeds size limit")
                    data_lines.append(value)
                elif not line and data_lines:
                    data = "\n".join(data_lines)
                    data_lines, event_size = [], 0
                    if data.strip() == "[DONE]":
                        return
                    chunk = json.loads(data)
                    if not isinstance(chunk, dict) or "error" in chunk:
                        raise ValueError("invalid upstream SSE chunk")
                    # Forward every choice/tool fragment without reconstruction.
                    yield chunk
            if data_lines:
                raise ValueError("upstream SSE ended in a partial event")


class ResponsesAdapter:
    dialect = RESPONSES

    def __init__(self, target: ProviderTarget) -> None:
        self.target = target

    async def buffered(self, req: NormalizedRequest):
        raise NotImplementedError("Responses dialect is not implemented; use chat_completions")

    async def stream(self, req: NormalizedRequest):
        raise NotImplementedError("Responses dialect is not implemented; use chat_completions")
        yield  # async generator contract


def make_adapter(target: ProviderTarget):
    if target.kind == RESPONSES:
        return ResponsesAdapter(target)
    if target.kind != CHAT_COMPLETIONS:
        raise ValueError(f"unsupported provider dialect: {target.kind}")
    return ChatCompletionsAdapter(target)


def resolve_request(payload: dict[str, Any]) -> NormalizedRequest:
    allowed = {f.name for f in fields(NormalizedRequest)}
    return NormalizedRequest(**{k: v for k, v in payload.items() if k in allowed})
