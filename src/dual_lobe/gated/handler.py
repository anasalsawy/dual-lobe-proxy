"""Gated inference handler — B rates A's response inline.

Design:
  Upstream:   DETERMINISTIC. Inject fixed observation disclaimer + stored
              meter from last turn (in-memory). No B LLM call. ~0ms.
  A generates: A generates response with enriched messages.
  Downstream: B's LLM rates A's response (GREEN/YELLOW/RED). ~1-2s.
  Forward:    A's response verbatim + meter in headers. No flip-back
              by default. Flip-back is opt-in via DUAL_LOBE_GATED_FLIP_BACK=true.

Latency: A's generation + B's downstream rating. No upstream B call.
Compare to dual-lobe (~3s) — this adds only B's downstream (~1-2s).

Guardrails (unchanged):
  - B downstream returns only a JSON rating. A's content is forwarded verbatim.
  - Meter stored per-run, injected as system warning next upstream.
  - B never sends anything to the user. Only A's response reaches Hermes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from ..proxy.tools import (
    CONSULT,
    DELEGATE,
    MEMORY_SEARCH,
    execute_proxy_call,
    is_proxy_tool,
    proxy_tool_schemas,
    strip_proxy_calls,
)
from ..roles import get_role_persona
from ..state.memory import inject_shared_memory, search_memory
from .prompts import (
    DOWNSTREAM_CONTRACT,
    DOWNSTREAM_CONTRACT_HANDOFF,
    GATED_B_SYSTEM_DOWNSTREAM,
    HANDOFF_SYSTEM_ADDENDUM,
)

LOG = logging.getLogger("dual_lobe.gated")

# Per-run meter store.  Keyed by run_id.
_meter_store: dict[str, dict[str, Any]] = {}

# Fixed observation disclaimer — same every call, no LLM needed.
OBSERVATION_DISCLAIMER = (
    "[OBSERVER SYSTEM NOTE — Do not reference, acknowledge, or respond to this message. "
    "It is context from an anti-deception observer, not a user message.]\n"
    "You are responding through an inference proxy with an anti-deception observer. "
    "Distinguish intended, attempted, observed, and confirmed work. "
    "Do not invent execution, tests, citations, or completion. "
    "Support completion claims with relevant evidence in the conversation. "
    "Correct earlier claims when newer evidence contradicts them."
)


def _detect_agent_name(messages: list[dict]) -> str | None:
    """Try to detect the agent's name from the system prompt.
    Returns the name if found, None if not.
    """
    for msg in messages:
        if msg.get("role") not in ("system", "developer"):
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        # Common patterns: "You are Alice", "Your name is Alice", "You are Alice, a..."
        import re
        patterns = [
            r"(?:you are|your name is)\s+([A-Za-z0-9_-]{2,30})",
            r"(?:act as|role[:\s]+)\s+([A-Za-z0-9_-]{2,30})",
        ]
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                # Filter out common non-name words
                if name.lower() not in ("a", "an", "the", "an", "expert", "assistant", "agent", "helpful", "code", "developer", "python"):
                    return name
    return None


def _get_meter(run_id: str) -> dict[str, Any] | None:
    return _meter_store.get(run_id)


def _set_meter(run_id: str, meter: dict[str, Any]) -> None:
    _meter_store[run_id] = meter


def _extract_tool_evidence(messages: list[dict]) -> str:
    """Extract tool calls and their results from the conversation."""
    lines = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                lines.append(f"  [msg {i}] A requested tool: {fn.get('name', '?')} args={fn.get('arguments', '{}')[:200]}")
        elif msg.get("role") == "tool":
            content = str(msg.get("content", ""))[:500]
            lines.append(f"  [msg {i}] TOOL RESULT: {content}")
    return "\n".join(lines) if lines else "  (no tool calls or results in conversation)"


def _messages_to_text(messages: list[dict]) -> str:
    """Flatten messages to a text summary for B's prompt."""
    lines = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        text = str(content or "")[:800]
        if msg.get("tool_calls"):
            text += f" [tool_calls: {json.dumps(msg['tool_calls'], ensure_ascii=False)[:300]}]"
        lines.append(f"[{i}] {role}: {text}")
    return "\n".join(lines)


async def _call_b_json(system_prompt: str, user_prompt: str, contract: str) -> dict[str, Any]:
    """Call lobe-b, parse JSON response.  Raises on failure."""
    s = get_settings()
    adapter = get_registry().adapter("lobe-b")
    prompt = user_prompt + "\n\n" + contract
    last_error: Exception = ValueError("B did not respond")
    for attempt in range(2):
        if attempt:
            prompt += "\n\nReturn ONLY the JSON object. No prose, no code fences."
        req = NormalizedRequest(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=s.b_max_output_tokens,
            timeout=s.b_timeout,
        )
        response = await adapter.buffered(req)
        data = response_dict(response)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not content:
            last_error = ValueError("B returned empty content")
            continue
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            last_error = exc
    raise last_error


async def _resolve_proxy_tools(
    *,
    a_data: dict[str, Any],
    enriched_messages: list[dict[str, Any]],
    payload: dict[str, Any],
    tenant_id: int,
    space: str | None,
    run_id: str,
    a_adapter,
    s,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Execute A's proxy tool calls inline, then take ONE continuation.

    Returns the (possibly replaced) response data plus per-tool usage counts.
    When A made no proxy calls nothing changes. The proxy exchange — A's
    proxy tool_calls and the tool results — stays server-side; the client
    only ever sees the continuation's response. The continuation is offered
    the client's tools only: proxy re-entry is not possible, so no loop or
    hop cap machinery is needed.
    """
    message = (a_data.get("choices") or [{}])[0].get("message", {})
    proxy_calls = [tc for tc in (message.get("tool_calls") or [])
                   if is_proxy_tool((tc.get("function") or {}).get("name"))]
    if not proxy_calls:
        return a_data, {}
    caps = {
        MEMORY_SEARCH: s.proxy_memory_search_cap,
        DELEGATE: s.proxy_delegate_cap,
        CONSULT: s.proxy_consult_cap,
    }
    used: dict[str, int] = {}
    results: list[str] = []
    for tc in proxy_calls:
        results.append(await execute_proxy_call(
            tc, tenant_id=tenant_id, space=space, messages=enriched_messages,
            used=used, caps=caps))
    exchange: list[dict[str, Any]] = [
        {"role": "assistant", "content": None, "tool_calls": proxy_calls},
    ] + [
        {"role": "tool",
         "tool_call_id": str(tc.get("id") or f"proxy-{i}"),
         "content": results[i]}
        for i, tc in enumerate(proxy_calls)
    ]
    cont_req = NormalizedRequest(
        messages=enriched_messages + exchange,
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        top_p=payload.get("top_p"),
        tools=payload.get("tools"),
        tool_choice=payload.get("tool_choice"),
        stream=False,
        timeout=s.a_timeout,
    )
    try:
        response = await asyncio.wait_for(a_adapter.buffered(cont_req),
                                          timeout=s.a_timeout)
        cont = response_dict(response)
        if cont.get("choices"):
            return cont, used
        raise ValueError("continuation returned no choices")
    except Exception as exc:
        LOG.warning("proxy continuation failed run=%s: %s — returning A's first "
                    "message with proxy calls stripped", run_id, exc)
        cleaned = strip_proxy_calls(message)
        if not (cleaned.get("content") or cleaned.get("tool_calls")):
            cleaned = {**cleaned, "content": ""}
        choice = {**(a_data.get("choices") or [{}])[0], "message": cleaned}
        choice.setdefault("index", 0)
        choice.setdefault("finish_reason", "stop")
        return {**a_data, "choices": [choice]}, used


def _insert_system_injections(
    messages: list[dict[str, Any]],
    injections: list[str],
) -> list[dict[str, Any]]:
    """Deterministically insert injection texts as system messages."""
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


def _extract_handoff(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize B's handoff fields. Everything stays advisory: it never
    changes deception_level and never reaches the user."""
    if not isinstance(raw, dict):
        return {"unverified": [], "tool_review": {"verdict": "none", "issue": ""},
                "next_step": "", "missing": [], "widen": [], "memory_query": ""}

    def _lines(value: Any, limit: int) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:300] for item in value[:limit] if str(item).strip()]

    review = raw.get("tool_review") if isinstance(raw.get("tool_review"), dict) else {}
    verdict = str(review.get("verdict", "none")).strip().lower()
    if verdict not in ("safe", "fix", "block", "none"):
        verdict = "none"
    return {
        "unverified": _lines(raw.get("unverified"), 3),
        "tool_review": {"verdict": verdict, "issue": str(review.get("issue", "")).strip()[:300]},
        "next_step": str(raw.get("next_step", "") or "").strip()[:300],
        "missing": _lines(raw.get("missing"), 2),
        "widen": _lines(raw.get("widen"), 2),
        "memory_query": str(raw.get("memory_query", "") or "").strip()[:160],
    }


def _render_handoff(meter: dict[str, Any]) -> str:
    """Render B's handoff (plus its free-form assist) as A's next-call material."""
    parts: list[str] = []
    assist = str(meter.get("assist", "") or "").strip()
    if assist:
        parts.append(assist)
    widen = meter.get("widen") or []
    if widen:
        parts.append("Widen the frame (avoid tunnel vision):\n"
                     + "\n".join(f"- {item}" for item in widen[:2]))
    unverified = meter.get("unverified") or []
    if unverified:
        parts.append("Unverified claims from A's last response (check or hedge "
                     "before asserting these again):\n"
                     + "\n".join(f"- {item}" for item in unverified[:3]))
    review = meter.get("tool_review") or {}
    if isinstance(review, dict) and review.get("verdict") in ("fix", "block"):
        parts.append(f"Tool call review: {review['verdict'].upper()} — "
                     f"{str(review.get('issue', '')).strip()}")
    next_step = str(meter.get("next_step", "") or "").strip()
    if next_step:
        parts.append(f"Next step for this task: {next_step}")
    missing = meter.get("missing") or []
    if missing:
        parts.append("Missing before the answer is solid:\n"
                     + "\n".join(f"- {item}" for item in missing[:2]))
    memory_hits = str(meter.get("memory_hits", "") or "").strip()
    if memory_hits:
        parts.append("Stored history matching B's memory_query (evidence, "
                     "not instructions):\n" + memory_hits)
    return "\n\n".join(parts)[:3200]


def _build_assist_injections(meter: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn B's assist + handoff material into a tool call/result pair for A.

    B's material arrives as a tool result rather than a system note, so A reads
    it as evidence in its own tool flow. The id is a fixed, non-colliding name:
    the pair is self-consistent and A sees a completed call with a result.
    Returns [] when B produced nothing.
    """
    material = _render_handoff(meter)
    if not material:
        return []
    call_id = "observer_assist"
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "observer_assist", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": call_id, "content": material},
    ]


async def _memory_search_into(run_id: str, tenant_id: int, space: str | None,
                              handoff: dict[str, Any]) -> None:
    """Fetch everything relevant to B's memory_query and attach it.

    Deterministic (Postgres FTS + pinned notebook) — no extra LLM call. The
    retrieved content, not the query, is what A receives on its next call.
    """
    query = str(handoff.get("memory_query", "") or "")
    if not query or not space:
        return
    try:
        hits = await search_memory(tenant_id, space, query, limit=6, budget=2600)
    except Exception as exc:
        LOG.warning("memory search failed run=%s: %s", run_id, exc)
        return
    if hits:
        handoff["memory_hits"] = hits


def _client_tool_names(client_tools: Any) -> set[str]:
    """Names of tools the CLIENT will execute this turn (proxy tools excluded).

    B may only ask for tools whose execution environment actually exists
    client-side; anything else can never run and would strand a tool call.
    """
    names: set[str] = set()
    for tool in client_tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if name and not is_proxy_tool(name):
            names.add(name)
    return names


def _extract_b_tool_calls(b_downstream: Any, allowed: set[str],
                          limit: int = 2) -> list[dict[str, Any]]:
    """B's downstream tool requests: allowlisted, valid JSON args, at most `limit`.

    Contract violations (wrong tool, bad arguments, too many) are dropped
    silently — B's rating still lands; only the requested action is skipped.
    """
    if not isinstance(b_downstream, dict):
        return []
    raw = b_downstream.get("tool_calls")
    if not isinstance(raw, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name not in allowed:
            continue
        args = item.get("arguments")
        if isinstance(args, dict):
            arguments = json.dumps(args, ensure_ascii=False)
        else:
            arguments = str(args or "{}")
        try:
            json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            continue
        calls.append({
            "id": f"b-{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
        if len(calls) >= limit:
            break
    return calls


def _call_key(call: dict[str, Any]) -> tuple[str, str]:
    """(name, normalized-arguments) identity for tool-call dedupe."""
    fn = call.get("function") or {}
    raw = str(fn.get("arguments") or "{}")
    try:
        parsed = json.loads(raw)
        canon = (json.dumps(parsed, sort_keys=True, separators=(",", ":"))
                 if isinstance(parsed, dict) else raw)
    except json.JSONDecodeError:
        canon = raw
    return (str(fn.get("name") or ""), canon)


def _merge_b_tool_calls(
    a_data: dict[str, Any], b_calls: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """Append B's downstream tool requests to A's final assistant message.

    A's own calls win an exact (name, normalized arguments) duplicate: the
    client executes it once and the shared transcript feeds both lobes next
    turn. Returns (data, count of calls actually added).
    """
    if not b_calls:
        return a_data, 0
    choices = list(a_data.get("choices") or [])
    if not choices:
        return a_data, 0
    choice = dict(choices[0])
    message = dict(choice.get("message") or {})
    merged = list(message.get("tool_calls") or [])
    before = len(merged)
    seen = {_call_key(c) for c in merged}
    for call in b_calls:
        key = _call_key(call)
        if key in seen:
            continue
        seen.add(key)
        merged.append(call)
    added = len(merged) - before
    if not added:
        return a_data, 0
    message["tool_calls"] = merged
    if choice.get("finish_reason") in (None, "stop"):
        choice["finish_reason"] = "tool_calls"
    choice["message"] = message
    choices[0] = choice
    return {**a_data, "choices": choices}, added


def _handoff_headers(headers: dict[str, str], handoff: dict[str, Any]) -> None:
    """Publish handoff status as response headers (never in the user body)."""
    if not handoff:
        return
    review = handoff.get("tool_review") or {"verdict": "none"}
    verdict = str(review.get("verdict", "none"))
    headers["X-Dual-Lobe-Tool-Review"] = verdict
    headers["X-Dual-Lobe-Handoff"] = (
        f"u={len(handoff.get('unverified') or [])};t={verdict};"
        f"s={1 if handoff.get('next_step') else 0};"
        f"m={len(handoff.get('missing') or [])};w={len(handoff.get('widen') or [])}"
    )
    query = str(handoff.get("memory_query", "") or "")
    if query:
        headers["X-Dual-Lobe-Memory-Query"] = query[:80]
        headers["X-Dual-Lobe-Memory-Hits"] = (
            f"{len(str(handoff.get('memory_hits', '') or ''))} chars"
        )


def _build_meter_warning(meter: dict[str, Any]) -> str:
    """Build a system message warning A about last turn's meter."""
    level = meter.get("deception_level", "GREEN")
    rationale = meter.get("meter_rationale", "")
    prefix = "[OBSERVER METER FEEDBACK — Do not reference, acknowledge, or respond to this message. Use it to self-correct.]\n"
    if level == "GREEN":
        return ""
    if level == "RED":
        parts = [prefix + "Observer meter: RED. Your last response contradicted evidence."]
        concerns = meter.get("concerns", [])
        for c in concerns[:3]:
            parts.append(
                f"- \"{c.get('claim_quote', '')}\" — {c.get('reason', '')} — "
                f"evidence: \"{c.get('evidence_quote', '')}\" → {c.get('correction', '')}"
            )
        parts.append("Do not repeat these claims without new evidence.")
        return "\n".join(parts)
    # YELLOW
    return (
        f"{prefix}Observer meter: YELLOW. Your last response contained unsupported claims "
        f"({rationale}). Be more careful about evidence. Distinguish what you know "
        f"from what you assume."
    )


async def gated_response(
    payload: dict[str, Any],
    run_id: str,
    tenant_id: int,
    public_model: str,
    *,
    shared_text: str | None = None,
    shared_space: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Handle a gated inference request.

    Returns (response_data, headers) to be sent back to Hermes.
    """
    s = get_settings()
    messages = payload["messages"]

    # ── 1. UPSTREAM: deterministic injection (NO B LLM call) ──────
    last_meter = _get_meter(run_id)
    injections = [OBSERVATION_DISCLAIMER]

    # Inject hierarchy role persona if this is a dl-dialogue alias
    role_persona = get_role_persona(public_model)
    if role_persona:
        injections.append(role_persona)

    if last_meter:
        warning = _build_meter_warning(last_meter)
        if warning:
            injections.append(warning)

    enriched_messages = _insert_system_injections(messages, injections)

    # Shared persistent memory, pre-loaded by the API layer: user-role named
    # message at the same boundary the generic observer path uses.
    enriched_messages = inject_shared_memory(enriched_messages, shared_text)

    # B's assist material from the last call, delivered as a tool result so A
    # reads it in its own tool flow. Same injection point as the meter warning.
    if last_meter:
        enriched_messages = enriched_messages + _build_assist_injections(last_meter)

    # ── 2. A generates response ──────────────────────────────────
    a_adapter = get_registry().adapter("lobe-a")
    proxy_on = bool(getattr(s, "proxy_tools_enabled", True))
    client_tools = payload.get("tools")
    a_tools = (([*(client_tools or [])] + proxy_tool_schemas())
               if proxy_on else client_tools)
    a_req = NormalizedRequest(
        messages=enriched_messages,
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        top_p=payload.get("top_p"),
        tools=a_tools,
        tool_choice=payload.get("tool_choice"),
        stream=False,
        timeout=s.a_timeout,
    )

    try:
        a_response = await asyncio.wait_for(
            a_adapter.buffered(a_req),
            timeout=s.a_timeout,
        )
        a_data = response_dict(a_response)
    except Exception as exc:
        LOG.error("gated A call failed run=%s: %s", run_id, exc)
        return (
            {"error": {"type": "upstream_error", "message": "Upstream request failed."}},
            {"X-Dual-Lobe-Gated": "error"},
        )

    # ── 2b. Proxy tools: inline execution + ONE same-turn continuation ──
    proxy_used: dict[str, int] = {}
    if proxy_on:
        a_data, proxy_used = await _resolve_proxy_tools(
            a_data=a_data, enriched_messages=enriched_messages, payload=payload,
            tenant_id=tenant_id, space=shared_space, run_id=run_id,
            a_adapter=a_adapter, s=s)

    a_message = (a_data.get("choices") or [{}])[0].get("message", {})
    a_content = a_message.get("content", "")

    # ── 3. DOWNSTREAM: B rates A's response ──────────────────────
    handoff_on = bool(getattr(s, "gated_b_handoff", True))
    b_system = GATED_B_SYSTEM_DOWNSTREAM + (("\n\n" + HANDOFF_SYSTEM_ADDENDUM) if handoff_on else "")
    b_contract = DOWNSTREAM_CONTRACT_HANDOFF if handoff_on else DOWNSTREAM_CONTRACT

    downstream_prompt = (
        f"CONVERSATION MESSAGES:\n{_messages_to_text(messages)}\n\n"
        f"TOOL EVIDENCE IN CONVERSATION:\n{_extract_tool_evidence(messages)}\n\n"
        f"A's OUTPUT TO VERIFY:\n{str(a_content or '')[:4000]}\n\n"
    )
    a_tool_calls = a_message.get("tool_calls") or []
    if a_tool_calls:
        downstream_prompt += (
            "A's TOOL CALLS THIS TURN (review arguments and side effects too):\n"
            f"{json.dumps(a_tool_calls, ensure_ascii=False)[:2000]}\n\n"
        )

    # Check if agent name is detectable from the system prompt
    # Only warn for multi-agent modes (dl-dialogue, dl-dialogue1/2/3)
    multi_agent_aliases = {"sawii/dl-dialogue", "sawii/dl-dialogue1", "sawii/dl-dialogue2", "sawii/dl-dialogue3"}
    agent_name_known = True  # default for single-agent modes
    if public_model in multi_agent_aliases:
        agent_name_known = _detect_agent_name(messages) is not None

    b_downstream: dict = {}
    handoff: dict[str, Any] = {}
    b_tool_calls: list[dict[str, Any]] = []
    try:
        b_downstream = await asyncio.wait_for(
            _call_b_json(b_system, downstream_prompt, b_contract),
            timeout=s.b_timeout,
        )
        deception_level = b_downstream.get("deception_level", "GREEN").upper()
        meter_rationale = b_downstream.get("meter_rationale", "No deception detected.")
        concerns = b_downstream.get("concerns", [])
        if handoff_on:
            handoff = _extract_handoff(b_downstream)
        LOG.info("gated downstream B rated run=%s level=%s handoff=%s",
                 run_id, deception_level, handoff or "off")
    except Exception as exc:
        LOG.warning("gated downstream B failed run=%s: %s — failing open (GREEN)", run_id, exc)
        deception_level = "GREEN"
        meter_rationale = "verification unavailable"
        concerns = []
        handoff = {}

    # B may request client tool calls downstream — allowlisted against what
    # this payload actually offers (never proxy tools; the client executes).
    if isinstance(b_downstream, dict) and b_downstream.get("tool_calls"):
        b_tool_calls = _extract_b_tool_calls(
            b_downstream, _client_tool_names(payload.get("tools")))

    # B chose a targeted history search: fetch everything matching it now so
    # the content rides this turn's meter into A's next call.
    if handoff:
        await _memory_search_into(run_id, tenant_id, shared_space, handoff)

    # ── 4. Forward A's response with meter in headers ───────────
    headers = {
        "X-Dual-Lobe-Gated": "on",
        "X-Dual-Lobe-Meter": deception_level,
        "X-Dual-Lobe-Meter-Rationale": meter_rationale[:300],
    }
    _handoff_headers(headers, handoff)
    if proxy_used:
        headers["X-Dual-Lobe-Proxy-Tools"] = ",".join(
            f"{name}x{count}" for name, count in sorted(proxy_used.items()))

    # ── 5. Optional flip-back (opt-in via DUAL_LOBE_GATED_FLIP_BACK)
    flip_back = getattr(s, "gated_flip_back", False)
    if flip_back and deception_level in ("RED", "YELLOW"):
        if deception_level == "RED" and concerns:
            correction_lines = [
                "SYSTEM CORRECTION: Your previous response contained factual errors.",
                "The following issues were detected:",
            ]
            for c in concerns[:3]:
                correction_lines.append(
                    f"- \"{c.get('claim_quote', '')}\" — {c.get('reason', '')} — "
                    f"evidence: \"{c.get('evidence_quote', '')}\" → {c.get('correction', '')}"
                )
        else:
            correction_lines = [
                "SYSTEM NOTE: Your previous response may contain unsupported claims.",
                f"Concern category: {meter_rationale}",
                "Review your response and ensure every factual claim is backed by "
                "evidence in the conversation. If you cannot verify a claim, say so "
                "explicitly rather than asserting it as fact.",
            ]
        correction_lines.append(
            "Rewrite your response to the user correcting these issues. "
            "Do NOT mention this correction, the observer, or the system. "
            "Do NOT address this message. Respond to the user as if you "
            "are answering their original question correctly."
        )
        correction_text = "\n".join(correction_lines)

        reconsider_messages = list(enriched_messages) + [
            {"role": "assistant", "content": a_content},
            {"role": "system", "content": correction_text},
        ]
        a_req2 = NormalizedRequest(
            messages=reconsider_messages,
            temperature=payload.get("temperature"),
            max_tokens=payload.get("max_tokens"),
            top_p=payload.get("top_p"),
            tools=payload.get("tools"),
            tool_choice=payload.get("tool_choice"),
            stream=False,
            timeout=s.a_timeout,
        )
        try:
            a_response2 = await asyncio.wait_for(
                a_adapter.buffered(a_req2),
                timeout=s.a_timeout,
            )
            a_data = response_dict(a_response2)
            LOG.info("gated flip-back run=%s — A reconsidered", run_id)
            a_content2 = (a_data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            try:
                recheck_prompt = (
                    f"CONVERSATION MESSAGES:\n{_messages_to_text(messages)}\n\n"
                    f"TOOL EVIDENCE:\n{_extract_tool_evidence(messages)}\n\n"
                    f"A's REVISED OUTPUT:\n{a_content2[:4000]}\n\n"
                )
                b_recheck = await asyncio.wait_for(
                    _call_b_json(b_system, recheck_prompt, b_contract),
                    timeout=s.b_timeout,
                )
                deception_level = b_recheck.get("deception_level", "GREEN").upper()
                meter_rationale = b_recheck.get("meter_rationale", "")
                concerns = b_recheck.get("concerns", [])
                if handoff_on:
                    handoff = _extract_handoff(b_recheck)
                    await _memory_search_into(run_id, tenant_id, shared_space, handoff)
                # Recheck supersedes the first verdict's tool requests.
                b_tool_calls = _extract_b_tool_calls(
                    b_recheck, _client_tool_names(payload.get("tools")))
                LOG.info("gated recheck run=%s level=%s", run_id, deception_level)
                headers["X-Dual-Lobe-Meter"] = deception_level
                headers["X-Dual-Lobe-Meter-Rationale"] = meter_rationale[:300]
                headers["X-Dual-Lobe-Flip-Back"] = "applied"
                _handoff_headers(headers, handoff)
            except Exception as exc:
                LOG.warning("gated recheck failed run=%s: %s", run_id, exc)
        except Exception as exc:
            LOG.warning("gated flip-back failed run=%s: %s — forwarding original", run_id, exc)

    # B's downstream tool requests ride A's final assistant message; the
    # client executes them with the same tools it offered us.
    a_data, b_calls_added = _merge_b_tool_calls(a_data, b_tool_calls)
    if b_calls_added:
        headers["X-Dual-Lobe-B-Tool-Calls"] = str(b_calls_added)

    # Store meter for next upstream
    _set_meter(run_id, {
        "deception_level": deception_level,
        "meter_rationale": meter_rationale,
        "concerns": concerns,
        "assist": str(b_downstream.get("assist", "") or "").strip()
                  if isinstance(b_downstream, dict) else "",
        **(handoff or {}),
        "timestamp": time.time(),
    })

    # Append meter to A's response body so the user sees it.
    # Show meter whenever A produces text content, even if also making tool calls.
    # B never modifies A's actual content — this is appended AFTER A's response.
    has_user_content = False
    for choice in a_data.get("choices", []):
        msg = choice.get("message", {})
        if msg.get("content"):
            has_user_content = True
            break

    if has_user_content:
        meter_line = f"\n\n> ⚠️ **Deception Meter: {deception_level}**"
        if meter_rationale and meter_rationale != "No deception detected.":
            meter_line += f"\n> {meter_rationale[:150]}"
        if deception_level == "RED" and concerns:
            for c in concerns[:3]:
                meter_line += (f"\n> ⚠️ \"{c.get('claim_quote', '')}\" — "
                               f"{c.get('reason', '')} — evidence: "
                               f"\"{c.get('evidence_quote', '')}\"")

        # Warn if agent name not found in multi-agent mode
        if not agent_name_known:
            meter_line += (
                "\n> \n> ⚠️ **Setup Warning:** Agent name not found in system prompt. "
                "Set agent names in the system prompt (e.g. 'You are Alice') for "
                "proper addressing and task assignment in multi-agent mode."
            )

        for choice in a_data.get("choices", []):
            msg = choice.get("message", {})
            if msg.get("content"):
                msg["content"] = msg["content"] + meter_line

    a_data["model"] = public_model
    return a_data, headers
