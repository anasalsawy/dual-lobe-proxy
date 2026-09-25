"""Proxy-owned server-side tools.

All three tools execute inline inside the gated handler during A's turn: the
proxy awaits the result, then A gets exactly one same-turn continuation that
carries every proxy tool result. The proxy exchange never reaches the client
transcript — the client only ever sees the continuation's response.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry

LOG = logging.getLogger("dual_lobe.proxy")

MEMORY_SEARCH = "proxy_memory_search"
DELEGATE = "proxy_delegate"
CONSULT = "proxy_consult"
PROXY_TOOL_NAMES = (MEMORY_SEARCH, DELEGATE, CONSULT)

# Budgets from the frozen design: delegation output <=~300 tokens,
# consultation <=~200 tokens, memory search results <=2600 chars.
DELEGATE_MAX_TOKENS = 300
CONSULT_MAX_TOKENS = 200
MEMORY_SEARCH_LIMIT = 6
MEMORY_SEARCH_BUDGET = 2600

DELEGATE_SYSTEM = (
    "You are Lobe B, the second lobe of a two-lobe inference system, acting as a "
    "WORKER for Lobe A. A has delegated a specific piece of work to you. Do that "
    "work now: reason carefully and produce the deliverable A asked for. Return "
    "ONLY the deliverable (code, analysis, text) in at most ~300 tokens. No "
    "preamble, no addressing the user, no mention of this delegation."
)

CONSULT_SYSTEM = (
    "You are Lobe B, the second lobe of a two-lobe inference system. Lobe A asked "
    "you a short advisory question (e.g. what the next step is, what to do here). "
    "Answer it directly and briefly in at most ~200 tokens: the recommended next "
    "step or decision. Prefer evidence over speculation; say plainly when you do "
    "not know. No preamble, no addressing the user."
)


def proxy_tool_schemas() -> list[dict[str, Any]]:
    """OpenAI function-tool schemas for the three proxy tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": MEMORY_SEARCH,
                "description": (
                    "Search the persistent shared memory store for stored history "
                    "relevant to the current question. Deterministic keyword search; "
                    "returns matching excerpts as text, or 'no match'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Short search phrase, at most 12 words.",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": DELEGATE,
                "description": (
                    "Delegate a self-contained piece of work to Lobe B (the peer "
                    "lobe). B performs the work and returns the deliverable, which "
                    "you then absorb into your own answer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": (
                                "Precise description of the work to perform: enough "
                                "context to complete it standalone."
                            ),
                        }
                    },
                    "required": ["task"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": CONSULT,
                "description": (
                    "Ask Lobe B a short advisory question and wait for the answer "
                    "within this turn. Use for orientation questions like 'what is "
                    "the next step?' or 'what should I do here?' — not for task "
                    "delegation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The advisory question to ask Lobe B.",
                        }
                    },
                    "required": ["question"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def is_proxy_tool(name: Any) -> bool:
    return name in PROXY_TOOL_NAMES


def strip_proxy_calls(message: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an assistant message without proxy tool calls."""
    calls = [tc for tc in (message.get("tool_calls") or [])
             if not is_proxy_tool((tc.get("function") or {}).get("name"))]
    cleaned = dict(message)
    if calls:
        cleaned["tool_calls"] = calls
    else:
        cleaned.pop("tool_calls", None)
    return cleaned


def _transcript(messages: list[dict[str, Any]], max_chars: int = 6000) -> str:
    """Compact conversation text handed to B for delegate/consult calls."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content
                               if isinstance(p, dict))
        text = str(content or "").strip()
        if not text:
            continue
        lines.append(f"[{role}] {text[:600]}")
    out = "\n".join(lines)
    if len(out) <= max_chars:
        return out
    half = max_chars // 2
    return out[:half] + "\n...[middle omitted]...\n" + out[-half:]


async def _call_b_text(system: str, user: str, max_tokens: int) -> str:
    """Plain-text lobe-b call (no JSON contract). Returns stripped content."""
    s = get_settings()
    adapter = get_registry().adapter("lobe-b")
    req = NormalizedRequest(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
        max_tokens=max_tokens,
        stream=False,
        timeout=s.b_timeout,
    )
    response = await adapter.buffered(req)
    data = response_dict(response)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return str(content or "").strip()


async def execute_proxy_call(
    call: dict[str, Any],
    *,
    tenant_id: int,
    space: str | None,
    messages: list[dict[str, Any]],
    used: dict[str, int],
    caps: dict[str, int],
) -> str:
    """Run one proxy tool call inline. Never raises; returns tool result text.

    ``used`` accumulates per-turn counts so the caller can observe caps.
    """
    fn = call.get("function") or {}
    name = str(fn.get("name") or "")
    if not is_proxy_tool(name):
        return f"Unknown proxy tool: {name}"
    cap = caps.get(name, 1)
    if used.get(name, 0) >= cap:
        return (f"Per-turn limit reached for {name} ({cap}). "
                "Continue with the information you already have.")
    used[name] = used.get(name, 0) + 1
    try:
        args = json.loads(str(fn.get("arguments") or "{}"))
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        return f"{name}: invalid arguments ({type(exc).__name__}); continue without it."

    try:
        if name == MEMORY_SEARCH:
            from ..state.memory import search_memory
            query = str(args.get("query") or "").strip()[:200]
            if not query:
                return "proxy_memory_search: empty query; continue without it."
            hits = await search_memory(tenant_id, space, query,
                                       limit=MEMORY_SEARCH_LIMIT,
                                       budget=MEMORY_SEARCH_BUDGET)
            return hits or "No matching stored history."
        if name == DELEGATE:
            task = str(args.get("task") or "").strip()
            if not task:
                return "proxy_delegate: empty task; continue without it."
            user = (f"CONVERSATION CONTEXT:\n{_transcript(messages)}\n\n"
                    f"DELEGATED TASK:\n{task[:3000]}")
            return await _call_b_text(DELEGATE_SYSTEM, user, DELEGATE_MAX_TOKENS) \
                or "Lobe B returned no deliverable; continue without it."
        if name == CONSULT:
            question = str(args.get("question") or "").strip()
            if not question:
                return "proxy_consult: empty question; continue without it."
            user = (f"CONVERSATION CONTEXT:\n{_transcript(messages)}\n\n"
                    f"QUESTION FROM LOBE A:\n{question[:2000]}")
            return await _call_b_text(CONSULT_SYSTEM, user, CONSULT_MAX_TOKENS) \
                or "Lobe B had no advice; continue without it."
        return f"Unknown proxy tool: {name}"
    except Exception as exc:
        LOG.warning("proxy tool %s failed: %s", name, type(exc).__name__)
        return f"{name} failed ({type(exc).__name__}); continue without it."
