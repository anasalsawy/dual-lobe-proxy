"""Intelligent recipient routing for multi-agent environments.

When multiple agents operate in the same space, messages can trigger unintended
responses. This module detects whether an incoming message is directed at the
current agent or another entity, allowing the agent to:

1. Stay silent (block response generation) if not addressed
2. Still ingest the message into memory for context awareness
3. Continue background operations without response overhead

This solves the "talking over each other" problem in multi-agent rooms.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from ..state import repositories as repo

LOG = logging.getLogger("dual_lobe.b.recipient_router")

# Tokens used for recipient analysis (much lighter than full review)
RECIPIENT_ANALYSIS_MAX_TOKENS = 200


class RecipientAnalysis:
    """Result of recipient routing analysis."""

    def __init__(self, should_respond: bool, confidence: float, reasoning: str,
                 speaker: str | None = None, detected_recipients: list[str] | None = None):
        self.should_respond = should_respond
        self.confidence = confidence
        self.reasoning = reasoning
        self.speaker = speaker
        self.detected_recipients = detected_recipients or []

    def to_event_payload(self) -> dict[str, Any]:
        """Serialize for event logging."""
        return {
            "should_respond": self.should_respond,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "speaker": self.speaker,
            "detected_recipients": self.detected_recipients,
        }


RECIPIENT_ROUTER_SYSTEM = """You are a recipient analyzer for multi-agent conversations.
Your ONLY job is to determine if an incoming message is directed at the current agent (YOU).

You have NO other responsibilities:
- Do NOT execute tasks
- Do NOT evaluate claims
- Do NOT reason about the message content beyond recipient detection
- Do NOT use tools
- Do NOT provide solutions or suggestions

TASK: Given a message, determine:
1. Is this message directed AT YOU (the current agent)?
2. What is your confidence (0.0-1.0)?
3. Who is the speaker?
4. What other recipients are mentioned (if any)?

Recipient detection rules:
- Explicit mentions: "Agent A, please...", "@agent_name", "Alice, can you..."
- Context references: "the backend agent", "the frontend expert", "the one handling X"
- Implicit addressing: If only ONE agent is present and they're being addressed, it's for them
- Broadcast: "everyone", "all agents", "team" = directed at all
- Unclear: When ambiguous and you can't tell, default to assuming it IS for you

Return ONLY this JSON object:
{
  "should_respond": true|false,
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation (max 100 chars)",
  "speaker": "name or null",
  "detected_recipients": ["list", "of", "names", "or", "roles"]
}""".strip()


async def _analyze_recipient(agent_name: str, message: str) -> str:
    """Call the router model to analyze recipient intention.

    Returns the raw model response (should be valid JSON).
    
    Args:
        agent_name: Name of the current agent (sanitized)
        message: User message (sanitized)
    
    Raises:
        ValueError: If inputs are invalid
        asyncio.TimeoutError: If analysis times out
        Exception: For provider/network errors
    """
    # Validate inputs
    if not agent_name or not isinstance(agent_name, str):
        raise ValueError("agent_name must be a non-empty string")
    
    if not message or not isinstance(message, str):
        raise ValueError("message must be a non-empty string")
    
    # Sanitize inputs to prevent injection
    agent_name = agent_name.strip()[:200]  # Max agent name length
    message = message.strip()[:10000]  # Max message length for router
    
    if not agent_name:
        raise ValueError("agent_name becomes empty after sanitization")
    
    if not message:
        raise ValueError("message becomes empty after sanitization")
    
    s = get_settings()
    prompt = f"""Current agent name/role: {agent_name}

Message to analyze:
{message}

Determine if this message is directed at '{agent_name}'."""

    try:
        async with asyncio.timeout(10):
            response = await get_registry().adapter("lobe-b").buffered(
                NormalizedRequest(
                    messages=[
                        {"role": "system", "content": RECIPIENT_ROUTER_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    max_tokens=RECIPIENT_ANALYSIS_MAX_TOKENS,
                    timeout=10,
                )
            )
    except asyncio.TimeoutError as exc:
        LOG.warning("Recipient router timed out: %s", exc)
        raise
    except Exception as exc:
        LOG.warning("Recipient router provider error: %s", exc)
        raise

    try:
        data = response_dict(response)
        content = data["choices"][0]["message"].get("content") or ""
        if not content:
            raise ValueError("Model returned empty content")
        return content
    except (KeyError, IndexError, TypeError) as exc:
        LOG.error("Unexpected response structure from model: %s", exc)
        raise ValueError(f"Invalid model response structure: {exc}")


def _validate_recipient_analysis(obj: dict[str, Any]) -> tuple[bool, str | None]:
    """Validate parsed recipient analysis object.
    
    Returns (is_valid, error_message_or_none)
    """
    # Check required fields
    if "should_respond" not in obj:
        return False, "missing required field: should_respond"
    
    if "confidence" not in obj:
        return False, "missing required field: confidence"
    
    if "reasoning" not in obj:
        return False, "missing required field: reasoning"
    
    # Validate types and ranges
    try:
        should_respond = bool(obj.get("should_respond"))
    except (ValueError, TypeError):
        return False, "should_respond must be boolean"
    
    try:
        confidence = float(obj.get("confidence"))
        if not 0.0 <= confidence <= 1.0:
            return False, f"confidence must be 0.0-1.0, got {confidence}"
    except (ValueError, TypeError):
        return False, "confidence must be a number"
    
    try:
        reasoning = str(obj.get("reasoning", ""))
        if not reasoning or len(reasoning.strip()) == 0:
            return False, "reasoning must be non-empty string"
        if len(reasoning) > 500:  # Allow more than initial spec for better reasoning
            return False, f"reasoning too long ({len(reasoning)} > 500 chars)"
    except (ValueError, TypeError):
        return False, "reasoning must be string"
    
    # Validate optional fields
    speaker = obj.get("speaker")
    if speaker is not None and not isinstance(speaker, str):
        return False, "speaker must be string or null"
    
    detected_recipients = obj.get("detected_recipients", [])
    if not isinstance(detected_recipients, list):
        return False, "detected_recipients must be array"
    
    for item in detected_recipients:
        if not isinstance(item, str):
            return False, f"detected_recipients items must be strings, got {type(item)}"
    
    return True, None


def _parse_recipient_analysis(content: str) -> RecipientAnalysis:
    """Parse router output into structured analysis.

    Tolerates malformed JSON; on parse failure, defaults to 'should_respond=True'
    (safe default: if uncertain, the agent should engage).
    """
    if not content or not content.strip():
        LOG.warning("Recipient router returned empty content")
        return RecipientAnalysis(
            should_respond=True,
            confidence=0.0,
            reasoning="empty_response",
        )
    
    # Try to parse JSON
    try:
        obj = json.loads(content)
    except json.JSONDecodeError as exc:
        LOG.warning("Recipient analysis JSON parse failed: %s", exc)
        return RecipientAnalysis(
            should_respond=True,
            confidence=0.0,
            reasoning="json_parse_error",
        )
    
    if not isinstance(obj, dict):
        LOG.warning("Recipient router response not a JSON object: %s", type(obj))
        return RecipientAnalysis(
            should_respond=True,
            confidence=0.0,
            reasoning="not_json_object",
        )
    
    # Validate the parsed object
    is_valid, error_msg = _validate_recipient_analysis(obj)
    if not is_valid:
        LOG.warning("Recipient analysis validation failed: %s", error_msg)
        return RecipientAnalysis(
            should_respond=True,
            confidence=0.0,
            reasoning=f"validation_error: {error_msg}",
        )
    
    # Safe to extract validated fields
    try:
        return RecipientAnalysis(
            should_respond=bool(obj.get("should_respond", True)),
            confidence=float(obj.get("confidence", 0.5)),
            reasoning=str(obj.get("reasoning", ""))[:500],
            speaker=obj.get("speaker"),
            detected_recipients=list(obj.get("detected_recipients") or []),
        )
    except Exception as exc:
        LOG.exception("Unexpected error creating RecipientAnalysis: %s", exc)
        return RecipientAnalysis(
            should_respond=True,
            confidence=0.0,
            reasoning="creation_error",
        )


async def route_message(
    session: AsyncSession,
    agent_name: str,
    message: str,
    tenant_id: int,
    run_id: str,
    context_for_memory: dict[str, Any] | None = None,
) -> tuple[RecipientAnalysis, bool]:
    """Route a message to determine response responsibility.

    Args:
        session: Database session
        agent_name: Name/role of the current agent
        message: The incoming message to analyze
        tenant_id: Tenant for event logging
        run_id: Run ID for context
        context_for_memory: Optional context to store even if not responding

    Returns:
        Tuple of (RecipientAnalysis, should_ingest_into_memory)
        - RecipientAnalysis contains routing decision and confidence
        - should_ingest_into_memory is always True (we always store, even if silent)
    """
    s = get_settings()

    # If recipient routing is disabled, always respond
    if not s.recipient_routing_enabled:
        return (
            RecipientAnalysis(
                should_respond=True,
                confidence=1.0,
                reasoning="routing_disabled",
            ),
            True,
        )

    # Short-circuit empty messages
    if not message or not message.strip():
        return (
            RecipientAnalysis(
                should_respond=False,
                confidence=1.0,
                reasoning="empty_message",
            ),
            False,
        )

    try:
        raw = await _analyze_recipient(agent_name, message)
        analysis = _parse_recipient_analysis(raw)

        # Log the routing decision
        await repo.append_event(
            session,
            "recipient_routed",
            tenant_id,
            run_id=run_id,
            actor="lobe-b.router",
            payload={
                "agent_name": agent_name,
                "should_respond": analysis.should_respond,
                "confidence": analysis.confidence,
                "reasoning": analysis.reasoning,
                "speaker": analysis.speaker,
                "detected_recipients": analysis.detected_recipients,
            },
        )

        # CRITICAL: even if should_respond=False, we still ingest into memory
        # The message is suppressed from generating output, but the agent is aware of it
        return analysis, True

    except Exception as exc:
        LOG.exception("Recipient routing failed critically; defaulting to respond=True")
        # Safe default: if analysis fails, respond (don't silently drop)
        await repo.append_event(
            session,
            "recipient_route_error",
            tenant_id,
            run_id=run_id,
            actor="lobe-b.router",
            payload={"error": str(exc)[:200]},
        )
        return (
            RecipientAnalysis(
                should_respond=True,
                confidence=0.0,
                reasoning="analysis_error_safe_default",
            ),
            True,
        )


class SuppressedResponseMarker:
    """Mark that a response was suppressed due to recipient routing.

    Used to distinguish "silent ingestion for memory" from "no output".
    """

    def __init__(self, reason: str, analysis: RecipientAnalysis):
        self.reason = reason
        self.analysis = analysis
        self.timestamp = time.time()

    def to_event_payload(self) -> dict[str, Any]:
        """Serialize for event logging."""
        return {
            "reason": self.reason,
            "confidence": self.analysis.confidence,
            "reasoning": self.analysis.reasoning,
            "speaker": self.analysis.speaker,
            "detected_recipients": self.analysis.detected_recipients,
        }
