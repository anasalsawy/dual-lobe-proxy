"""Gated inference handler — B inline between Hermes and A.

Guardrails:
  - B upstream returns structured JSON (injection texts), NOT a messages
    array.  The handler deterministically inserts them as system messages.
    B's voice never appears as a user/assistant message.
  - B downstream returns only a JSON rating.  A's response is forwarded
    VERBATIM.  B never touches A's content.
  - On RED flip-back, the correction is a system message (not user), and
    the prompt tells A to fix the response without addressing the message.
  - A's response to the user is always the final output.  B never sends
    anything to the user.

Flow:
  1. Request arrives from Hermes
  2. UPSTREAM: B's LLM produces injection texts (observation, broadening, meter)
     Handler inserts them as system messages deterministically.
  3. Enriched messages → A generates response
  4. DOWNSTREAM: B's LLM rates A's response (GREEN/YELLOW/RED)
  5. GREEN/YELLOW → forward A's response verbatim to Hermes
     RED → flip back: system message correction → A regenerates → forward
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from .prompts import (
    GATED_B_SYSTEM_UPSTREAM,
    GATED_B_SYSTEM_DOWNSTREAM,
    UPSTREAM_CONTRACT,
    DOWNSTREAM_CONTRACT,
)

LOG = logging.getLogger("dual_lobe.gated")

# Per-run meter store.  Keyed by run_id.
_meter_store: dict[str, dict[str, Any]] = {}


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
    req = NormalizedRequest(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt + "\n\n" + contract},
        ],
        temperature=0,
        max_tokens=s.b_max_output_tokens,
        timeout=s.b_timeout,
    )
    response = await adapter.buffered(req)
    data = response_dict(response)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise ValueError("B returned empty content")
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return json.loads(content)


def _insert_system_injections(
    messages: list[dict[str, Any]],
    injections: list[str],
) -> list[dict[str, Any]]:
    """Deterministically insert injection texts as system messages.

    Injections go right after existing system messages, before the
    conversation starts.  B's voice never appears as user/assistant.
    """
    if not injections:
        return messages
    result = []
    inserted = False
    for msg in messages:
        result.append(msg)
        if msg.get("role") == "system" and not inserted:
            # After the last leading system message, insert injections
            pass
    # Simpler: find the index after the last leading system message
    index = 0
    while index < len(messages) and messages[index].get("role") in ("system", "developer"):
        index += 1
    result = list(messages[:index])
    for text in injections:
        if text and text.strip():
            result.append({"role": "system", "content": text.strip()})
    result.extend(messages[index:])
    return result


async def gated_response(
    payload: dict[str, Any],
    run_id: str,
    tenant_id: int,
    public_model: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Handle a gated inference request.

    Returns (response_data, headers) to be sent back to Hermes.
    """
    s = get_settings()
    messages = payload["messages"]

    # ── 1. UPSTREAM: B produces injection texts ──────────────────
    last_meter = _get_meter(run_id)
    upstream_prompt = (
        f"CONVERSATION MESSAGES:\n{_messages_to_text(messages)}\n\n"
        f"TOOL EVIDENCE IN CONVERSATION:\n{_extract_tool_evidence(messages)}\n\n"
    )
    if last_meter:
        upstream_prompt += (
            f"LAST TURN METER:\n"
            f"  deception_level: {last_meter.get('deception_level', 'GREEN')}\n"
            f"  rationale: {last_meter.get('meter_rationale', '')}\n"
        )
        if last_meter.get("concerns"):
            upstream_prompt += f"  concerns: {json.dumps(last_meter['concerns'], ensure_ascii=False)}\n"
        upstream_prompt += "\nInclude the meter carry-forward in your injection.\n"
    else:
        upstream_prompt += "No prior meter (first turn or clean).\n"

    injections: list[str] = []
    try:
        b_upstream = await asyncio.wait_for(
            _call_b_json(GATED_B_SYSTEM_UPSTREAM, upstream_prompt, UPSTREAM_CONTRACT),
            timeout=s.b_timeout,
        )
        # B returns structured injection texts, NOT a messages array.
        # The handler controls how they're inserted.  B never touches
        # the message structure directly.
        obs = b_upstream.get("observation_disclaimer", "")
        broadening = b_upstream.get("context_broadening", "")
        meter = b_upstream.get("meter_carryforward", "")
        if obs:
            injections.append(obs)
        if broadening:
            injections.append(broadening)
        if meter:
            injections.append(meter)
        LOG.info("gated upstream B run=%s injections=%d", run_id, len(injections))
    except Exception as exc:
        LOG.warning("gated upstream B failed run=%s: %s — using original messages", run_id, exc)

    # Deterministically insert injections as system messages.
    # B's voice never appears as user/assistant.
    enriched_messages = _insert_system_injections(messages, injections)

    # ── 2. A generates response ──────────────────────────────────
    a_adapter = get_registry().adapter("lobe-a")
    a_req = NormalizedRequest(
        messages=enriched_messages,
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        top_p=payload.get("top_p"),
        tools=payload.get("tools"),
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

    a_message = (a_data.get("choices") or [{}])[0].get("message", {})
    a_content = a_message.get("content", "")

    # ── 3. DOWNSTREAM: B rates A's response ──────────────────────
    # B returns ONLY a JSON rating.  It never touches A's content.
    downstream_prompt = (
        f"CONVERSATION MESSAGES:\n{_messages_to_text(messages)}\n\n"
        f"TOOL EVIDENCE IN CONVERSATION:\n{_extract_tool_evidence(messages)}\n\n"
        f"A's OUTPUT TO VERIFY:\n{a_content[:4000]}\n\n"
    )

    try:
        b_downstream = await asyncio.wait_for(
            _call_b_json(GATED_B_SYSTEM_DOWNSTREAM, downstream_prompt, DOWNSTREAM_CONTRACT),
            timeout=s.b_timeout,
        )
        deception_level = b_downstream.get("deception_level", "GREEN").upper()
        meter_rationale = b_downstream.get("meter_rationale", "No deception detected.")
        concerns = b_downstream.get("concerns", [])
        LOG.info("gated downstream B rated run=%s level=%s", run_id, deception_level)
    except Exception as exc:
        LOG.warning("gated downstream B failed run=%s: %s — failing open (GREEN)", run_id, exc)
        deception_level = "GREEN"
        meter_rationale = "verification unavailable"
        concerns = []

    # ── 4. Act on the meter ──────────────────────────────────────
    headers = {
        "X-Dual-Lobe-Gated": "on",
        "X-Dual-Lobe-Meter": deception_level,
        "X-Dual-Lobe-Meter-Rationale": meter_rationale[:200],
    }

    if deception_level in ("RED", "YELLOW"):
        # Flip back: send correction as a SYSTEM message (not user).
        # A must NOT address the correction or mention it to the user.
        # YELLOW gets a softer nudge; RED gets specific concerns.
        if deception_level == "RED" and concerns:
            correction_lines = [
                "SYSTEM CORRECTION: Your previous response contained factual errors.",
                "The following issues were detected:",
            ]
            for c in concerns[:2]:
                correction_lines.append(
                    f"- You claimed: \"{c.get('claim_quote', '')}\"\n"
                    f"  But the evidence shows: \"{c.get('evidence_quote', '')}\"\n"
                    f"  Issue: {c.get('reason', '')}\n"
                    f"  You should say: {c.get('correction', '')}"
                )
        else:
            # YELLOW: softer nudge, no specific claims
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
            a_data2 = response_dict(a_response2)
            a_data = a_data2
            LOG.info("gated RED flip-back run=%s — A reconsidered", run_id)
            # Quick recheck
            a_content2 = (a_data2.get("choices") or [{}])[0].get("message", {}).get("content", "")
            try:
                recheck_prompt = (
                    f"CONVERSATION MESSAGES:\n{_messages_to_text(messages)}\n\n"
                    f"TOOL EVIDENCE:\n{_extract_tool_evidence(messages)}\n\n"
                    f"A's REVISED OUTPUT:\n{a_content2[:4000]}\n\n"
                )
                b_recheck = await asyncio.wait_for(
                    _call_b_json(GATED_B_SYSTEM_DOWNSTREAM, recheck_prompt, DOWNSTREAM_CONTRACT),
                    timeout=s.b_timeout,
                )
                deception_level = b_recheck.get("deception_level", "GREEN").upper()
                meter_rationale = b_recheck.get("meter_rationale", "")
                concerns = b_recheck.get("concerns", [])
                LOG.info("gated recheck run=%s level=%s", run_id, deception_level)
                headers["X-Dual-Lobe-Meter"] = deception_level
                headers["X-Dual-Lobe-Meter-Rationale"] = meter_rationale[:200]
                headers["X-Dual-Lobe-Flip-Back"] = "applied"
            except Exception as exc:
                LOG.warning("gated recheck failed run=%s: %s", run_id, exc)
        except Exception as exc:
            LOG.warning("gated RED flip-back failed run=%s: %s — forwarding original", run_id, exc)

    # Store meter for next upstream
    _set_meter(run_id, {
        "deception_level": deception_level,
        "meter_rationale": meter_rationale,
        "concerns": concerns,
        "timestamp": time.time(),
    })

    # A's response is forwarded VERBATIM.  B never modifies it.
    a_data["model"] = public_model
    return a_data, headers
