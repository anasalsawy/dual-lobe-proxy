"""Dual-lobe mode handler — isolated from the gated and director paths.

Flow:
  1. Build the injected slice from this run's ring buffer.
  2. Add the reserved memory tool to A's tool list (host keeps its own tools).
  3. A produces a working response or requests a real host tool.
  4. B reviews the working response and bounded A/B collaboration happens before
     release.
  5. A produces one clean final response.
  6. B verifies that final response; the meter is attached afterward.
  7. The structured A/B exchange is returned separately for visible UI rendering
     and its summary is stored for the next request.

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
from .loop import (build_final_context, build_slice_text, rate_answer,
                   render_exchange, run_exchange)
from .store import DualLobeStore
from .threeway import latest_intervention_seq

LOG = logging.getLogger("dual_lobe.dl")

_INLINE_PREFIX = "Dual-Lobe collaboration\n\n"
_INLINE_FINAL_MARKER = "\n\n──────── FINAL ANSWER ────────\n"


def _strip_display_exchange_from_history(messages: list[dict]) -> list[dict]:
    """Remove our display-only transcript when an ordinary client echoes it back.

    This lets generic OpenAI-compatible clients show the collaboration inline
    while keeping the semantic history presented to A/B equivalent to one clean
    final assistant turn.
    """
    cleaned: list[dict] = []
    for original in messages:
        msg = dict(original)
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
            content = msg["content"]
            if content.startswith(_INLINE_PREFIX) and _INLINE_FINAL_MARKER in content:
                msg["content"] = content.split(_INLINE_FINAL_MARKER, 1)[1]
        cleaned.append(msg)
    return cleaned


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


def split_tool_calls(tool_calls: list[dict], reserved_enabled: bool = True) -> tuple[list[dict], list[dict]]:
    """Separate host calls from proxy-private calls without hijacking collisions."""
    reserved, host = [], []
    for call in tool_calls or []:
        name = (call.get("function") or {}).get("name")
        if reserved_enabled and name == prompts.MEMORY_TOOL_NAME:
            reserved.append(call)
        else:
            host.append(call)
    return reserved, host


async def _resolve_memory_call(store: DualLobeStore, call: dict, settings) -> str:
    arguments = (call.get("function") or {}).get("arguments") or "{}"
    try:
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("arguments must be a JSON object")
    except Exception:
        return json.dumps({"error": "invalid arguments; expected a JSON object"})

    query = str(parsed.get("query", "") or "").strip().casefold()
    limit = parsed.get("limit")
    limit = limit if isinstance(limit, int) else settings.dual_lobe_slice_entries
    limit = max(1, min(20, limit))

    # The ring is intentionally simple and deterministic: lexical retrieval over
    # recent run-scoped entries, with recency as the tie breaker.  This makes the
    # reserved tool an actual search instead of silently ignoring its query.
    pool_limit = min(200, max(limit * 8, settings.dual_lobe_slice_entries))
    entries = await store.newest(pool_limit)
    if query:
        terms = [term for term in query.replace("_", " ").split() if len(term) > 1]
        ranked = []
        for recency, entry in enumerate(entries):
            haystack = json.dumps(entry, ensure_ascii=False).casefold()
            phrase_hits = haystack.count(query)
            term_hits = sum(haystack.count(term) for term in terms)
            score = phrase_hits * 10 + term_hits
            if score:
                ranked.append((score, -recency, entry))
        ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        entries = [entry for _, _, entry in ranked[:limit]]
    else:
        entries = entries[:limit]

    return json.dumps({"entries": entries, "count": len(entries), "query": query},
                      ensure_ascii=False)


def _meter_line(level: str, rationale: str, concerns: list[dict], assist: str = "",
                available: bool = True) -> str:
    if not available or level == "UNAVAILABLE":
        return "\n\n> ⚪ **Deception Meter: unavailable**\n> Verification could not be completed."
    line = f"\n\n> ⚠️ **Deception Meter: {level}**"
    if rationale and rationale != "No deception detected.":
        line += f"\n> {rationale[:150]}"
    if level == "RED":
        for concern in (concerns or [])[:3]:
            line += (f"\n> ⚠️ \"{str(concern.get('claim_quote', ''))}\" — "
                     f"{str(concern.get('reason', ''))} — evidence: "
                     f"\"{str(concern.get('evidence_quote', ''))}\"")
    assist_text = str(assist or "").strip()
    if assist_text:
        line += f"\n> 💡 {assist_text}"
    return line


def _strip_reserved_from_message(message: dict, host_calls: list[dict]) -> None:
    """Ensure the proxy-private memory tool is never exposed to the host."""
    if host_calls:
        message["tool_calls"] = host_calls
    else:
        message.pop("tool_calls", None)


async def _call_a(a_adapter, *, messages: list[dict], payload: dict[str, Any],
                  tools: list[dict] | None, settings) -> dict:
    return response_dict(await asyncio.wait_for(
        a_adapter.buffered(NormalizedRequest(
            messages=messages,
            temperature=payload.get("temperature"),
            max_tokens=payload.get("max_tokens"),
            top_p=payload.get("top_p"),
            tools=tools or None,
            tool_choice=payload.get("tool_choice") if tools else None,
            parallel_tool_calls=False if tools else None,
            stream=False,
            timeout=settings.a_timeout,
        )),
        timeout=settings.a_timeout,
    ))


async def _emit_final_event(event_sink, run_id: str, final_answer: str) -> None:
    if event_sink is None:
        return
    event = {
        "type": "final_message", "actor": "A", "stage": "final",
        "run_id": run_id, "content": final_answer,
    }
    try:
        result = event_sink(event)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        LOG.warning("dual-lobe final event sink failed run=%s: %s", run_id, exc)


async def dual_lobe_response(payload: dict[str, Any], run_id: str, tenant_id: int,
                            public_model: str, task: str = "",
                            event_sink=None) -> tuple[dict, dict, Any]:
    """Handle one dual-lobe request.

    Returns (response_data, headers, background_callable). Pre-final collaboration
    is completed synchronously; the background callable is therefore None.
    """
    settings = get_settings()
    if not settings.dual_lobe_enabled or not settings.dual_lobe_tenant_enabled:
        raise RuntimeError("Dual-lobe mode is disabled.")

    messages = _strip_display_exchange_from_history(payload["messages"])
    store = DualLobeStore(tenant_id, run_id)
    three_way = bool(payload.get("dual_lobe_three_way", False))
    intervention_cursor = 0
    if three_way:
        try:
            intervention_cursor = await latest_intervention_seq(tenant_id, run_id)
        except Exception as exc:
            LOG.warning("three-way cursor initialization failed run=%s: %s", run_id, exc)

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

    injections = [_OBSERVATION_NOTE, prompts.WORKING_INSTRUCTIONS]
    if slice_text:
        injections.append(slice_text)
    enriched = _insert_system_injections(messages, injections)

    # ── 2. Offer the reserved memory tool ────────────────────────────────────
    host_tool_names = {
        (tool.get("function") or {}).get("name") for tool in (payload.get("tools") or [])
    }
    reserved_tool_active = (
        settings.dual_lobe_memory_tool and prompts.MEMORY_TOOL_NAME not in host_tool_names
    )
    tools = _add_memory_tool(payload.get("tools"), settings.dual_lobe_memory_tool)
    a_adapter = get_registry().adapter("lobe-a")

    # ── 3/4. Call A, answering the reserved tool in-process if used ──────────
    try:
        data = await _call_a(a_adapter, messages=enriched, payload=payload,
                             tools=tools, settings=settings)
    except Exception as exc:
        LOG.error("dual-lobe A call failed run=%s: %s", run_id, exc)
        return ({"error": {"type": "upstream_error", "message": "Upstream request failed."}},
                {"X-Dual-Lobe-Mode": "error"}, None)

    message = ((data.get("choices") or [{}])[0].get("message") or {})

    # Resolve proxy-private memory calls in-process.  Keep this bounded so a
    # misbehaving model cannot loop forever on the reserved tool.
    max_internal_memory_hops = 3
    internal_hops = 0
    while True:
        reserved, host_calls = split_tool_calls(message.get("tool_calls"), reserved_tool_active)

        # A real host tool request means there is no final answer yet.  Never run
        # the meter/background exchange against an empty pre-tool response.  If
        # A mixed our private tool with host tools, strip the private call before
        # returning; the guaranteed injected memory slice still preserves the
        # core memory path for this turn.
        if host_calls:
            if reserved:
                LOG.warning(
                    "A mixed %d reserved memory call(s) with %d host call(s); "
                    "reserved calls were suppressed run=%s",
                    len(reserved), len(host_calls), run_id,
                )
                _strip_reserved_from_message(message, host_calls)
            data["model"] = public_model
            return data, {
                "X-Dual-Lobe-Mode": "on",
                "X-Dual-Lobe-Meter": "PENDING",
                "X-Dual-Lobe-Slice-Entries": str(len(entries)),
                "X-Dual-Lobe-Round-Cap": str(settings.dual_lobe_rounds),
            }, None

        if not reserved:
            break

        internal_hops += 1
        if internal_hops > max_internal_memory_hops:
            LOG.warning("dual-lobe reserved memory tool hop limit reached run=%s", run_id)
            _strip_reserved_from_message(message, [])
            break

        results = []
        for call in reserved:
            results.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": await _resolve_memory_call(store, call, settings),
            })
        follow_up = enriched + [message] + results
        try:
            data = await _call_a(a_adapter, messages=follow_up, payload=payload,
                                 tools=tools, settings=settings)
            message = ((data.get("choices") or [{}])[0].get("message") or {})
            enriched = follow_up
            LOG.info("dual-lobe resolved %d reserved tool call(s) run=%s",
                     len(reserved), run_id)
        except Exception as exc:
            LOG.warning("dual-lobe follow-up A call failed run=%s: %s", run_id, exc)
            # Do not leak a private call when its internal resolution failed.
            _strip_reserved_from_message(message, [])
            break

    answer = str(message.get("content") or "")

    # No visible answer means there is nothing meaningful to rate or exchange.
    if not answer.strip():
        data["model"] = public_model
        return data, {
            "X-Dual-Lobe-Mode": "on",
            "X-Dual-Lobe-Meter": "PENDING",
            "X-Dual-Lobe-Slice-Entries": str(len(entries)),
            "X-Dual-Lobe-Round-Cap": str(settings.dual_lobe_rounds),
        }, None

    # ── 5. Pre-final A/B collaboration ─────────────────────────────────────
    # The first A response is a working response.  It is not released as the
    # canonical assistant answer until B has reviewed it and A has finalized.
    try:
        exchange_result = await run_exchange(
            tenant_id, run_id, task, messages, answer, None, entries, settings,
            event_sink=event_sink, three_way=three_way,
            intervention_after_seq=intervention_cursor,
            intervention_grace_ms=int(payload.get("dual_lobe_three_way_grace_ms", 2500) or 0) if three_way else 0,
        )
    except Exception as exc:
        LOG.warning("dual-lobe pre-final exchange failed run=%s: %s", run_id, exc)
        exchange_result = {
            "exchange": [], "summary": "", "rounds": 0, "stop_reason": "exchange_error"
        }

    # ── 6. Clean final A response ────────────────────────────────────────────
    # Feed A the collaboration as private system context.  Host tools are not
    # offered during finalization: if evidence is still missing, A must say so
    # rather than manufacture a hidden execution step.
    final_context = build_final_context(answer, exchange_result)
    final_messages = _insert_system_injections(messages, [_OBSERVATION_NOTE, final_context])
    finalization = "ok"
    try:
        final_data = await _call_a(
            a_adapter, messages=final_messages, payload=payload, tools=None, settings=settings
        )
        final_message = ((final_data.get("choices") or [{}])[0].get("message") or {})
        final_answer = str(final_message.get("content") or "").strip()
        if not final_answer:
            raise ValueError("final A response was empty")
        data = final_data
        message = final_message
    except Exception as exc:
        LOG.warning("dual-lobe finalization failed run=%s; using working answer: %s", run_id, exc)
        finalization = "fallback-working"
        final_answer = answer
        # Reuse the original A completion, but never leak private memory calls.
        data = data
        message = ((data.get("choices") or [{}])[0].get("message") or {})
        message["content"] = final_answer

    await _emit_final_event(event_sink, run_id, final_answer)

    # ── 7. B verifies the FINAL answer ───────────────────────────────────────
    meter = await rate_answer(tenant_id, run_id, messages, final_answer, settings)

    # Assist remains machine-readable verifier output in collaboration mode.
    # The useful B material has already had a chance to change Final A, so we do
    # not append a second, potentially contradictory assist line to the user.
    line = _meter_line(
        meter["deception_level"], meter.get("meter_rationale", ""),
        meter.get("concerns", []), "", meter.get("available", True),
    )
    for choice in data.get("choices", []):
        msg = choice.get("message", {})
        if msg.get("content"):
            msg["content"] = str(msg["content"]) + line

    # Keep normal chat history clean: message.content is Final A (+ meter).
    # A UI can render the collaboration before it using this structured extension
    # or the live event_sink.  No speaker-labelled transcript is folded into the
    # canonical assistant turn.
    exchange = exchange_result.get("exchange") or []
    data["dual_lobe"] = {
        "phase": "pre_final",
        "visible": True,
        "three_way": three_way,
        "exchange": exchange,
        "rendered_exchange": render_exchange(exchange),
        "rounds": exchange_result.get("rounds", 0),
        "stop_reason": exchange_result.get("stop_reason", ""),
        "summary": exchange_result.get("summary", ""),
        "working_answer": answer,
        "final_answer": final_answer,
        "meter": meter,
    }

    # Optional compatibility display for clients that cannot render the structured
    # extension yet.  It is OFF by default because inlining the transcript into
    # message.content would pollute the next ordinary chat turn.
    if payload.get("dual_lobe_inline_exchange", True):
        rendered = render_exchange(exchange)
        if rendered:
            combined = (
                _INLINE_PREFIX + rendered + _INLINE_FINAL_MARKER + final_answer + line
            )
            for choice in data.get("choices", []):
                msg = choice.get("message", {})
                if msg.get("content") is not None:
                    msg["content"] = combined

    data["model"] = public_model
    headers = {
        "X-Dual-Lobe-Mode": "pre-final",
        "X-Dual-Lobe-Exchange": "visible-structured",
        "X-Dual-Lobe-Three-Way": "on" if three_way else "off",
        "X-Dual-Lobe-Exchange-Rounds": str(exchange_result.get("rounds", 0)),
        "X-Dual-Lobe-Stop-Reason": str(exchange_result.get("stop_reason", ""))[:80],
        "X-Dual-Lobe-Finalization": finalization,
        "X-Dual-Lobe-Meter": meter["deception_level"] if meter.get("available", True) else "UNAVAILABLE",
        "X-Dual-Lobe-Meter-Rationale": str(meter.get("meter_rationale", ""))[:300],
        "X-Dual-Lobe-Slice-Entries": str(len(entries)),
        "X-Dual-Lobe-Round-Cap": str(settings.dual_lobe_rounds),
    }

    # No post-answer A/B conversation: the substantive exchange already happened
    # before Final A.  Its summary has been persisted for the next turn.
    return data, headers, None


async def dual_lobe_event_stream(payload: dict[str, Any], run_id: str, tenant_id: int,
                                 public_model: str, task: str = ""):
    """Yield live dual-lobe events followed by the final buffered result.

    The underlying inference remains authoritative in ``dual_lobe_response``.
    This wrapper simply bridges its ``event_sink`` into an async iterator so an
    API transport can forward A/B collaboration events while inference is still
    running.  The payload is copied and forced to keep the canonical final
    assistant message clean; transports may render the exchange separately.

    Yields dictionaries of the form::

        {"kind": "event", "event": {...}}
        {"kind": "result", "data": {...}, "headers": {...}}

    Cancellation propagates to the in-flight inference task.
    """
    queue: asyncio.Queue = asyncio.Queue()
    stream_payload = copy.deepcopy(payload)
    stream_payload["dual_lobe_inline_exchange"] = False

    async def sink(event: dict[str, Any]) -> None:
        await queue.put(("event", event))

    async def worker() -> None:
        try:
            data, headers, _ = await dual_lobe_response(
                stream_payload, run_id, tenant_id, public_model, task=task,
                event_sink=sink,
            )
            await queue.put(("result", (data, headers)))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await queue.put(("error", exc))

    task_obj = asyncio.create_task(worker(), name=f"dual-lobe-stream:{run_id}")
    try:
        while True:
            kind, value = await queue.get()
            if kind == "event":
                yield {"kind": "event", "event": value}
                continue
            if kind == "error":
                raise value
            data, headers = value
            yield {"kind": "result", "data": data, "headers": headers}
            return
    finally:
        if not task_obj.done():
            task_obj.cancel()
        try:
            await task_obj
        except asyncio.CancelledError:
            pass


_OBSERVATION_NOTE = (
    "[OBSERVER SYSTEM NOTE — Do not reference, acknowledge, or respond to this message. "
    "It is context from an anti-deception observer, not a user message.]\n"
    "You are responding through an inference proxy with an anti-deception observer. "
    "Distinguish intended, attempted, observed, and confirmed work. "
    "Do not invent execution, tests, citations, or completion. "
    "Support completion claims with relevant evidence in the conversation. "
    "Correct earlier claims when newer evidence contradicts them."
)
