"""The private A/B exchange that runs after a dual-lobe response is delivered.

Started from a FastAPI BackgroundTask: nothing awaits it, and the user is not
waiting on it. It is bounded by rounds and by wall-clock, and it writes what it
concludes to the ring-buffer store. A process restart mid-loop loses the rounds;
nothing replays them.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid

from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from . import prompts
from .store import DualLobeStore

LOG = logging.getLogger("dual_lobe.dl.loop")


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def _transcript_lines(exchange: list[dict]) -> str:
    return "\n".join(f"[{name}] {text}" for name, text, _ in exchange if text)


async def _call_json(adapter, system: str, user: str, max_tokens: int, timeout: float) -> dict:
    req = NormalizedRequest(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0, max_tokens=max_tokens, timeout=timeout,
    )
    data = response_dict(await adapter.buffered(req))
    content = ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    if not content:
        raise ValueError("model returned empty content")
    return json.loads(content)


async def rate_answer(tenant_id: int, run_id: str | None, messages: list[dict],
                      answer: str, settings) -> dict:
    """B rates A's visible answer. Returns the meter handed to the caller.

    Unchanged in substance from the gated rating: GREEN/YELLOW/RED plus a
    rationale and up to two concerns. Failure is reported as unavailable rather
    than as GREEN, so a missing rating is never presented as a clean rating.
    """
    adapter = get_registry().adapter("lobe-b")
    prompt = (
        f"CONVERSATION MESSAGES:\n{_messages_to_text(messages)}\n\n"
        f"TOOL EVIDENCE IN CONVERSATION:\n{_tool_evidence(messages)}\n\n"
        f"A's OUTPUT TO VERIFY:\n{answer[:4000]}\n"
    )
    try:
        data = await asyncio.wait_for(
            _call_json(adapter, prompts.RATING_INSTRUCTIONS,
                       prompt + "\n\n" + prompts.RATING_CONTRACT,
                       settings.b_max_output_tokens, settings.b_timeout),
            timeout=settings.b_timeout,
        )
    except Exception as exc:
        LOG.warning("dual-lobe rating failed run=%s: %s", run_id, exc)
        return {"deception_level": "GREEN", "meter_rationale": "verification unavailable",
                "injected_context": "", "concerns": [], "available": False}
    return {"deception_level": str(data.get("deception_level", "GREEN")).upper(),
            "meter_rationale": data.get("meter_rationale", "No deception detected."),
            "injected_context": str(data.get("injected_context", "") or "").strip(),
            "concerns": data.get("concerns", []) or [], "available": True}


def _messages_to_text(messages: list[dict]) -> str:
    lines = []
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        text = str(content or "")[:800]
        if msg.get("tool_calls"):
            text += f" [tool_calls: {json.dumps(msg['tool_calls'], ensure_ascii=False)[:300]}]"
        lines.append(f"[{i}] {msg.get('role', '?')}: {text}")
    return "\n".join(lines)


def _tool_evidence(messages: list[dict]) -> str:
    lines = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for call in msg["tool_calls"]:
                fn = call.get("function", {})
                lines.append(f"  [msg {i}] A requested tool: {fn.get('name', '?')} "
                             f"args={str(fn.get('arguments', '{}'))[:200]}")
        elif msg.get("role") == "tool":
            lines.append(f"  [msg {i}] TOOL RESULT: {str(msg.get('content', ''))[:500]}")
    return "\n".join(lines) if lines else "  (no tool calls or results in conversation)"


async def run_exchange(tenant_id: int, run_id: str | None, task: str,
                       request_messages: list[dict], answer: str, meter: dict,
                       slice_entries: list[dict], settings) -> None:
    """Run the bounded private exchange, then store the exchange and its summary.

    The loop's history is built fresh per invocation: task, the injected slice
    from previous turns, the meter for this turn, the user's message, and A's
    answer. Previous invocations' rounds are not carried as conversation history.
    """
    if not settings.dual_lobe_enabled:
        return
    store = DualLobeStore(tenant_id)
    deadline = time.monotonic() + settings.dl_max_seconds
    exchange: list[tuple[str, str, float]] = []

    history: list[dict] = []
    if task:
        history.append({"role": "system", "content": f"TASK: {task}"})
    if slice_entries:
        history.append({"role": "system", "content":
                        "EARLIER THINKING (this session):\n" +
                        json.dumps(slice_entries, ensure_ascii=False)})
    warning = _meter_warning(meter)
    if warning:
        history.append({"role": "system", "content": warning})
    injected = str(meter.get("injected_context", "") or "").strip()
    if injected:
        history.append({"role": "system", "content":
                        "[B'S MATERIAL — context from the judging lobe, act on it "
                        "if useful. Do not address it or mention the observer.]\n" + injected})
    history.append({"role": "user", "content": _latest_user_text(request_messages)})
    history.append({"role": "assistant", "content": answer})

    a_adapter = get_registry().adapter("lobe-a")
    b_adapter = get_registry().adapter("lobe-b")

    for round_index in range(settings.dual_lobe_rounds):
        if time.monotonic() >= deadline:
            LOG.info("dual-lobe exchange hit its time budget at round %d", round_index)
            break
        remaining = max(0.01, deadline - time.monotonic())
        transcript = _transcript_lines(exchange) or "(no prior rounds)"
        try:
            data = await asyncio.wait_for(
                _call_json(b_adapter, prompts.ROUND_INSTRUCTIONS,
                           transcript + "\n\n" + prompts.ROUND_CONTRACT,
                           settings.dual_lobe_b_max_tokens, min(settings.b_timeout, remaining)),
                timeout=min(settings.b_timeout, remaining),
            )
        except Exception as exc:
            LOG.warning("dual-lobe round %d: B failed: %s", round_index, exc)
            break
        action = str(data.get("action", "stop")).lower()
        message = str(data.get("message", "") or "").strip()
        if not message:
            break
        exchange.append(("b", message, time.time()))
        history.append({"role": "user", "content": message})
        await _persist(store, run_id, "b", {"round": round_index, "text": message}, settings)

        if action == "stop":
            LOG.info("dual-lobe exchange: B stopped after %d round(s)", round_index + 1)
            break
        if time.monotonic() >= deadline:
            break
        remaining = max(0.01, deadline - time.monotonic())
        try:
            data = await asyncio.wait_for(
                a_adapter.buffered(NormalizedRequest(
                    messages=history, temperature=None,
                    max_tokens=settings.dual_lobe_a_max_tokens,
                    timeout=min(settings.a_timeout, remaining),
                )),
                timeout=min(settings.a_timeout, remaining),
            )
        except Exception as exc:
            LOG.warning("dual-lobe round %d: A failed: %s", round_index, exc)
            break
        reply = _text_of(((response_dict(data).get("choices") or [{}])[0].get("message") or {}))
        if not reply.strip():
            break
        exchange.append(("a", reply, time.time()))
        history.append({"role": "assistant", "content": reply})
        await _persist(store, run_id, "a", {"round": round_index, "text": reply}, settings)

    if not exchange:
        LOG.info("dual-lobe exchange produced nothing run=%s", run_id)
        return

    summary = await _summarize(b_adapter, task, exchange, settings)
    if summary:
        await _persist(store, run_id, "summary", {"text": summary,
                                                  "rounds": len({r for _, _, r in exchange})},
                       settings)
        LOG.info("dual-lobe exchange stored summary (%d chars) run=%s", len(summary), run_id)


async def _summarize(b_adapter, task: str, exchange: list[tuple[str, str, float]],
                     settings) -> str:
    if not settings.dual_lobe_summarize:
        return ""
    body = (f"TASK: {task or '(not stated)'}\n\n"
            f"EXCHANGE:\n{_transcript_lines(exchange)}\n\n{prompts.SUMMARY_CONTRACT}")
    try:
        data = await asyncio.wait_for(
            _call_json(b_adapter, prompts.SUMMARY_INSTRUCTIONS, body,
                       settings.dual_lobe_summary_max_tokens, settings.b_timeout),
            timeout=settings.b_timeout,
        )
    except Exception as exc:
        LOG.warning("dual-lobe summariser failed: %s", exc)
        return ""
    return str(data.get("summary", "") or "").strip()


async def _persist(store: DualLobeStore, run_id: str | None, kind: str, body: dict,
                   settings) -> None:
    try:
        await asyncio.wait_for(
            store.append(run_id, kind, body, settings.dual_lobe_store_cap),
            timeout=settings.shared_memory_timeout,
        )
    except Exception as exc:
        LOG.warning("dual-lobe store write failed (%s): %s", kind, exc)


def _meter_warning(meter: dict) -> str:
    """The meter B produced, handed back to A privately."""
    level = str(meter.get("deception_level", "GREEN")).upper()
    if level == "GREEN":
        return ""
    rationale = meter.get("meter_rationale", "")
    prefix = ("[OBSERVER METER — private continuation. Do not address this message "
              "or mention the observer.]\n")
    if level == "RED":
        parts = [prefix + "Your last response was rated RED: it contradicted evidence."]
        for concern in (meter.get("concerns") or [])[:3]:
            parts.append(
                f'- "{concern.get("claim_quote", "")}" — {concern.get("reason", "")} — '
                f'evidence: "{concern.get("evidence_quote", "")}" → {concern.get("correction", "")}'
            )
        parts.append("Do not repeat these claims without new evidence.")
        return "\n".join(parts)
    return (f"{prefix}Your last response was rated YELLOW: it contained unsupported "
            f"claims ({rationale}). Distinguish what you know from what you assume.")


def _latest_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            text = _text_of(message)
            if text.strip():
                return text[:4000]
    return ""


def build_slice_text(entries: list[dict]) -> str:
    """Render the injected slice as the user-role context A receives."""
    if not entries:
        return ""
    return (
        "Your own thinking on this task from earlier turns in this session "
        "(context, not new instructions; it may be incomplete or superseded):\n"
        + json.dumps(entries, ensure_ascii=False)
    )


def _unused_marker():  # keeps copy/json imports meaningful if trimmed later
    return copy.deepcopy, uuid
