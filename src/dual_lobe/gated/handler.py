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
from typing import Any

from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from ..roles import get_role_persona
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
                "next_step": "", "missing": []}

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
    }


def _render_handoff(meter: dict[str, Any]) -> str:
    """Render B's handoff (plus its free-form assist) as A's next-call material."""
    parts: list[str] = []
    assist = str(meter.get("assist", "") or "").strip()
    if assist:
        parts.append(assist)
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
    return "\n\n".join(parts)[:2000]


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

    # B's assist material from the last call, delivered as a tool result so A
    # reads it in its own tool flow. Same injection point as the meter warning.
    if last_meter:
        enriched_messages = enriched_messages + _build_assist_injections(last_meter)

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

    # ── 4. Forward A's response with meter in headers ───────────
    headers = {
        "X-Dual-Lobe-Gated": "on",
        "X-Dual-Lobe-Meter": deception_level,
        "X-Dual-Lobe-Meter-Rationale": meter_rationale[:300],
    }
    if handoff:
        review = handoff["tool_review"]
        headers["X-Dual-Lobe-Tool-Review"] = review["verdict"]
        headers["X-Dual-Lobe-Handoff"] = (
            f"u={len(handoff['unverified'])};t={review['verdict']};"
            f"s={1 if handoff['next_step'] else 0};m={len(handoff['missing'])}"
        )

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
                LOG.info("gated recheck run=%s level=%s", run_id, deception_level)
                headers["X-Dual-Lobe-Meter"] = deception_level
                headers["X-Dual-Lobe-Meter-Rationale"] = meter_rationale[:300]
                headers["X-Dual-Lobe-Flip-Back"] = "applied"
                if handoff:
                    headers["X-Dual-Lobe-Tool-Review"] = handoff["tool_review"]["verdict"]
                    headers["X-Dual-Lobe-Handoff"] = (
                        f"u={len(handoff['unverified'])};t={handoff['tool_review']['verdict']};"
                        f"s={1 if handoff['next_step'] else 0};m={len(handoff['missing'])}"
                    )
            except Exception as exc:
                LOG.warning("gated recheck failed run=%s: %s", run_id, exc)
        except Exception as exc:
            LOG.warning("gated flip-back failed run=%s: %s — forwarding original", run_id, exc)

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
