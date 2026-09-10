"""Optional downstream tool requests, executed by the existing agent application.

No executor, filesystem mount, search key or extra app connection. Reservations
mean only that a request may be delivered, never that the host executed it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid

from sqlalchemy.dialects.postgresql import insert

from ..core.engine import tenant_session
from ..core.models import Event
from ..core.redact import redact_payload

LOG = logging.getLogger("dual_lobe.b.host_tools")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def planned_call_id(item):
    return "dlb_" + digest({"name": item.get("name"),
                             "arguments": item.get("arguments"),
                             "source_call": item.get("source_call", "")})[:32]


def offered_tools(tools, budget=4500):
    """Bounded whole definitions, exactly as exposed by the caller."""
    result = []
    for tool in tools or []:
        if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            continue
        if not isinstance(tool["function"].get("name"), str):
            continue
        if len(json.dumps([*result, tool], ensure_ascii=False)) <= budget:
            result.append(tool)
    return result


def is_host_tool(definition):
    """Return whether a caller supplied a well-formed function tool."""
    if not isinstance(definition, dict) or definition.get("type") != "function":
        return False
    function = definition.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        return False
    return True


def make_plan(review, tools, source_call=""):
    available = {t["function"]["name"]: t for t in tools
                 if is_host_tool(t)}
    result = []
    for request in review.tool_requests:
        definition = available.get(request.name)
        if definition:
            result.append({"name": request.name, "arguments": request.arguments,
                           "definition_hash": digest(definition),
                           "source_call": source_call,
                           "claim_quote": request.claim_quote,
                           "request_kind": request.request_kind,
                           "full_artifact": request.full_artifact})
    return result


def candidates(plan, request, message):
    # Preserve explicit caller constraints and nonstandard provider replay state.
    if not request.tools or request.tool_choice not in (None, "auto", "required") or request.response_format:
        return []
    if message.get("refusal") or message.get("reasoning_details") or message.get("signature"):
        return []
    existing = message.get("tool_calls") or []
    if existing and request.parallel_tool_calls is False:
        return []
    available = {t["function"]["name"]: t for t in request.tools
                 if is_host_tool(t)}
    duplicates = set()
    for call in existing:
        try:
            duplicates.add(digest(call["function"]))
        except (KeyError, TypeError, ValueError):
            return []
    result = []
    for item in plan[:2]:
        try:
            definition = available.get(item["name"])
            # B sees a redacted copy of tool definitions in the persisted
            # observation. Accept either the exact host definition hash or its
            # redacted equivalent; never weaken name/argument validation.
            definition_hash = (digest(definition) if definition else "")
            redacted_hash = digest(redact_payload(definition)) if definition else ""
            if (not definition or item["definition_hash"] not in (definition_hash, redacted_hash)
                    or not isinstance(item["arguments"], dict)):
                continue
            arguments = json.dumps(item["arguments"], ensure_ascii=False, allow_nan=False)
            if len(arguments) > 2000:
                continue
            function = {"name": item["name"], "arguments": arguments}
            # Compare parsed arguments, so whitespace is not a second request.
            same = any(c["function"]["name"] == item["name"] and
                       json.loads(c["function"]["arguments"]) == item["arguments"] for c in existing)
            if same or digest(function) in duplicates:
                continue
            duplicates.add(digest(function))
            result.append({"id": planned_call_id(item), "type": "function", "function": function})
        except (KeyError, TypeError, ValueError):
            continue
        if request.parallel_tool_calls is False and result:
            break
    return result


async def reserve(tenant, run, source_call, turn, calls, timeout):
    """At most once per B snapshot AND real user turn; failure simply skips B tools."""
    try:
        async with asyncio.timeout(timeout):
            async with tenant_session(tenant) as session:
                for key in (f"source:{source_call}", f"turn:{turn}"):
                    result = await session.execute(insert(Event).values(
                        tenant_id=tenant, run_id=uuid.UUID(run), kind="observer_tool_reservation",
                        actor="lobe-b", payload=redact_payload({"source_call": source_call,
                            "requests": calls, "execution_status": "not_observed"}),
                        idempotency_key=f"btools:{tenant}:{run}:{key}",
                    ).on_conflict_do_nothing(index_elements=["idempotency_key"]).returning(Event.id))
                    if result.scalar_one_or_none() is None:
                        await session.rollback()
                        return False
                await session.commit()
                return True
    except Exception as exc:
        LOG.warning("B tool request skipped error_type=%s", type(exc).__name__)
        return False


def injector(context, request, original_messages, tenant, run, floor, attempt, settings, audit):
    if not settings.b_host_tools_enabled or not context.host_tool_plan or not context.review_source_call or not request.tools:
        return None
    # A tool-result round trip does not create another real user turn.
    users = [m for m in original_messages if m.get("role") == "user" and m.get("name") != "director_b"]
    turn = digest([floor, attempt, len(users), users[-1] if users else {}])

    async def inject(message):
        try:
            calls = candidates(context.host_tool_plan, request, message)
            if calls and await reserve(tenant, run, context.review_source_call, turn, calls, settings.b_state_read_timeout):
                audit["observer_delivery"]["injected_tool_ids"] = [c["id"] for c in calls]
                audit["observer_delivery"]["tool_source"] = "lobe-b"
                audit["observer_delivery"]["tool_lane"] = "peer-verifier"
                return calls
        except Exception as exc:
            LOG.warning("B tool request skipped error_type=%s", type(exc).__name__)
        return []
    return inject
