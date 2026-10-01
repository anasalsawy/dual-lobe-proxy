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
from copy import deepcopy
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

from ..core.settings import get_settings
from ..core.meter_format import (
    format_deception_meter,
    strip_deception_meter,
)
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from ..provider import calltrace
from ..proxy.tools import (CONSULT, DELEGATE, MEMORY_SEARCH, execute_proxy_call,
                           is_proxy_tool, proxy_tool_schemas)
from ..state.memory import inject_shared_memory
from .routing import routing_requested

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


def _privacy_view(messages: list[dict[str, Any]], guard, vault) -> list[dict[str, Any]]:
    """Build A's view: text is tokenized, images and tool arguments are withheld."""
    safe = deepcopy(messages)
    for message in safe:
        content = message.get("content")
        if isinstance(content, str):
            message["content"], _ = guard.sanitize(content, vault=vault)
        elif isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"image", "image_url", "input_image"} or "image_url" in part:
                    parts.append({"type": "text", "text": "[screen/image withheld from A; B can inspect it]"})
                elif isinstance(part.get("text"), str):
                    text, _ = guard.sanitize(part["text"], vault=vault)
                    parts.append({**part, "text": text})
            message["content"] = parts
        if message.get("tool_calls"):
            # A can see that B used a tool, never the arguments it sent.
            for call in message["tool_calls"]:
                fn = call.get("function") or {}
                if fn:
                    fn["arguments"] = "[tool arguments withheld from A]"
    return safe


def _rehydrate_messages(messages: list[dict[str, Any]], vault) -> list[dict[str, Any]]:
    """Restore tokens only for the local B lobe or the protected tool boundary."""
    safe = deepcopy(messages)
    for message in safe:
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = vault.rehydrate_text(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = vault.rehydrate_text(part["text"])
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            if isinstance(fn.get("arguments"), str):
                fn["arguments"] = vault.rehydrate_text(fn["arguments"])
    return safe


def _role_alias(speaker: str, *, secure: bool) -> str:
    return "lobe-b-secure" if secure and speaker == "B" else _ROLES[speaker]


def _assert_secure_b_local() -> None:
    """Fail closed if the secure variant's raw-data B endpoint is remote."""
    from urllib.parse import urlparse
    from fastapi import HTTPException

    s = get_settings()
    if s.testing_mode and not s.secure_b_local_only:
        return
    target = get_registry().target("lobe-b-secure")
    host = (urlparse(target.base_url or "").hostname or "").lower()
    local = host in {"localhost", "::1", "host.docker.internal"} or host.startswith("127.")
    if not local:
        raise HTTPException(503, "Secure B is local-only unless testing mode is explicitly enabled with "
                                "DUAL_LOBE_TESTING_MODE=true and "
                                "DUAL_LOBE_SECURE_B_LOCAL_ONLY=false.")


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
    match = re.match(
        r"(?i)^(?:(?:hey|hi|hello|yo)\s+(?:lobe\s+)?([ab])\b|"
        r"(?:lobe\s+)?([ab])\s*(?:[,!:]|\b(?:ask|please|what|help|answer|respond)\b))",
        text,
    )
    return next((group.upper() for group in match.groups() if group), "A") if match else "A"


def requested_consultee(text: str, speaker: str) -> str | None:
    match = re.search(r"(?i)\b(?:ask|consult|check with|get the view of)\s+(?:lobe\s+)?([ab])\b", text)
    if not match:
        return None
    other = match.group(1).upper()
    return other if other != speaker else None


def requested_handoff(text: str, speaker: str) -> str | None:
    """Honor an explicit user instruction to give the turn to the peer lobe."""
    match = re.search(
        r"(?i)\b(?:hand(?:\s+off)?|transfer|pass|route)\b.{0,80}?\bto\s+(?:lobe\s+)?([ab])\b",
        text,
    ) or re.search(r"(?i)\b(?:let|have)\s+(?:lobe\s+)?([ab])\s+(?:answer|respond|take over)\b", text)
    if not match:
        return None
    target = match.group(1).upper()
    return target if target != speaker else None


def _system_prompt(speaker: str, *, verify: bool = False, tools: list[dict] | None = None) -> str:
    if verify:
        from ..gated.prompts import GATED_B_SYSTEM_DOWNSTREAM
        return _role_specific_verifier_text(GATED_B_SYSTEM_DOWNSTREAM, speaker)
    names = ", ".join(
        str((tool.get("function") or {}).get("name", ""))
        for tool in (tools or [])
        if (tool.get("function") or {}).get("name")
    ) or "none supplied"
    return (
        "Keep the host application's system/developer instructions, identity, voice, and native tool-use behavior. "
        "This adds only dual-lobe routing behavior; it does not replace the host instructions. "
        "Use the tools supplied with this request through their normal tool-call interface. When the user asks you "
        "to perform a task and an appropriate tool is available, call it and continue from its result; do not give "
        "commands or sample code for the user to run instead. If a tool fails, report its exact error and try another "
        "relevant available tool. Do not infer that all access is denied from one failed tool. Never ask the user to "
        "paste passwords, API keys, access tokens, cookies, or credential/session-file contents. "
        "The tools available on this request are: " + names + ". "
        "Do not write a deception meter or verification rating; the proxy appends the verifier's meter once. "
        "Use consult_other_lobe when the user explicitly asks for the peer's view or when that independent input "
        "would materially improve the answer. Use relevant peer advice to complete the task, and let the proxy "
        "include the exact consultation result once. Use handoff_to_other_lobe when the user asks the peer to take "
        "the user-facing turn. Preserve the host assistant's normal identity and style in the response."
    )

def _role_specific_verifier_text(text: str, verifier: str) -> str:
    """Adapt the shared A-facing verifier rubric to whichever lobe spoke."""
    speaker = "B" if verifier == "A" else "A"
    text = re.sub(r"\bA\b", f"Lobe {speaker}", text)
    return text.replace("Gate-B", f"Lobe {verifier}")

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
        timeout=get_settings().b_timeout if alias in {"lobe-b", "lobe-b-secure"} else get_settings().a_timeout,
        reasoning_effort=payload.get("reasoning_effort"),
        frequency_penalty=payload.get("frequency_penalty"),
        presence_penalty=payload.get("presence_penalty"),
    )
    with calltrace.stage((f"{speaker}-verify" if verify else f"{speaker or alias}-generate")):
        raw = await adapter.buffered(request)
    data = response_dict(raw)
    choices = data.get("choices") or []
    if not choices:
        raise ValueError(f"{alias} returned no choices")
    return choices[0].get("message") or {}


async def _consult(speaker: str, question: str, messages: list[dict[str, Any]],
                   payload: dict[str, Any], *, secure: bool = False, guard=None, vault=None) -> str:
    peer = "B" if speaker == "A" else "A"
    if secure and peer == "A":
        messages = _privacy_view(messages, guard, vault)
        question, _ = guard.sanitize(question, vault=vault)
    elif secure and peer == "B":
        messages = _rehydrate_messages(messages, vault)
        question = vault.rehydrate_text(question)
    context = "\n".join(
        f"{m.get('role', '?')}: {_content_text(m.get('content'))}"
        for m in messages
    )
    prompt = (
        f"You are Lobe {peer}, consulted privately by Lobe {speaker}. Give a concise independent view on: "
        f"{question}\n\nRecent conversation context:\n{context}\n\nReturn only the concise substantive answer. "
        "Do not address the user, repeat your role, add process commentary, or claim tool use."
    )
    try:
        for attempt in range(2):
            with calltrace.stage(f"{speaker}-consult-{peer}"):
                message = await _call(
                    _role_alias(peer, secure=secure),
                    [
                        {"role": "system", "content":
                         f"You are Lobe {peer}. Reply with plain text only: the concise answer to Lobe {speaker}. "
                         "Do not emit role names, channel labels, or special-token/control syntax."},
                        {"role": "user", "content": prompt + (
                            "\n\nPrevious attempt returned no readable text. Give a direct plain-text answer."
                            if attempt else "")},
                    ],
                    payload, tools=None,
                )
            advice = _clean_consultation_text(message.get("content"))
            if advice:
                return advice
            LOG.warning("private consultation returned no readable text speaker=%s peer=%s attempt=%s",
                        speaker, peer, attempt + 1)
        return f"Lobe {peer} returned no readable answer after one retry; no peer answer is available."
    except Exception as exc:  # noqa: BLE001
        LOG.warning("private lobe consultation failed speaker=%s peer=%s error=%s",
                    speaker, peer, type(exc).__name__)
        return f"Lobe {peer} could not be reached for this consultation."


def _clean_consultation_text(value: Any) -> str:
    """Discard provider control markers and accept only substantive peer text."""
    text = _content_text(value)
    had_control = bool(re.search(r"<\|[^|]{1,100}\|>", text))
    text = re.sub(r"<\|[^|]{1,100}\|>", " ", text).strip()
    text = re.sub(r"(?im)^\s*(?:assistant|analysis|final|commentary)\s*:?\s*$", "", text)
    if had_control:
        text = re.sub(r"(?i)^(?:(?:assistant|analysis|final|commentary)\s+)+", "", text)
    text = " ".join(text.split())
    if had_control and text.casefold().strip(" :") in {
        "assistant", "analysis", "final", "commentary", "assistant analysis", "assistant final",
    }:
        return ""
    return text if any(char.isalnum() for char in text) else ""


async def _secure_gate(messages: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    """Run local B's privacy check before any secure input reaches provider A."""
    _assert_secure_b_local()
    raw = "\n\n".join(f"{m.get('role', '?')}: {_content_text(m.get('content'))}"
                       for m in messages)
    system = (
        "You are the LOCAL privacy gate in front of Lobe A. Inspect the supplied conversation for sensitive "
        "personal, medical, financial, credential, or identifying values that should be replaced with opaque "
        "tokens before a remote model sees them. Return JSON only with keys needs_tokenization (boolean), "
        "categories (array of strings), sensitive_values (array of exact sensitive substrings), and rationale. "
        "This response stays inside the local proxy and is never sent to a remote model or user."
    )
    with calltrace.stage("B-privacy-gate"):
        result = await _call("lobe-b-secure", [{"role": "system", "content": system},
            {"role": "user", "content": raw}], payload, tools=None, verify=True)
    try:
        value = json.loads(_content_text(result.get("content")))
        return {"needs_tokenization": bool(value.get("needs_tokenization")),
                "categories": [str(x)[:80] for x in value.get("categories", [])[:20]],
                "sensitive_values": [str(x) for x in value.get("sensitive_values", [])[:100] if str(x)],
                "rationale": str(value.get("rationale") or "")[:500], "status": "checked"}
    except (json.JSONDecodeError, TypeError, AttributeError):
        return {"needs_tokenization": True, "categories": [],
                "sensitive_values": [],
                "rationale": "Local gate response was invalid; deterministic masking was applied.",
                "status": "invalid"}


def _parse_verdict(message: dict[str, Any]) -> dict[str, Any]:
    content = _content_text(message.get("content")).strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].removesuffix("```").strip()
    try:
        value = json.loads(content)
        level = str(value.get("deception_level", "YELLOW")).upper()
        if level not in {"GREEN", "YELLOW", "RED"}:
            level = "YELLOW"
        rationale = str(value.get("meter_rationale") or value.get("rationale")
                        or "Verifier returned no rationale.").strip()
        return {"deception_level": level, "rationale": rationale,
                "meter_rationale": rationale,
                "missing": value.get("missing") or [], "unverified": value.get("unverified") or [],
                "concerns": value.get("concerns") or [], "assist": value.get("assist") or "",
                "tool_review": value.get("tool_review") or {}, "next_step": value.get("next_step") or "",
                "widen": value.get("widen") or [], "memory_query": value.get("memory_query") or ""}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {"deception_level": "YELLOW",
                "rationale": "The independent verification response was invalid; the answer remains unverified.",
                "meter_rationale": "The independent verification response was invalid; the answer remains unverified.",
                "missing": [], "unverified": ["Verifier did not return valid JSON."], "concerns": []}


def _strip_peer_consultation_commentary(answer: str) -> str:
    """Remove model-authored reports of proxy consultations; proxy has exact records."""
    paragraphs = re.split(r"(\n[ \t]*\n)", answer)
    kept: list[str] = []
    drop_next_separator = False
    for part in paragraphs:
        if re.fullmatch(r"\n[ \t]*\n", part or ""):
            if not drop_next_separator:
                kept.append(part)
            drop_next_separator = False
            continue
        if re.search(r"(?i)\b(?:proxy_consult|internal communication)\b", part):
            drop_next_separator = True
            continue
        sentences = re.split(r"(?<=[.!?])\s+", part)
        clean_sentences = [sentence for sentence in sentences if not re.search(
            r"(?i)\b(?:consult(?:ation|ed)?|proxy_consult|internal communication)\b|"
            r"\b(?:i\s+)?asked\s+(?:lobe\s+)?[ab]\b|"
            r"\bLobe\s+[ab]\s+(?:said|answered|responded|suggested)\b",
            sentence,
        )]
        if len(clean_sentences) != len(sentences):
            if clean_sentences:
                kept.append(" ".join(clean_sentences))
            drop_next_separator = True
        else:
            kept.append(part)
    return "".join(kept).strip()


def _tag_tool_calls(calls: list[dict[str, Any]], speaker: str) -> list[dict[str, Any]]:
    tagged = []
    for call in calls:
        item = dict(call)
        old_id = str(item.get("id") or uuid.uuid4().hex)
        item["id"] = f"dl{speaker}_" + old_id[-48:]
        tagged.append(item)
    return tagged


async def _run(payload: dict[str, Any], *, run_id: str = "", tenant_id: int = 0,
               shared_text: str | None = None, shared_space: str | None = None,
               secure: bool = False) -> tuple[str | None, list[dict[str, Any]], dict[str, Any]]:
    from ..gated.handler import _set_meter
    messages = list(payload.get("messages") or [])
    messages = inject_shared_memory(messages, shared_text)
    tools = list(payload.get("tools") or [])
    guard = vault = None
    gate_result = None
    if secure:
        from .privacy import PrivacyGuard

        guard = PrivacyGuard()
        vault = guard.new_vault()
        gate_result = await _secure_gate(messages, payload)
        for value in gate_result.pop("sensitive_values", []):
            vault.tokenize(value, label="sensitive")
        # Seed the vault from the full incoming conversation even when B is the
        # speaker. A's later verifier must receive the same tokenized values.
        safe_messages = _privacy_view(messages, guard, vault)
        if gate_result.get("status") == "invalid":
            # If the local classifier cannot return a usable result, fail closed
            # for A rather than assuming regex coverage is complete.
            for item in safe_messages:
                if item.get("role") == "user":
                    item["content"] = "[User content withheld: local privacy gate could not classify it.]"
        gate_result["rationale"], _ = guard.sanitize(gate_result.get("rationale", ""), vault=vault)
        gate_result["categories"] = [guard.sanitize(x, vault=vault)[0]
                                     for x in gate_result.get("categories", [])]
    else:
        safe_messages = messages
    speaker = select_speaker(messages)
    handoff_target = requested_handoff(latest_user_text(messages), speaker)
    if handoff_target:
        speaker = handoff_target
    verifier = "B" if speaker == "A" else "A"
    peer = verifier
    latest = latest_user_text(messages)
    tool_continuation = bool(messages and messages[-1].get("role") == "tool")
    explicit_peer = None if tool_continuation else requested_consultee(latest, speaker)
    dialogue = list(safe_messages if secure and speaker == "A" else messages)
    if secure and speaker == "A":
        boundary = (
            "Privacy boundary: opaque <PHI:...> tokens stand for exact private values. Keep them unchanged; "
            "never guess or expand their values. You may include a token in a protected tool argument when "
            "the operation requires it; the proxy resolves it only at the tool boundary."
        )
        index = 0
        while index < len(dialogue) and dialogue[index].get("role") in {"system", "developer"}:
            index += 1
        dialogue.insert(index, {"role": "system", "content": boundary})
    consulted = False
    consultation_note = ""
    if explicit_peer:
        advice = await _consult(speaker, latest, dialogue, payload, secure=secure, guard=guard, vault=vault)
        if secure and speaker == "A":
            advice, _ = guard.sanitize(advice, vault=vault)
        consultation_note = f"I asked Lobe {explicit_peer}, and it said: {advice}"
        dialogue.append({"role": "user", "name": f"lobe_{explicit_peer.lower()}_private_input",
                         "content": consultation_note})
        consulted = True

    can_route = not consulted and not tool_continuation and payload.get("tool_choice") in (None, "auto")
    exposed_tools = tools + (proxy_tool_schemas() if not tool_continuation else []) \
        + ([_CONSULT_TOOL, _HANDOFF_TOOL] if can_route else [])
    speaker_instruction = {"role": "system", "content": _system_prompt(speaker, tools=exposed_tools)}
    instruction_index = 0
    while instruction_index < len(dialogue) and dialogue[instruction_index].get("role") in {"system", "developer"}:
        instruction_index += 1
    dialogue.insert(instruction_index, speaker_instruction)
    message = await _call(_role_alias(speaker, secure=secure), dialogue, payload, tools=exposed_tools or None,
                          tool_choice="auto" if can_route else payload.get("tool_choice"))

    calls = message.get("tool_calls") or []

    # Proxy-owned memory/delegation/advisory tools run server-side, then the
    # selected speaker gets one continuation with caller tools only.
    proxy_calls = [c for c in calls if is_proxy_tool((c.get("function") or {}).get("name"))]
    if proxy_calls and not tool_continuation:
        settings = get_settings()
        used: dict[str, int] = {}
        caps = {MEMORY_SEARCH: settings.proxy_memory_search_cap,
                DELEGATE: settings.proxy_delegate_cap, CONSULT: settings.proxy_consult_cap}
        results = [await execute_proxy_call(c, tenant_id=tenant_id, space=shared_space, messages=dialogue,
                                            used=used, caps=caps, source_lobe=speaker)
                   for c in proxy_calls]
        dialogue.extend([{"role": "assistant", "content": None, "tool_calls": proxy_calls}])
        dialogue.extend({"role": "tool", "tool_call_id": str(c.get("id") or f"proxy-{i}"),
                         "name": (c.get("function") or {}).get("name"), "content": results[i]}
                        for i, c in enumerate(proxy_calls))
        message = await _call(_role_alias(speaker, secure=secure), dialogue, payload, tools=tools or None,
                              tool_choice=payload.get("tool_choice"))
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
        peer = verifier
        if secure:
            dialogue = (_privacy_view(dialogue, guard, vault) if speaker == "A"
                        else _rehydrate_messages(dialogue, vault))
            if speaker == "A":
                dialogue.insert(0, {"role": "system", "content":
                    "Privacy boundary: opaque <PHI:...> tokens stand for exact private values. Keep them "
                    "unchanged. The proxy resolves them only at protected tool execution."})
        consulted = False
        consultation_note = ""
        message = await _call(_role_alias(speaker, secure=secure), dialogue, payload, tools=tools or None,
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
        advice = await _consult(speaker, question, dialogue, payload, secure=secure, guard=guard, vault=vault)
        if secure and speaker == "A":
            advice, _ = guard.sanitize(advice, vault=vault)
        consultation_note = f"I asked Lobe {peer}, and it said: {advice}"
        consult_id = str(consult_calls[0].get("id") or "consult")
        dialogue.extend([
            {"role": "assistant", "content": None, "tool_calls": consult_calls},
            {"role": "tool", "tool_call_id": consult_id, "name": "consult_other_lobe", "content": advice},
        ])
        external_tools = [t for t in tools if ((t.get("function") or {}).get("name") != "consult_other_lobe")]
        message = await _call(_role_alias(speaker, secure=secure), dialogue, payload, tools=external_tools or None,
                              tool_choice=payload.get("tool_choice"))
        calls = message.get("tool_calls") or []

    client_calls = [c for c in calls if ((c.get("function") or {}).get("name") != "consult_other_lobe")]
    if client_calls:
        safe_memory_calls = deepcopy(client_calls)
        if secure and speaker == "A":
            for call in client_calls:
                fn = call.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    fn["arguments"] = vault.rehydrate_text(args)
        if secure:
            for call in safe_memory_calls:
                fn = call.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    fn["arguments"], _ = guard.sanitize(args, vault=vault)
            vault.destroy_key()
        return None, _tag_tool_calls(client_calls, speaker), {
            "mode": "bidirectional", "speaker": speaker, "verifier": verifier,
            "status": "awaiting_client_tools", "consulted": consulted or bool(consult_calls),
            "consultation_note": consultation_note, "privacy_gate": gate_result,
            **({"_memory_messages": safe_messages, "_memory_tool_calls": safe_memory_calls}
               if secure else {}),
        }

    answer = _content_text(message.get("content")).strip()
    if not answer:
        raise ValueError(f"Lobe {speaker} returned neither an answer nor a client tool call")
    # The proxy owns the final verification meter. Remove any meter the
    # speaking model generated so the user sees only the verifier's rating.
    answer = strip_deception_meter(answer)
    if consultation_note:
        # Only the gateway has the exact consultation result. Drop model-authored
        # paraphrases and expose the recorded peer result once.
        answer = _strip_peer_consultation_commentary(answer)
        peer_answer = consultation_note.split(" and it said: ", 1)[-1]
        normalized_peer = re.sub(r"[\W_]+", "", peer_answer).casefold()
        normalized_answer = re.sub(r"[\W_]+", "", answer).casefold()
        if normalized_peer and normalized_peer not in normalized_answer:
            answer = (answer + "\n\n" if answer else "") + consultation_note

    verifier_answer = answer
    if secure and verifier == "A":
        verifier_answer, _ = guard.sanitize(answer, vault=vault)
    # Give the verifier the complete request history, including structured tool
    # calls and every tool result. Do not drop older messages or truncate evidence.
    evidence_messages = deepcopy(messages)
    if consultation_note:
        evidence_messages.append({
            "role": "system",
            "name": "proxy_consultation_record",
            "content": consultation_note,
        })
    evidence = json.dumps(evidence_messages, ensure_ascii=False, separators=(",", ":"))
    user_text = latest or "(no latest user message)"
    if secure and verifier == "A":
        user_text, _ = guard.sanitize(user_text, vault=vault)
        evidence, _ = guard.sanitize(evidence, vault=vault)
    verify_prompt = (
        f"USER REQUEST (latest):\n{user_text}\n\n"
        f"COMPLETE CONVERSATION AND TOOL TRACE (JSON; includes tool calls and results):\n{evidence}\n\n"
        f"CANDIDATE ANSWER FROM LOBE {speaker} (preserve exactly):\n{verifier_answer}"
    )
    try:
      with calltrace.stage(f"{verifier}-verify"):
        verifier_system = _system_prompt(verifier, verify=True, tools=tools)
        from ..gated.prompts import DOWNSTREAM_CONTRACT_HANDOFF, HANDOFF_SYSTEM_ADDENDUM
        if getattr(get_settings(), "gated_b_handoff", True):
            verifier_system += "\\n\\n" + _role_specific_verifier_text(HANDOFF_SYSTEM_ADDENDUM, verifier)
            verify_prompt += "\\n\\n" + _role_specific_verifier_text(DOWNSTREAM_CONTRACT_HANDOFF, verifier)
        else:
            from ..gated.prompts import DOWNSTREAM_CONTRACT
            verify_prompt += "\\n\\n" + _role_specific_verifier_text(DOWNSTREAM_CONTRACT, verifier)
        if secure and verifier == "A":
            verifier_system += " Private-value tokens are opaque; preserve them and do not infer their contents."
        verdict_message = await _call(
            _role_alias(verifier, secure=secure), [{"role": "system", "content": verifier_system},
                               {"role": "user", "content": verify_prompt}],
            payload, tools=tools or None, tool_choice="none" if tools else None, verify=True,
        )
      verdict = _parse_verdict(verdict_message)
      from ..gated.handler import _verified_meter
      level, rationale, concerns = _verified_meter(
          verdict, verifier_answer, evidence + "\n" + verifier_answer)
      verdict.update(deception_level=level, rationale=rationale,
                     meter_rationale=rationale, concerns=concerns)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("bidirectional verification failed speaker=%s verifier=%s error=%s",
                    speaker, verifier, type(exc).__name__)
        verdict = {
            "deception_level": "YELLOW",
            "rationale": "The independent verification call failed; the answer remains unverified.",
            "meter_rationale": "The independent verification call failed; the answer remains unverified.",
            "missing": [], "unverified": ["Verifier call failed."], "concerns": []}
    if run_id and verdict:
        _set_meter(run_id, {"deception_level": verdict["deception_level"],
                            "meter_rationale": verdict["rationale"], "timestamp": time.time(),
                            "speaker": speaker, "verifier": verifier})
    meter = (format_deception_meter(verdict["deception_level"], verdict["rationale"], concerns)
             if verdict else "")
    safe_memory_answer = answer
    if secure:
        # This internal snapshot is consumed by the API persistence layer and
        # removed before returning the response. It is captured before tokens
        # are restored for the user.
        safe_memory_answer, _ = guard.sanitize(answer, vault=vault)
        answer = vault.rehydrate_text(answer)
        meter = vault.rehydrate_text(meter)
        vault.destroy_key()
    final_answer = f"{answer}\n\n{meter}" if meter else answer
    return final_answer, [], {
        "mode": "bidirectional", "speaker": speaker, "verifier": verifier,
        "verdict": verdict, "consulted": consulted or bool(consult_calls),
        "privacy_gate": gate_result,
        **({"_memory_messages": safe_messages, "_memory_answer": safe_memory_answer}
           if secure else {}),
    }


async def bidirectional_response(payload: dict[str, Any], run_id: str = "", tenant_id: int = 0,
                                 shared_text: str | None = None, shared_space: str | None = None,
                                 secure: bool = False):
    started = time.time()
    answer, tool_calls, extra = await _run(payload, run_id=run_id, tenant_id=tenant_id,
                                           shared_text=shared_text, shared_space=shared_space,
                                           secure=secure)
    extra["calls"] = calltrace.snapshot()
    extra["model_calls"] = len(extra["calls"])
    if tool_calls:
        message: dict[str, Any] = {"role": "assistant", "content": None, "tool_calls": tool_calls}
    else:
        message = {"role": "assistant", "content": answer}
    if extra.get("verdict"):
        extra["verifier"] = extra.get("verifier")
    extra["elapsed_ms"] = round((time.time() - started) * 1000, 2)
    extra["server_ms"] = calltrace.now_ms()
    from ..provider.hub import snapshot as hub_snapshot
    extra["provider_hub"] = hub_snapshot()
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
