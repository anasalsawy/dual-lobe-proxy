"""Dual-lobe mode handler — isolated from the gated and director paths.

Flow:
  1. Build the injected slice from this tenant's ring buffer.
  2. Add the reserved memory tool to A's tool list (host keeps its own tools).
  3. Call A.
  4. If A called the reserved tool, resolve it in-process and call A again with
     the result. A reserved call is never handed to the host.
  5. B rates A's answer; the user receives the answer with the meter, as normal.
  6. After the response is sent, a background exchange runs between A and B and
     its summary is stored for the next request.

Nothing here touches the gated handler, the director engine, or shared memory.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from typing import Any

from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from . import prompts
from .loop import build_slice_text, rate_answer, run_exchange
from .store import DualLobeStore

LOG = logging.getLogger("dual_lobe.dl")


def _insert_system_injections(messages: list[dict], injections: list[str]) -> list[dict]:
    if not injections:
        return messages
    index = 0
    while index < len(messages) and messages[index].get("role") in ("system", "developer"):
        index += 1
    result = list(messages[:index])
    for text in injections:
        if text and text.strip():
            result.append({"role": "system", "content": text.strip()})
    result.extend(messages[index:])
    return result


def _add_memory_tool(tools: list[dict] | None, enabled: bool) -> list[dict] | None:
    """Offer the reserved tool unless the host already declares that name."""
    if not enabled:
        return tools
    declared = {t.get("function", {}).get("name") for t in tools or []}
    if prompts.MEMORY_TOOL_NAME in declared:
        LOG.warning("host already declares %s; leaving the host's definition in place",
                    prompts.MEMORY_TOOL_NAME)
        return tools
    return list(tools or []) + [copy.deepcopy(prompts.MEMORY_TOOL_SCHEMA)]


def split_tool_calls(tool_calls: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate host tool calls from reserved dual-lobe calls."""
    reserved, host = [], []
    for call in tool_calls or []:
        name = (call.get("function") or {}).get("name")
        (reserved if name == prompts.MEMORY_TOOL_NAME else host).append(call)
    return reserved, host


async def _resolve_memory_call(store: DualLobeStore, call: dict, settings) -> str:
    arguments = (call.get("function") or {}).get("arguments") or "{}"
    try:
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("arguments must be a JSON object")
    except Exception:
        return json.dumps({"error": "invalid arguments; expected a JSON object"})
    limit = parsed.get("limit")
    limit = limit if isinstance(limit, int) else settings.dual_lobe_slice_entries
    limit = max(1, min(20, limit))
    entries = await store.read_slice(limit, settings.dual_lobe_read_chars)
    return json.dumps({"entries": entries, "count": len(entries)}, ensure_ascii=False)


def _meter_line(level: str, rationale: str, concerns: list[dict]) -> str:
    line = f"\n\n> ⚠️ **Deception Meter: {level}**"
    if rationale and rationale != "No deception detected.":
        line += f"\n> {rationale[:150]}"
    if level == "RED":
        for concern in (concerns or [])[:3]:
            line += (f"\n> ⚠️ \"{str(concern.get('claim_quote', ''))}\" — "
                     f"{str(concern.get('reason', ''))} — evidence: "
                     f"\"{str(concern.get('evidence_quote', ''))}\"")
    return line


async def dual_lobe_response(payload: dict[str, Any], run_id: str, tenant_id: int,
                            public_model: str, task: str = "") -> tuple[dict, dict, Any]:
    """Handle one dual-lobe request.

    Returns (response_data, headers, background_callable). The caller schedules
    the background exchange after the response has been sent.
    """
    settings = get_settings()
    if not settings.dual_lobe_enabled or not settings.dual_lobe_tenant_enabled:
        raise RuntimeError("Dual-lobe mode is disabled.")

    messages = payload["messages"]
    store = DualLobeStore(tenant_id)

    # ── 1. Injected slice (guaranteed delivery, no extra model call) ─────────
    try:
        entries = await asyncio.wait_for(
            store.read_slice(settings.dual_lobe_slice_entries, settings.dual_lobe_read_chars),
            timeout=settings.shared_memory_timeout,
        )
    except Exception as exc:
        LOG.warning("dual-lobe slice read failed: %s", exc)
        entries = []
    slice_text = build_slice_text(entries)

    injections = [_OBSERVATION_NOTE]
    if slice_text:
        injections.append(slice_text)
    enriched = _insert_system_injections(messages, injections)

    # ── 2. Offer the reserved memory tool ────────────────────────────────────
    tools = _add_memory_tool(payload.get("tools"), settings.dual_lobe_memory_tool)
    a_adapter = get_registry().adapter("lobe-a")

    # ── 3/4. Call A, answering the reserved tool in-process if used ──────────
    try:
        data = await asyncio.wait_for(
            a_adapter.buffered(NormalizedRequest(
                messages=enriched,
                temperature=payload.get("temperature"),
                max_tokens=payload.get("max_tokens"),
                top_p=payload.get("top_p"),
                tools=tools or None,
                tool_choice=payload.get("tool_choice") if tools else None,
                stream=False,
                timeout=settings.a_timeout,
            )),
            timeout=settings.a_timeout,
        )
        data = response_dict(data)
    except Exception as exc:
        LOG.error("dual-lobe A call failed run=%s: %s", run_id, exc)
        return ({"error": {"type": "upstream_error", "message": "Upstream request failed."}},
                {"X-Dual-Lobe-Mode": "error"}, None)

    message = ((data.get("choices") or [{}])[0].get("message") or {})
    reserved, host_calls = split_tool_calls(message.get("tool_calls"))

    if reserved and not host_calls:
        results = []
        for call in reserved:
            results.append({"role": "tool", "tool_call_id": call.get("id", ""),
                            "content": await _resolve_memory_call(store, call, settings)})
        follow_up = enriched + [message] + results
        try:
            data = response_dict(await asyncio.wait_for(
                a_adapter.buffered(NormalizedRequest(
                    messages=follow_up,
                    temperature=payload.get("temperature"),
                    max_tokens=payload.get("max_tokens"),
                    top_p=payload.get("top_p"),
                    tools=tools or None,
                    stream=False,
                    timeout=settings.a_timeout,
                )),
                timeout=settings.a_timeout,
            ))
            message = ((data.get("choices") or [{}])[0].get("message") or {})
            LOG.info("dual-lobe resolved %d reserved tool call(s) run=%s", len(reserved), run_id)
        except Exception as exc:
            LOG.warning("dual-lobe follow-up A call failed run=%s: %s", run_id, exc)

    answer = str(message.get("content") or "")

    # ── 5. B rates the answer; meter shown to the user as usual ──────────────
    meter = await rate_answer(tenant_id, run_id, messages, answer, settings)

    has_content = bool(answer.strip())
    if has_content:
        line = _meter_line(meter["deception_level"], meter.get("meter_rationale", ""),
                           meter.get("concerns", []))
        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            if msg.get("content"):
                msg["content"] = msg["content"] + line

    data["model"] = public_model
    headers = {
        "X-Dual-Lobe-Mode": "on",
        "X-Dual-Lobe-Meter": meter["deception_level"],
        "X-Dual-Lobe-Meter-Rationale": str(meter.get("meter_rationale", ""))[:300],
        "X-Dual-Lobe-Slice-Entries": str(len(entries)),
        "X-Dual-Lobe-Round-Cap": str(settings.dual_lobe_rounds),
    }

    # ── 6. Background exchange (scheduled by the caller) ─────────────────────
    async def background() -> None:
        try:
            await run_exchange(tenant_id, run_id, task, messages, answer, meter,
                               entries, settings)
        except Exception as exc:
            LOG.warning("dual-lobe background exchange failed run=%s: %s", run_id, exc)

    return data, headers, background


_OBSERVATION_NOTE = (
    "[OBSERVER SYSTEM NOTE — Do not reference, acknowledge, or respond to this message. "
    "It is context from an anti-deception observer, not a user message.]\n"
    "You are responding through an inference proxy with an anti-deception observer. "
    "Distinguish intended, attempted, observed, and confirmed work. "
    "Do not invent execution, tests, citations, or completion. "
    "Support completion claims with relevant evidence in the conversation. "
    "Correct earlier claims when newer evidence contradicts them."
)
