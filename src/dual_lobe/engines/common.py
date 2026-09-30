"""Shared runtime for the ported engines: trace, JSON parsing, and an agent tool loop.

``run_agent`` replaces a CrewAI Agent + Task kickoff: one system prompt, one task
prompt, native OpenAI function calling for tools, looped until the model answers
without calling a tool. Every model request goes through the registry adapter,
so it rides the round-robin hub.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel

from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry

T = TypeVar("T", bound=BaseModel)


@dataclass
class ProxyToolEvent:
    name: str
    input_text: str
    output_text: str
    provenance: str
    ts: float = field(default_factory=time.time)


class ProxyToolTrace:
    def __init__(self):
        self.events: list[ProxyToolEvent] = []
        self._lock = threading.Lock()

    def add(self, name: str, *, input_text: str = "", output_text: str = "", provenance: str = "") -> None:
        with self._lock:
            self.events.append(ProxyToolEvent(name, str(input_text or ""), str(output_text or ""), str(provenance or "")))

    def snapshot_from(self, cursor: int = 0) -> list[ProxyToolEvent]:
        with self._lock:
            return list(self.events[max(0, cursor):])

    def render(self, max_chars_per_event: int | None = None) -> str:
        with self._lock:
            events = list(self.events)
        rows = []
        for i, event in enumerate(events, 1):
            output = event.output_text
            if max_chars_per_event is not None and len(output) > max_chars_per_event:
                original = len(output)
                output = output[:max_chars_per_event] + f"\n[TRUNCATED_BY_RENDER: original_chars={original}]"
            rows.append(
                f"[{i}] tool={event.name}\nprovenance={event.provenance}\n"
                f"input={event.input_text}\noutput={output}"
            )
        return "\n\n".join(rows)


def _balanced_objects(text: str):
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


def extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        pass
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.I | re.S):
        for candidate in _balanced_objects(m.group(1)):
            try:
                value = json.loads(candidate)
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
    for candidate in _balanced_objects(text):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except Exception:
            continue
    return {}


def parse_model(text: str, model: type[T], fallback: T) -> T:
    data = extract_json_object(text)
    if not data:
        return fallback
    try:
        return model.model_validate(data)
    except Exception:
        return fallback


@dataclass
class Tool:
    """One callable tool: OpenAI JSON schema for its arguments plus the function."""

    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}

    async def call(self, arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except Exception as exc:
            return f"TOOL_ARGUMENTS_INVALID: {type(exc).__name__}: {exc}"
        try:
            result = self.fn(**args)
            if inspect.isawaitable(result):
                result = await result
            return str(result)
        except TypeError as exc:
            return f"TOOL_ARGUMENTS_INVALID: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"TOOL_FAILED: {type(exc).__name__}: {exc}"


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required or [])}


@dataclass
class AgentPause:
    """The agent called client-side tools: the client must run them, then the
    conversation resumes from ``messages`` with one tool message per call."""

    messages: list[dict[str, Any]]
    calls: list[dict[str, Any]]


def client_tool_names(client_tools: list[dict[str, Any]] | None) -> set[str]:
    return {str((t.get("function") or {}).get("name")) for t in (client_tools or [])
            if isinstance(t, dict) and (t.get("function") or {}).get("name")}


async def run_agent(
    *,
    alias: str,
    system: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
    tools: list[Tool] | None = None,
    max_steps: int = 12,
    client_tools: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> str | AgentPause:
    """One agent turn: call the model, run any tool calls, repeat until it answers.

    Server tools (``tools``) run here. Client tools (OpenAI specs the caller's
    system executes) pause the loop: an AgentPause is returned and the caller
    resumes later with ``history`` = the paused messages plus the tool results.
    """
    adapter = get_registry().adapter(alias)
    messages: list[dict[str, Any]] = list(history) if history else [
        {"role": "system", "content": system}, {"role": "user", "content": prompt}]
    by_name = {t.name: t for t in (tools or [])}
    client_names = client_tool_names(client_tools) - set(by_name)
    specs = ([t.spec() for t in by_name.values()]
             + [t for t in (client_tools or []) if (t.get("function") or {}).get("name") in client_names]) or None
    deadline = time.monotonic() + timeout
    message: dict[str, Any] = {}
    for step in range(max_steps):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{alias} agent exceeded {timeout:.0f}s")
        last = step == max_steps - 1
        req = NormalizedRequest(messages=messages, max_tokens=max_tokens, timeout=remaining,
                                tools=None if last else specs,
                                tool_choice=None if (last or not specs) else "auto")
        data = response_dict(await asyncio.wait_for(adapter.buffered(req), timeout=remaining))
        message = ((data.get("choices") or [{}])[0].get("message") or {})
        calls = message.get("tool_calls") or []
        if not calls:
            return str(message.get("content") or "")
        for call in calls:
            # The client matches results by id, so every call needs a unique one.
            if not call.get("id"):
                call["id"] = f"call_{uuid.uuid4().hex[:24]}"
            call.setdefault("type", "function")
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        pending = []
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            if name in client_names:
                pending.append(call)
                continue
            tool = by_name.get(name)
            output = (await tool.call(fn.get("arguments") or "{}")) if tool else f"UNKNOWN_TOOL: {name}"
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})
        if pending:
            return AgentPause(messages=messages, calls=pending)
    return str(message.get("content") or "")


async def stream_text(
    *,
    alias: str,
    system: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
    on_delta: Callable[[str], Awaitable[None]],
) -> str:
    """One tool-less model call whose text is passed to ``on_delta`` as it arrives."""
    adapter = get_registry().adapter(alias)
    req = NormalizedRequest(messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                            max_tokens=max_tokens, timeout=timeout, stream=True)
    parts: list[str] = []
    async with asyncio.timeout(timeout):
        async for chunk in adapter.stream(req):
            for choice in chunk.get("choices") or []:
                text = (choice.get("delta") or {}).get("content")
                if text:
                    parts.append(text)
                    await on_delta(text)
    return "".join(parts)


def spawn(coro: Awaitable[Any]) -> asyncio.Task:
    return asyncio.ensure_future(coro)
