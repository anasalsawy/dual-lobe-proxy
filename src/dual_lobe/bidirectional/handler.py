"""Bidirectional conversational routing with reciprocal verification.

The selected lobe owns the user-facing turn and receives the caller's tools.
The other lobe can be consulted internally, then verifies the final candidate.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from ..provider import calltrace
from ..state.memory import inject_shared_memory

LOG = logging.getLogger("dual_lobe.bidirectional")

_CONSULT_TOOL = {
    "type": "function",
    "function": {
        "name": "consult_other_lobe",
        "description": "Ask the other lobe for an internal opinion before you answer the user.",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
            "additionalProperties": False,
        },
    },
}
_HANDOFF_TOOL = {
    "type": "function",
    "function": {
        "name": "handoff_to_other_lobe",
        "description": "Transfer the user-facing turn to the other lobe, which will answer the user.",
        "parameters": {
            "type": "object",
            "properties": {"context": {"type": "string"}},
            "required": ["context"],
            "additionalProperties": False,
        },
    },
}
_ROLES = {"A": "lobe-a", "B": "lobe-b"}


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(part.get("text", "")) for part in value if isinstance(part, dict))
    return str(value or "")


def latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_text(message.get("content"))
    return ""


def select_speaker(messages: list[dict[str, Any]]) -> str:
    """Select a resumed tool-call owner first, otherwise honor direct address."""
    tool_ids = []
    for message in reversed(messages):
        if message.get("role") != "tool":
            break
        tool_ids.append(str(message.get("tool_call_id") or ""))
    if tool_ids:
        resolved = []
        wanted = set(tool_ids)
        for message in reversed(messages):
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                continue
            for call in message["tool_calls"]:
                call_id = str(call.get("id") or "")
                if call_id in wanted:
                    if call_id.startswith("dlA_"):
                        resolved.append("A")
                    elif call_id.startswith("dlB_"):
                        resolved.append("B")
            if resolved:
                break
        if resolved and len(set(resolved)) == 1:
            return resolved[0]

    text = latest_user_text(messages).lstrip()
    match = re.match(r"(?i)^(?:(?:hey|hi|hello|yo)\s+(?:lobe\s+)?([ab])\b|(?:lobe\s+)?([ab])\s*[,!:])", text)
    return next((group.upper() for group in match.groups() if group), "A") if match else "A"


def requested_consultee(text: str, speaker: str) -> str | None:
    match = re.search(r"(?i)\b(?:ask|consult|check with|get the view of)\s+(?:lobe\s+)?([ab])\b", text)
    if not match:
        return None
    other = match.group(1).upper()
    return other if other != speaker else None


def _system_prompt(speaker: str, *, verify: bool = False, tools: list[dict] | None = None) -> str:
    available = ", ".join(
        str((tool.get("function") or {}).get("name", "")) for tool in (tools or [])
        if (tool.get("function") or {}).get("name")
    ) or "none supplied"
    if verify:
        return (
            f"You are Lobe {speaker}, the independent verifier. Review the other lobe's exact candidate "
            "answer against the user's request and the supplied conversation/tool results. Return only JSON "
            'with keys deception_level (GREEN, YELLOW, or RED), rationale, missing, and unverified. '
            "Do not rewrite the answer. Use GREEN only when the answer is adequately supported; use YELLOW "
            "for unresolved evidence or uncertainty, and RED for a material false or unsafe claim. "
            f"The client tools available to the speaking lobe were: {available}."
        )
    return (
        f"You are Lobe {speaker}, the user-facing speaker for this turn. Respond normally to the latest user. "
        "The other lobe is your peer: use consult_other_lobe when the user asks you to get its view or when "
        "its independent input would materially improve the answer. Consultation is private; incorporate useful "
        "input in your own final answer and explicitly attribute material input to the other lobe (for example, "
        "'I asked Lobe B, and it suggested ...'). Use handoff_to_other_lobe when the other lobe should take over "
        "and answer the user directly. You may call any client tool supplied with this request. Tool calls are "
        "executed by the caller and returned to you on the next request. Never claim a tool ran until its result "
        "appears in the conversation. Do not expose internal consultation unless useful to the user."
    )


async def _call(alias: str, messages: list[dict[str, Any]], payload: dict[str, Any], *,
                tools: list[dict[str, Any]] | None, tool_choice: Any = None,
                verify: bool = False, speaker: str | None = None) -> dict[str, Any]:
    adapter = get_registry().adapter(alias)
    completion_tokens = None if verify else payload.get("max_completion_tokens")
    request = NormalizedRequest(
        messages=messages,
        temperature=0 if verify else payload.get("temperature"),
        max_tokens=1200 if verify else (payload.get("max_tokens") if completion_tokens is None else None),
        max_completion_tokens=completion_tokens,
        top_p=payload.get("top_p"),
        stop=payload.get("stop"),
        tools=tools,
        tool_choice=tool_choice,
        parallel_tool_calls=payload.get("parallel_tool_calls"),
        stream=False,
        response_format={"type": "json_object"} if verify else payload.get("response_format"),
        seed=payload.get("seed"),
        timeout=get_settings().b_timeout if alias == "lobe-b" else get_settings().a_timeout,
        reasoning_effort=payload.get("reasoning_effort"),
        frequency_penalty=payload.get("frequency_penalty"),
        presence_penalty=payload.get("presence_penalty"),
    )
    raw = await adapter.buffered(request)
    data = response_dict(raw)
    choices = data.get("choices") or []
    if not choices:
        raise ValueError(f"{alias} returned no choices")
    return choices[0].get("message") or {}


async def _consult(speaker: str, question: str, messages: list[dict[str, Any]],
                   payload: dict[str, Any]) -> str:
    peer = "B" if speaker == "A" else "A"
    context = "\n".join(
        f"{m.get('role', '?')}: {_content_text(m.get('content'))[:1800]}"
        for m in messages[-8:]
    )
    prompt = (
        f"You are Lobe {peer}, consulted privately by Lobe {speaker}. Give a concise independent view on: "
        f"{question}\n\nRecent conversation context:\n{context}\n\nReturn your useful input directly. "
        "Do not address the user or claim tool use."
    )
    try:
        message = await _call(_ROLES[peer], [{"role": "user", "content": prompt}], payload, tools=None)
        return _content_text(message.get("content"))[:5000] or "The other lobe returned no text."
    except Exception as exc:  # noqa: BLE001
        LOG.warning("private lobe consultation failed speaker=%s peer=%s error=%s",
                    speaker, peer, type(exc).__name__)
        return f"Lobe {peer} could not be reached for this consultation."


def _parse_verdict(message: dict[str, Any]) -> dict[str, Any]:
    content = _content_text(message.get("content")).strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].removesuffix("```").strip()
    try:
        value = json.loads(content)
        level = str(value.get("deception_level", "YELLOW")).upper()
        if level not in {"GREEN", "YELLOW", "RED"}:
            level = "YELLOW"
        rationale = str(value.get("rationale") or "Verifier returned no rationale.").strip()
        return {"deception_level": level, "rationale": rationale[:1200],
                "missing": value.get("missing") or [], "unverified": value.get("unverified") or []}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {"deception_level": "YELLOW",
                "rationale": "The independent verification response was invalid; the answer remains unverified.",
                "missing": [], "unverified": ["Verifier did not return valid JSON."]}


def _tag_tool_calls(calls: list[dict[str, Any]], speaker: str) -> list[dict[str, Any]]:
    tagged = []
    for call in calls:
        item = dict(call)
        old_id = str(item.get("id") or uuid.uuid4().hex)
        item["id"] = f"dl{speaker}_" + old_id[-48:]
        tagged.append(item)
    return tagged


async def _run(payload: dict[str, Any], *, run_id: str = "", tenant_id: int = 0,
               shared_text: str | None = None) -> tuple[str | None, list[dict[str, Any]], dict[str, Any]]:
    from ..gated.handler import OBSERVATION_DISCLAIMER, _get_meter, _set_meter
    messages = list(payload.get("messages") or [])
    messages = inject_shared_memory(messages, shared_text)
    messages.insert(0, {"role": "system", "content": OBSERVATION_DISCLAIMER})
    previous_meter = _get_meter(run_id) if run_id else None
    if previous_meter:
        messages.insert(1, {"role": "system", "content":
            "[Observer meter from prior turn: " + str(previous_meter.get("deception_level", "YELLOW")) +
            " — " + str(previous_meter.get("meter_rationale", ""))[:500] + "]"})
    tools = list(payload.get("tools") or [])
    speaker = select_speaker(messages)
    verifier = "B" if speaker == "A" else "A"
    latest = latest_user_text(messages)
    tool_continuation = bool(messages and messages[-1].get("role") == "tool")
    explicit_peer = None if tool_continuation else requested_consultee(latest, speaker)
    dialogue = list(messages)
    consulted = False
    consultation_note = ""
    if explicit_peer:
        advice = await _consult(speaker, latest, dialogue, payload)
        consultation_note = f"Lobe {explicit_peer}'s view: {advice}"
        dialogue.append({"role": "user", "name": f"lobe_{explicit_peer.lower()}_private_input",
                         "content": consultation_note})
        consulted = True

    can_route = not consulted and not tool_continuation and payload.get("tool_choice") in (None, "auto")
    exposed_tools = tools + ([_CONSULT_TOOL, _HANDOFF_TOOL] if can_route else [])
    message = await _call(_ROLES[speaker], dialogue, payload, tools=exposed_tools or None,
                          tool_choice="auto" if can_route else payload.get("tool_choice"))

    calls = message.get("tool_calls") or []
    handoff_calls = [c for c in calls if ((c.get("function") or {}).get("name") == "handoff_to_other_lobe")]
    if handoff_calls and not tool_continuation:
        previous_speaker = speaker
        new_speaker = "B" if speaker == "A" else "A"
        context = latest
        try:
            args = json.loads((handoff_calls[0].get("function") or {}).get("arguments") or "{}")
            context = str(args.get("context") or latest)
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        dialogue.append({"role": "user", "name": f"lobe_{previous_speaker.lower()}_handoff",
                         "content": f"Lobe {previous_speaker} handed this user-facing turn to you. "
                                    f"Its context: {context}"})
        speaker, verifier = new_speaker, previous_speaker
        consulted = False
        consultation_note = ""
        message = await _call(_ROLES[speaker], dialogue, payload, tools=tools or None,
                              tool_choice=payload.get("tool_choice"))
        calls = message.get("tool_calls") or []

    consult_calls = [c for c in calls if ((c.get("function") or {}).get("name") == "consult_other_lobe")]
    if consult_calls and not consulted:
        question = latest
        try:
            args = json.loads((consult_calls[0].get("function") or {}).get("arguments") or "{}")
            question = str(args.get("question") or latest)
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        advice = await _consult(speaker, question, dialogue, payload)
        consultation_note = f"Lobe {'B' if speaker == 'A' else 'A'}'s view: {advice}"
        consult_id = str(consult_calls[0].get("id") or "consult")
        dialogue.extend([
            {"role": "assistant", "content": None, "tool_calls": consult_calls},
            {"role": "tool", "tool_call_id": consult_id, "name": "consult_other_lobe", "content": advice},
        ])
        external_tools = [t for t in tools if ((t.get("function") or {}).get("name") != "consult_other_lobe")]
        message = await _call(_ROLES[speaker], dialogue, payload, tools=external_tools or None,
                              tool_choice=payload.get("tool_choice"))
        calls = message.get("tool_calls") or []

    client_calls = [c for c in calls if ((c.get("function") or {}).get("name") != "consult_other_lobe")]
    if client_calls:
        return None, _tag_tool_calls(client_calls, speaker), {
            "mode": "bidirectional", "speaker": speaker, "verifier": verifier,
            "status": "awaiting_client_tools", "consulted": consulted or bool(consult_calls),
            "consultation_note": consultation_note,
        }

    answer = _content_text(message.get("content")).strip()
    if not answer:
        raise ValueError(f"Lobe {speaker} returned neither an answer nor a client tool call")

    evidence = "\n".join(
        f"[{m.get('role')}] {_content_text(m.get('content'))[:1200]}"
        for m in messages[-12:] if m.get("role") in {"assistant", "tool"}
    )
    user_text = latest or "(no latest user message)"
    verify_prompt = (
        f"USER REQUEST:\n{user_text}\n\nCANDIDATE ANSWER FROM LOBE {speaker} (preserve exactly):\n{answer}\n\n"
        f"RECENT TOOL/ASSISTANT EVIDENCE:\n{evidence or '(no prior tool evidence)'}"
    )
    try:
        with calltrace.stage(f"{verifier}-verify"):
            verdict_message = await _call(
                _ROLES[verifier], [{"role": "system", "content": _system_prompt(verifier, verify=True, tools=tools)},
                                   {"role": "user", "content": verify_prompt}],
                payload, tools=tools or None, tool_choice="none" if tools else None, verify=True,
            )
        verdict = _parse_verdict(verdict_message)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("bidirectional verification failed speaker=%s verifier=%s error=%s",
                    speaker, verifier, type(exc).__name__)
        verdict = {"deception_level": "YELLOW",
                   "rationale": "The independent verification call failed; the answer remains unverified.",
                   "missing": [], "unverified": ["Verifier call failed."]}
    if run_id:
        _set_meter(run_id, {"deception_level": verdict["deception_level"],
                            "meter_rationale": verdict["rationale"], "timestamp": time.time(),
                            "speaker": speaker, "verifier": verifier})
    meter = f"[{verdict['deception_level']}] {verdict['rationale']}"
    return f"{answer}\n\nDual-Lobe meter: {meter}", [], {
        "mode": "bidirectional", "speaker": speaker, "verifier": verifier,
        "verdict": verdict, "consulted": consulted or bool(consult_calls),
    }


async def bidirectional_response(payload: dict[str, Any], run_id: str = "", tenant_id: int = 0,
                                 shared_text: str | None = None):
    started = time.time()
    answer, tool_calls, extra = await _run(payload, run_id=run_id, tenant_id=tenant_id,
                                           shared_text=shared_text)
    if tool_calls:
        message: dict[str, Any] = {"role": "assistant", "content": None, "tool_calls": tool_calls}
    else:
        message = {"role": "assistant", "content": answer}
    if extra.get("verdict"):
        extra["model_calls"] = 2 + int(extra.get("consulted", False))
        extra["verifier"] = extra.get("verifier")
        extra["elapsed_ms"] = round((time.time() - started) * 1000, 2)
    extra["calls"] = calltrace.snapshot()
    extra["server_ms"] = calltrace.now_ms()
    data = {
        "id": f"chatcmpl-bidir-{uuid.uuid4().hex[:20]}", "object": "chat.completion",
        "created": int(started), "model": payload.get("model") or "sawii/dl-bidirectional",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "dual_lobe": extra,
    }
    if not payload.get("stream"):
        headers = {"X-Dual-Lobe-Routing": "bidirectional"}
        if extra.get("verdict"):
            headers["X-Dual-Lobe-Meter"] = extra["verdict"]["deception_level"]
            headers["X-Dual-Lobe-Speaker"] = extra["speaker"]
        return JSONResponse(data, headers=headers)

    async def events():
        chunk = {**data, "object": "chat.completion.chunk"}
        choice = chunk["choices"][0]
        choice["delta"] = choice.pop("message")
        choice["finish_reason"] = "tool_calls" if tool_calls else "stop"
        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"X-Dual-Lobe-Routing": "bidirectional"})
