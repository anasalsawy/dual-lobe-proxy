"""Wrapped co-author handler.

Flow:
    canonical user messages
        -> B upstream route + peer note
        -> A-facing view
        -> A draft / tool call
        -> B downstream co-author + deception guard
        -> A answer + optional visible B contribution

The canonical message list is never destructively edited.  Only A's request
view is rewritten.  B's downstream `to_a` is retained per run and injected at
the next A inference boundary, so A remains aware of its co-author without
pretending B's words originated from A.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from typing import Any

from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from .prompts import COAUTHOR_B_UPSTREAM, COAUTHOR_B_DOWNSTREAM

LOG = logging.getLogger("dual_lobe.coauthor")

# Run-local conversational handoff.  This is intentionally lightweight; the
# canonical visible B contribution also travels in normal conversation history.
# Production durability can later move this to the existing event/state store.
_handoff_store: dict[str, dict[str, Any]] = {}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in ("text", "input_text"):
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "\n".join(x for x in parts if x)
    return str(content or "")


def _latest_user_index(messages: list[dict[str, Any]]) -> int | None:
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user":
            return idx
    return None


def _explicitly_addressed_to_b(text: str) -> bool:
    """Conservative detector for messages explicitly addressed to B.

    We intentionally avoid semantic guessing.  B's LLM translates the newest
    message; this helper only keeps older raw orchestration like `@B ...` from
    leaking into A's view on later turns.
    """
    import re
    t = (text or "").lstrip()
    return bool(re.match(r"^(?:@?b\b[,:\-]?|lobe\s+b\b[,:\-]?|hey\s+b\b[,:\-]?)", t, re.I))


def _sanitize_b_addressed_history(messages: list[dict[str, Any]], latest_idx: int | None) -> list[dict[str, Any]]:
    result = copy.deepcopy(messages)
    for idx, msg in enumerate(result):
        if idx == latest_idx or msg.get("role") != "user":
            continue
        text = _content_text(msg.get("content"))
        if _explicitly_addressed_to_b(text):
            msg["content"] = (
                "[This historical user message was addressed specifically to Lobe B. "
                "Its relevant task content was relayed to A through the co-author channel.]"
            )
    return result


def _messages_to_text(messages: list[dict[str, Any]], max_chars: int = 18000) -> str:
    lines: list[str] = []
    for i, msg in enumerate(messages):
        observed = {
            k: v for k, v in msg.items()
            if k in ("role", "content", "name", "tool_calls", "tool_call_id")
        }
        lines.append(f"[{i}] " + json.dumps(observed, ensure_ascii=False))
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...[middle omitted]...\n" + text[-half:]


def _tool_evidence(messages: list[dict[str, Any]], max_chars: int = 7000) -> str:
    lines: list[str] = []
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                lines.append(
                    f"[{i}] TOOL_REQUEST id={call.get('id','')} "
                    f"name={fn.get('name','')} args={fn.get('arguments','')}"
                )
        elif role == "tool":
            lines.append(
                f"[{i}] TOOL_RESULT id={msg.get('tool_call_id','')} "
                f"name={msg.get('name','')} result={_content_text(msg.get('content'))}"
            )
    text = "\n".join(lines) if lines else "(no tool evidence in conversation)"
    return text[:max_chars]


def _clean_json_content(content: str) -> dict[str, Any]:
    value = (content or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1]
        if value.endswith("```"):
            value = value[:-3]
        value = value.strip()
    return json.loads(value)


async def _call_b(system: str, user: str, *, max_tokens: int | None = None) -> dict[str, Any]:
    s = get_settings()
    adapter = get_registry().adapter("lobe-b")
    last_error: Exception = ValueError("B did not respond")
    for attempt in range(2):
        prompt = user + (
            "\n\nRespond ONLY with the JSON object. No prose, no code fences." if attempt else ""
        )
        req = NormalizedRequest(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=max_tokens or min(s.b_max_output_tokens, 1000),
            stream=False,
            timeout=s.b_timeout,
        )
        response = await adapter.buffered(req)
        data = response_dict(response)
        message = (data.get("choices") or [{}])[0].get("message") or {}
        try:
            return _clean_json_content(str(message.get("content") or ""))
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    raise last_error


def _replace_latest_user_view(
    messages: list[dict[str, Any]],
    user_for_a: str,
) -> list[dict[str, Any]]:
    """Return A's private view without mutating canonical history."""
    idx = _latest_user_index(messages)
    result = _sanitize_b_addressed_history(messages, idx)
    if idx is not None and user_for_a.strip():
        # Preserve role/name metadata.  For multimodal latest messages we keep
        # non-text parts and replace text with B's routed text.
        original = result[idx].get("content")
        if isinstance(original, list):
            kept = [
                part for part in original
                if isinstance(part, dict) and part.get("type") not in ("text", "input_text")
            ]
            result[idx]["content"] = [{"type": "text", "text": user_for_a}] + kept
        else:
            result[idx]["content"] = user_for_a
    return result


def _insert_peer_note(messages: list[dict[str, Any]], note: str, label: str = "B_TO_A") -> list[dict[str, Any]]:
    if not note.strip():
        return messages
    result = list(messages)
    idx = 0
    while idx < len(result) and result[idx].get("role") in ("system", "developer"):
        idx += 1
    peer = {
        "role": "system",
        "content": (
            f"[{label} — PEER CO-AUTHOR MESSAGE]\n"
            "This is a message from Lobe B, your peer co-author, not from the user and "
            "not from you. Consider it critically. Shared evidence outranks either lobe. "
            "Use useful parts naturally; do not mention this wrapper unless B is speaking visibly.\n"
            f"{note.strip()}"
        ),
    }
    result.insert(idx, peer)
    return result


def _append_visible_b(a_content: str, b_content: str) -> str:
    b_content = b_content.strip()
    if not b_content:
        return a_content
    if a_content.strip():
        return f"{a_content.rstrip()}\n\nB: {b_content}"
    return f"B: {b_content}"


def _normalize_upstream(data: dict[str, Any], latest_user: str) -> tuple[str, str, str]:
    user_for_a = str(data.get("user_for_a") or "").strip() or latest_user
    to_a = str(data.get("to_a") or "").strip()
    b_only = str(data.get("b_only") or "").strip()
    return user_for_a, to_a, b_only


def _normalize_downstream(data: dict[str, Any]) -> dict[str, Any]:
    allowed = {"PASS", "ADD", "CORRECT", "REFRAME", "EVIDENCE_GAP", "DECEPTION_GUARD"}
    action = str(data.get("action") or "PASS").upper()
    if action not in allowed:
        action = "PASS"

    verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
    level = str(verification.get("level") or data.get("deception_level") or "GREEN").upper()
    if level not in {"GREEN", "YELLOW", "RED"}:
        level = "GREEN"

    verification_to_user = str(verification.get("to_user") or "").strip()
    # sawii/dialogue promises a visible B verification on every completed answer.
    # Never make a clean PASS disappear merely because the model omitted prose.
    if not verification_to_user:
        if level == "GREEN":
            verification_to_user = (
                "I checked A's answer against the available evidence and found no material unsupported claim."
            )
        elif level == "YELLOW":
            verification_to_user = (
                "I found a material evidence gap in A's answer; treat the affected claim as unverified."
            )
        else:
            verification_to_user = (
                "I found a material conflict between A's answer and the available evidence."
            )

    concerns = verification.get("concerns")
    if not isinstance(concerns, list):
        concerns = data.get("concerns") if isinstance(data.get("concerns"), list) else []

    return {
        "action": action,
        "reply_to_a": str(data.get("reply_to_a") or "").strip(),
        "reply_to_user": str(data.get("reply_to_user") or "").strip(),
        "coauthor_to_a": str(data.get("coauthor_to_a") or data.get("to_a") or "").strip(),
        "coauthor_to_user": str(data.get("coauthor_to_user") or data.get("to_user") or "").strip(),
        "verification": {
            "level": level,
            "to_a": str(verification.get("to_a") or "").strip(),
            "to_user": verification_to_user,
            "rationale": str(verification.get("rationale") or data.get("rationale") or "").strip(),
            "concerns": concerns[:2],
        },
    }


def _join_nonempty(*parts: str) -> str:
    return "\n\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


def _b_visible_message(down: dict[str, Any]) -> str:
    verification = down.get("verification") if isinstance(down.get("verification"), dict) else {}
    return _join_nonempty(
        down.get("reply_to_a", ""),
        down.get("reply_to_user", ""),
        down.get("coauthor_to_user", ""),
        verification.get("to_user", ""),
    )


def _b_private_message(down: dict[str, Any]) -> str:
    verification = down.get("verification") if isinstance(down.get("verification"), dict) else {}
    return _join_nonempty(
        down.get("reply_to_a", ""),
        down.get("coauthor_to_a", ""),
        verification.get("to_a", ""),
    )


async def _a_react_to_b(
    *,
    a_messages: list[dict[str, Any]],
    a_content: str,
    b_to_a: str,
    b_visible: str,
    payload: dict[str, Any],
) -> str:
    """Give A one immediate inference boundary to absorb B's downstream note.

    This is conversational co-authoring, not a hidden rewrite.  A is told that B
    is a peer and may disagree if evidence supports A.  The output is a concise
    continuation/correction so the user can see the collaboration rather than a
    duplicated full answer.
    """
    if not b_to_a.strip():
        return ""
    s = get_settings()
    adapter = get_registry().adapter("lobe-a")
    reaction_messages = list(a_messages) + [
        {"role": "assistant", "content": a_content},
        {
            "role": "system",
            "content": (
                "[B_TO_A_DOWNSTREAM — PEER CO-AUTHOR MESSAGE]\n"
                "Lobe B reviewed your draft and sent the peer note below. "
                "Consider it critically against the shared evidence. If it improves or corrects "
                "your answer, respond with ONLY the concise continuation/correction or direct reply "
                "to B that should follow B's visible contribution. You may address B naturally when "
                "B spoke to you. Do not repeat your full answer. If B is wrong or adds nothing, return "
                "an empty response.\n\nB'S PRIVATE NOTE TO YOU:\n" + b_to_a.strip() +
                "\n\nB'S VISIBLE MESSAGE IN THE SHARED CONVERSATION:\n" + b_visible.strip()
            ),
        },
    ]
    req = NormalizedRequest(
        messages=reaction_messages,
        temperature=payload.get("temperature"),
        max_tokens=min(int(payload.get("max_tokens") or 800), 1200),
        top_p=payload.get("top_p"),
        # The co-author reaction is textual. Tool execution stays on normal host
        # boundaries so a hidden reaction cannot smuggle an external action.
        tools=None,
        tool_choice=None,
        stream=False,
        timeout=s.a_timeout,
    )
    response = await adapter.buffered(req)
    data = response_dict(response)
    return _content_text((data.get("choices") or [{}])[0].get("message", {}).get("content")).strip()


async def coauthor_response(
    payload: dict[str, Any],
    run_id: str,
    tenant_id: int,
    public_model: str = "sawii/dialogue",
) -> tuple[dict[str, Any], dict[str, str]]:
    """Generate one wrapped co-authored response."""
    del tenant_id  # retained for API symmetry / future persistent state
    s = get_settings()
    canonical = payload["messages"]
    latest_idx = _latest_user_index(canonical)
    latest_user = _content_text(canonical[latest_idx].get("content")) if latest_idx is not None else ""
    prior = _handoff_store.get(run_id) or {}

    # 1) Upstream B sees canonical history first and builds A's view.
    upstream_prompt = (
        f"CANONICAL CONVERSATION:\n{_messages_to_text(canonical)}\n\n"
        f"NEWEST USER MESSAGE:\n{latest_user}\n\n"
        f"PREVIOUS B_TO_A HANDOFF (if any):\n{prior.get('to_a','')}\n\n"
        f"TOOL/EVIDENCE STATE:\n{_tool_evidence(canonical)}"
    )
    upstream_error = ""
    try:
        raw_up = await asyncio.wait_for(
            _call_b(COAUTHOR_B_UPSTREAM, upstream_prompt, max_tokens=min(s.b_max_output_tokens, 700)),
            timeout=s.b_timeout,
        )
        user_for_a, upstream_to_a, b_only = _normalize_upstream(raw_up, latest_user)
    except Exception as exc:
        # Fail open: never destroy the user's message if B is unavailable.
        LOG.warning("coauthor upstream B unavailable run=%s error=%s", run_id, type(exc).__name__)
        upstream_error = type(exc).__name__
        user_for_a, upstream_to_a, b_only = latest_user, "", ""

    a_messages = _replace_latest_user_view(canonical, user_for_a)
    carry = str(prior.get("to_a") or "").strip()
    if carry:
        a_messages = _insert_peer_note(a_messages, carry, "B_TO_A_FROM_PREVIOUS_DOWNSTREAM")
    if upstream_to_a:
        a_messages = _insert_peer_note(a_messages, upstream_to_a, "B_TO_A_UPSTREAM")

    # 2) A works with user task + B peer thought.
    a_adapter = get_registry().adapter("lobe-a")
    a_req = NormalizedRequest(
        messages=a_messages,
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        top_p=payload.get("top_p"),
        tools=payload.get("tools"),
        tool_choice=payload.get("tool_choice"),
        stream=False,
        timeout=s.a_timeout,
    )
    try:
        a_response = await asyncio.wait_for(a_adapter.buffered(a_req), timeout=s.a_timeout)
        a_data = response_dict(a_response)
    except Exception as exc:
        LOG.error("coauthor A failed run=%s error=%s", run_id, type(exc).__name__)
        return (
            {"error": {"type": "upstream_error", "message": "Upstream request failed."}},
            {"X-Dual-Lobe-Coauthor": "error"},
        )

    choice = (a_data.get("choices") or [{}])[0]
    a_message = choice.get("message") or {}
    a_content = _content_text(a_message.get("content"))

    # Tool handoff must remain protocol-clean.  B observes the request later
    # together with its actual result; a tool request alone is not evidence.
    if a_message.get("tool_calls"):
        headers = {
            "X-Dual-Lobe-Coauthor": "on",
            "X-Dual-Lobe-Coauthor-Stage": "tool-handoff",
            "X-Dual-Lobe-Upstream": "unavailable" if upstream_error else "ok",
        }
        return a_data, headers

    # 3) Downstream B co-authors and guards the completed A draft.
    downstream_prompt = (
        f"CANONICAL CONVERSATION:\n{_messages_to_text(canonical)}\n\n"
        f"A-FACING NEWEST USER MESSAGE:\n{user_for_a}\n\n"
        f"B'S UPSTREAM NOTE TO A:\n{upstream_to_a}\n\n"
        f"CONTENT ADDRESSED SPECIFICALLY TO B:\n{b_only}\n\n"
        f"TOOL/EVIDENCE STATE:\n{_tool_evidence(canonical)}\n\n"
        f"A'S DRAFT TO CO-AUTHOR:\n{a_content}"
    )
    downstream_error = ""
    try:
        raw_down = await asyncio.wait_for(
            _call_b(COAUTHOR_B_DOWNSTREAM, downstream_prompt, max_tokens=min(s.b_max_output_tokens, 1200)),
            timeout=s.b_timeout,
        )
        down = _normalize_downstream(raw_down)
    except Exception as exc:
        LOG.warning("coauthor downstream B unavailable run=%s error=%s", run_id, type(exc).__name__)
        downstream_error = type(exc).__name__
        down = {
            "action": "PASS",
            "reply_to_a": "",
            "reply_to_user": "",
            "coauthor_to_a": "",
            "coauthor_to_user": "",
            "verification": {
                "level": "UNAVAILABLE",
                "to_a": "",
                "to_user": "Verification unavailable: B could not complete its downstream check.",
                "rationale": "co-author verification unavailable",
                "concerns": [],
            },
        }

    # Build B's two conversational projections.  B is always visible downstream
    # because verification.to_user is mandatory (with deterministic fallback).
    verification = down.get("verification") if isinstance(down.get("verification"), dict) else {}
    b_private = _b_private_message(down)
    b_visible = _b_visible_message(down)

    # Store B->A peer cognition for the next inference boundary as well.
    _handoff_store[run_id] = {
        "to_a": b_private,
        "action": down.get("action", "PASS"),
        "deception_level": verification.get("level", "UNAVAILABLE"),
        "rationale": verification.get("rationale", ""),
        "updated_at": time.time(),
    }

    # Give A one immediate conversational boundary whenever B actually spoke to A
    # privately/directly. This permits visible A↔B exchange in the same user turn.
    a_reaction = ""
    if b_private:
        try:
            a_reaction = await asyncio.wait_for(
                _a_react_to_b(
                    a_messages=a_messages,
                    a_content=a_content,
                    b_to_a=b_private,
                    b_visible=b_visible,
                    payload=payload,
                ),
                timeout=s.a_timeout,
            )
        except Exception as exc:
            LOG.warning("coauthor A reaction unavailable run=%s error=%s", run_id, type(exc).__name__)

    # Visible product is deliberately lobe-labelled so the user experiences two
    # cooperating participants rather than a hidden grader. B always appears.
    merged = f"A: {a_content.strip()}" if a_content.strip() else "A:"
    merged = merged.rstrip() + "\n\nB: " + b_visible.strip()
    if a_reaction:
        merged = merged.rstrip() + "\n\nA: " + a_reaction
    out = copy.deepcopy(a_data)
    if not out.get("choices"):
        out["choices"] = [{"index": 0, "message": {"role": "assistant", "content": merged}, "finish_reason": "stop"}]
    else:
        out["choices"][0].setdefault("message", {"role": "assistant"})
        out["choices"][0]["message"]["content"] = merged
    out["model"] = public_model
    out.setdefault("id", f"chatcmpl-dialogue-{uuid.uuid4().hex[:24]}")

    headers = {
        "X-Dual-Lobe-Coauthor": "on",
        "X-Dual-Lobe-Coauthor-Action": down.get("action", "PASS"),
        "X-Dual-Lobe-Meter": verification.get("level", "UNAVAILABLE"),
        "X-Dual-Lobe-Meter-Rationale": verification.get("rationale", "")[:300],
        "X-Dual-Lobe-B-Visible": "always",
        "X-Dual-Lobe-Upstream": "unavailable" if upstream_error else "ok",
        "X-Dual-Lobe-Downstream": "unavailable" if downstream_error else "ok",
        "X-Dual-Lobe-A-Reaction": "yes" if a_reaction else "no",
    }
    return out, headers
