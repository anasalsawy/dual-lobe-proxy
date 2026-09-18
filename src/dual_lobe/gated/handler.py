"""Gated inference handler — B inline between Hermes and A.

Flow:
  1. Request arrives from Hermes (messages include prior tool calls + results)
  2. UPSTREAM: B's LLM enriches context (observation + broadening + meter)
  3. Enriched messages → A generates response
  4. DOWNSTREAM: B's LLM compares A's response against conversation evidence
  5. GREEN → forward to Hermes
     YELLOW → forward to Hermes (meter stored for next upstream)
     RED → flip back to A: "your claim contradicts evidence, reconsider"
     A regenerates → B rechecks → forward (max one flip, then forward regardless)

No background worker, no outbox, no shadow cycle.  Everything inline.
The meter is stored per-run and injected on the next upstream.
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
from ..core.engine import tenant_session
from ..state import repositories as repo
from .prompts import (
    GATED_B_SYSTEM_UPSTREAM,
    GATED_B_SYSTEM_DOWNSTREAM,
    UPSTREAM_CONTRACT,
    DOWNSTREAM_CONTRACT,
)

LOG = logging.getLogger("dual_lobe.gated")

# Per-run meter store.  Keyed by run_id.  Stores the last deception rating
# so it can be injected into the next upstream call.  In production this
# should use the DB, but for the parallel test we keep it in-memory.
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


async def _call_b(system_prompt: str, user_prompt: str, contract: str) -> dict[str, Any]:
    """Call lobe-b and parse JSON response.  Raises on failure."""
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
    # Tolerate markdown code fences
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return json.loads(content)


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
    stream = payload.get("stream", False)

    # ── 1. UPSTREAM: B enriches context ──────────────────────────
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
        upstream_prompt += "\nInject the meter carry-forward so A adjusts.\n"
    else:
        upstream_prompt += "No prior meter (first turn or clean). No meter to carry forward.\n"

    enriched_messages = messages
    try:
        b_upstream = await asyncio.wait_for(
            _call_b(GATED_B_SYSTEM_UPSTREAM, upstream_prompt, UPSTREAM_CONTRACT),
            timeout=s.b_timeout,
        )
        enriched_messages = b_upstream.get("messages", messages)
        LOG.info("gated upstream B enriched run=%s injected=%s", run_id, b_upstream.get("injected", []))
    except Exception as exc:
        LOG.warning("gated upstream B failed run=%s: %s — using original messages", run_id, exc)
        enriched_messages = messages

    # ── 2. A generates response ──────────────────────────────────
    a_adapter = get_registry().adapter("lobe-a")
    a_req = NormalizedRequest(
        messages=enriched_messages,
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        top_p=payload.get("top_p"),
        tools=payload.get("tools"),
        tool_choice=payload.get("tool_choice"),
        stream=False,  # gated mode buffers A's response for B's check
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

    # ── 3. DOWNSTREAM: B verifies A's response ───────────────────
    downstream_prompt = (
        f"CONVERSATION MESSAGES:\n{_messages_to_text(messages)}\n\n"
        f"TOOL EVIDENCE IN CONVERSATION:\n{_extract_tool_evidence(messages)}\n\n"
        f"A's OUTPUT TO VERIFY:\n{a_content[:4000]}\n\n"
    )

    try:
        b_downstream = await asyncio.wait_for(
            _call_b(GATED_B_SYSTEM_DOWNSTREAM, downstream_prompt, DOWNSTREAM_CONTRACT),
            timeout=s.b_timeout,
        )
        deception_level = b_downstream.get("deception_level", "GREEN").upper()
        meter_rationale = b_downstream.get("meter_rationale", "No deception detected.")
        concerns = b_downstream.get("concerns", [])
        LOG.info("gated downstream B rated run=%s level=%s rationale=%s",
                 run_id, deception_level, meter_rationale[:80])
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

    if deception_level == "RED" and concerns:
        # Flip back to A: send B's correction and ask A to reconsider
        correction_parts = ["Your last response was rated RED for deception. The following concerns were raised:"]
        for c in concerns[:2]:
            correction_parts.append(
                f"- Claim: \"{c.get('claim_quote', '')}\"\n"
                f"  Evidence: \"{c.get('evidence_quote', '')}\"\n"
                f"  Issue: {c.get('reason', '')}\n"
                f"  Correction: {c.get('correction', '')}"
            )
        correction_parts.append("Reconsider your response and correct these issues.")
        correction_text = "\n".join(correction_parts)

        reconsider_messages = list(enriched_messages) + [
            {"role": "assistant", "content": a_content},
            {"role": "user", "content": correction_text},
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
            a_data["model"] = public_model
            LOG.info("gated RED flip-back succeeded run=%s — A reconsidered", run_id)
            # Re-rate the reconsidered response (quick check)
            a_content2 = (a_data2.get("choices") or [{}])[0].get("message", {}).get("content", "")
            try:
                recheck_prompt = (
                    f"CONVERSATION MESSAGES:\n{_messages_to_text(messages)}\n\n"
                    f"TOOL EVIDENCE:\n{_extract_tool_evidence(messages)}\n\n"
                    f"A's REVISED OUTPUT:\n{a_content2[:4000]}\n\n"
                )
                b_recheck = await asyncio.wait_for(
                    _call_b(GATED_B_SYSTEM_DOWNSTREAM, recheck_prompt, DOWNSTREAM_CONTRACT),
                    timeout=s.b_timeout,
                )
                deception_level = b_recheck.get("deception_level", "GREEN").upper()
                meter_rationale = b_recheck.get("meter_rationale", "")
                concerns = b_recheck.get("concerns", [])
                LOG.info("gated recheck rated run=%s level=%s", run_id, deception_level)
                headers["X-Dual-Lobe-Meter"] = deception_level
                headers["X-Dual-Lobe-Meter-Rationale"] = meter_rationale[:200]
                headers["X-Dual-Lobe-Flip-Back"] = "applied"
            except Exception as exc:
                LOG.warning("gated recheck failed run=%s: %s — forwarding reconsidered response", run_id, exc)
        except Exception as exc:
            LOG.warning("gated RED flip-back failed run=%s: %s — forwarding original", run_id, exc)

    # Store the meter for next upstream
    _set_meter(run_id, {
        "deception_level": deception_level,
        "meter_rationale": meter_rationale,
        "concerns": concerns,
        "timestamp": time.time(),
    })

    # Ensure model field is set correctly
    a_data["model"] = public_model
    return a_data, headers
