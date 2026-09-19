"""Intelligent recipient routing for multi-agent environments.

When multiple agents operate in the same space, messages can trigger unintended
responses. This module detects whether an incoming message is directed at the
current agent or another entity, allowing the agent to:

1. Stay silent (block response generation) if not addressed
2. Still ingest the message into memory for context awareness
3. Continue background operations without response overhead

This solves the "talking over each other" problem in multi-agent rooms.

Now also supports dynamic rule injection:
- "Research agent, don't speak" -> mutes that agent
- "IT agent, only respond to urgent" -> urgent-only mode
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, List

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from ..state import repositories as repo
from .rules import parse_rule_command, add_rule, get_active_rules, normalize_agent_name

LOG = logging.getLogger("dual_lobe.b.recipient_router")

# Tokens used for recipient analysis (much lighter than full review)
RECIPIENT_ANALYSIS_MAX_TOKENS = 200


class RecipientAnalysis:
    """Result of recipient routing analysis."""

    def __init__(self, should_respond: bool, confidence: float, reasoning: str,
                 speaker: str | None = None, detected_recipients: list[str] | None = None,
                 is_broadcast: bool = False):
        self.should_respond = should_respond
        self.confidence = confidence
        self.reasoning = reasoning
        self.speaker = speaker
        self.detected_recipients = detected_recipients or []
        self.is_broadcast = is_broadcast

    def to_event_payload(self) -> dict[str, Any]:
        """Serialize for event logging."""
        return {
            "should_respond": self.should_respond,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "speaker": self.speaker,
            "detected_recipients": self.detected_recipients,
            "is_broadcast": self.is_broadcast,
        }


RECIPIENT_ROUTER_SYSTEM = """You are a recipient analyzer for multi-agent conversations.
Your job is to determine if an incoming message is directed at the current agent (YOU) and whether you should respond.

You will receive:
- The agent's tier and which tiers are higher/lower
- The conversation messages (which may include the agent's name from the system prompt)
- The incoming message to analyze

Determine:
1. Who is the speaker? (human or which agent — extract the name from the messages)
2. Is this a direct address to this agent (by name) or a broadcast?
3. Should this agent respond?

ROUTING RULES:
- If the message is directed at this agent by name, should_respond = true.
- If the message addresses everyone in the room (e.g. "guys", "everyone", "team", "all of you"), should_respond = true for all agents — this is a direct address to all, not a broadcast.
- If the message is a continuation of recent direct contact with this agent (follow-up in the same conversation thread), should_respond = true.
- Otherwise it's a broadcast. Broadcasts flow downward only — higher tier to lower, not up, not same-tier. Only the highest tier present responds to a broadcast.
- These rules apply to all speakers, human or agent.

Use the agent's actual name (from the system prompt or messages) for identifying who is speaking and who is addressed. If no name is found, use the tier identifier.

Your ONLY output is a JSON object with these fields:
{
  "should_respond": true|false,
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation (max 100 chars)",
  "speaker": "name or null",
  "detected_recipients": ["list", "of", "names"],
  "is_broadcast": true|false
}""".strip()


HierarchyRule = dict[str, Any]


def parse_hierarchy_roles(raw: str) -> dict[str, int]:
    """Parse 'chief:0,l1:1,l2:2' into a role -> rank map.

    Lower number = higher rank. Empty input returns empty dict.
    Also adds short-name aliases (e.g. 'dl-dialogue1' for 'sawii/dl-dialogue1')
    so rank_of can resolve names the LLM returns without the full prefix.
    """
    result: dict[str, int] = {}
    if not raw:
        return result
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            continue
        role, rank = entry.split(":", 1)
        role = role.strip().lower()
        try:
            r = int(rank.strip())
            result[role] = r
            # Add short name without sawii/ prefix for fuzzy LLM matching
            if "/" in role:
                short = role.split("/")[-1]
                result[short] = r
        except ValueError:
            continue
    return result


def rank_of(role: str, hierarchy: dict[str, int]) -> int | None:
    if not role:
        return None
    role = role.lower().strip()
    # Try exact match, then short-name match (strip sawii/ prefix)
    if role in hierarchy:
        return hierarchy[role]
    if "/" in role:
        short = role.split("/")[-1]
        if short in hierarchy:
            return hierarchy[short]
    return None


def _apply_routing_mode(
    analysis: RecipientAnalysis,
    mode: str,
    agent_name: str,
    hierarchy: dict[str, int],
) -> RecipientAnalysis:
    """Adjust the raw recipient analysis according to routing mode.

    Modes:
      off      -> always respond (analysis ignored)
      flat     -> respond if addressed or broadcast; unchanged from router
      hierarchy-> broadcast direction rules:
                   - Human broadcasts: only highest-ranked present agent responds
                   - Agent broadcasts: only allowed DOWN hierarchy (higher → lower rank)
                   - Same-level or upward agent broadcasts: treated as noise (no response)
                   - Direct addressing always works regardless of direction
    """
    if mode == "off":
        return RecipientAnalysis(
            should_respond=True,
            confidence=1.0,
            reasoning="routing_off",
            speaker=analysis.speaker,
            detected_recipients=analysis.detected_recipients,
            is_broadcast=analysis.is_broadcast,
        )

    if mode == "flat":
        # In flat mode, broadcast = respond. Direct addressing already decided by router.
        return analysis

    if mode == "hierarchy":
        # Direct addressing overrides hierarchy - always respond if directly addressed
        if not analysis.is_broadcast:
            return analysis

        # Determine if speaker is human or agent
        speaker = analysis.speaker
        speaker_is_human = speaker is None or speaker.lower() in ["user", "human", "anas"]
        
        if speaker_is_human:
            # Human broadcast: only highest-ranked agents respond
            candidates = [r for r in analysis.detected_recipients if rank_of(r, hierarchy) is not None]
            if not candidates:
                # No known hierarchy members mentioned in the broadcast.
                # Only the highest-ranked agent (rank 0) should respond.
                # Lower-ranked agents stay silent.
                agent_rank = rank_of(agent_name, hierarchy)
                if agent_rank is not None and agent_rank == 0:
                    return RecipientAnalysis(
                        should_respond=True,
                        confidence=analysis.confidence,
                        reasoning=f"broadcast_fallback:{agent_name}",
                        speaker=analysis.speaker,
                        detected_recipients=analysis.detected_recipients,
                        is_broadcast=True,
                    )
                return RecipientAnalysis(
                    should_respond=False,
                    confidence=analysis.confidence,
                    reasoning=f"broadcast_not_highest:{agent_name}",
                    speaker=analysis.speaker,
                    detected_recipients=analysis.detected_recipients,
                    is_broadcast=True,
                )

            best = min(candidates, key=lambda r: rank_of(r, hierarchy))  # type: ignore[arg-type]
            agent_rank = rank_of(agent_name, hierarchy)
            best_rank = rank_of(best, hierarchy)
            should = agent_rank is not None and best_rank is not None and agent_rank <= best_rank
            return RecipientAnalysis(
                should_respond=should,
                confidence=analysis.confidence,
                reasoning=f"broadcast_hierarchy:{best}",
                speaker=analysis.speaker,
                detected_recipients=analysis.detected_recipients,
                is_broadcast=True,
            )
        else:
            # Agent-to-agent broadcast: enforce DOWN hierarchy direction only
            speaker_rank = rank_of(speaker, hierarchy) if speaker else None
            agent_rank = rank_of(agent_name, hierarchy)
            
            # Can only broadcast DOWN the hierarchy (lower rank number = higher authority)
            # Higher agent (rank 0) can broadcast to lower agents (rank 1, 2, etc.)
            # Same level (rank 1 → rank 1) or upward (rank 2 → rank 1) is NOT allowed
            if speaker_rank is not None and agent_rank is not None:
                if speaker_rank < agent_rank:
                    # Higher agent broadcasting to lower agent - ALLOW
                    # Check if current agent is in the intended recipient group
                    if agent_name in analysis.detected_recipients or "all" in [r.lower() for r in analysis.detected_recipients]:
                        return RecipientAnalysis(
                            should_respond=True,
                            confidence=analysis.confidence,
                            reasoning=f"broadcast_down:{speaker}",
                            speaker=analysis.speaker,
                            detected_recipients=analysis.detected_recipients,
                            is_broadcast=True,
                        )
                    else:
                        # Not addressed to this agent specifically
                        return RecipientAnalysis(
                            should_respond=False,
                            confidence=analysis.confidence,
                            reasoning=f"broadcast_not_for_me:{speaker}",
                            speaker=analysis.speaker,
                            detected_recipients=analysis.detected_recipients,
                            is_broadcast=True,
                        )
                else:
                    # Same level or upward broadcast - BLOCK (treat as noise)
                    return RecipientAnalysis(
                        should_respond=False,
                        confidence=analysis.confidence,
                        reasoning=f"broadcast_blocked:{speaker}",
                        speaker=analysis.speaker,
                        detected_recipients=analysis.detected_recipients,
                        is_broadcast=True,
                    )
            else:
                # Unknown ranks - safe default: treat as noise
                return RecipientAnalysis(
                    should_respond=False,
                    confidence=analysis.confidence,
                    reasoning="broadcast_agent_unknown",
                    speaker=analysis.speaker,
                    detected_recipients=analysis.detected_recipients,
                    is_broadcast=True,
                )

    # Unknown mode: safe default
    LOG.warning("Unknown routing mode %r; defaulting to flat", mode)
    return analysis


async def _analyze_recipient(agent_name: str, message: str, active_rules: List[AgentRule] | None = None, routing_context: dict | None = None, system_prompt: str | None = None, conversation_history: list[dict] | None = None) -> str:
    """Call the router model to analyze recipient intention.

    Returns the raw model response (should be valid JSON).
    
    Args:
        agent_name: Name of the current agent (sanitized)
        message: User message (sanitized)
        active_rules: Active rules for this agent (if any)
        routing_context: Additional context like hierarchy info, routing mode
    
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
    
    # Build prompt with all relevant context
    prompt_parts = [f"Current agent: {agent_name}"]

    # Include system prompt so B can see the agent's actual name
    if system_prompt:
        prompt_parts.append(f"Agent system prompt: {system_prompt[:500]}")

    # Add routing mode and tier context
    if routing_context:
        mode = routing_context.get("mode", "flat")
        prompt_parts.append(f"Routing mode: {mode}")

        if mode == "hierarchy":
            # Pass tier info — B's LLM does the full routing decision
            tier_info = routing_context.get("tier_info", "")
            if tier_info:
                prompt_parts.append(tier_info)
            else:
                # Fallback: pass hierarchy dict if tier_info not available
                hierarchy = routing_context.get("hierarchy", {})
                if hierarchy:
                    hierarchy_str = ", ".join([f"{role}:{rank}" for role, rank in hierarchy.items()])
                    prompt_parts.append(f"Hierarchy (lower number = higher rank): {hierarchy_str}")
    
    # Add active rules if present
    if active_rules:
        rule_texts = [rule.rule_text for rule in active_rules]
        rules_str = " | ".join(rule_texts)
        prompt_parts.append(f"Active behavioral rules for you: {rules_str}")
    
    prompt_parts.extend([
        "",
    ])

    # Add recent conversation history so B can understand follow-up messages
    if conversation_history:
        history_lines = []
        # Only include last 6 messages to keep prompt short
        for msg in conversation_history[-6:]:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            text = str(content or "")[:200]
            if msg.get("name"):
                text = f"[{msg['name']}] {text}"
            history_lines.append(f"  [{role}] {text}")
        if history_lines:
            prompt_parts.append("Recent conversation context:")
            prompt_parts.extend(history_lines)
            prompt_parts.append("")

    prompt_parts.extend([
        "Message to analyze:",
        message,
        "",
        f"Determine if this message is directed at this agent and whether you should respond.",
        "The agent's name may appear in the system prompt above — use it to detect direct addressing.",
        "Use the conversation context to understand follow-up messages (e.g. 'yes' or 'go ahead' after addressing this agent).",
        "Consider your routing mode, tier info, and active behavioral rules when deciding.",
        "Remember: direct addressing (explicitly calling the agent's name) should generally override broadcast rules.",
    ])
    
    prompt = "\n".join(prompt_parts)

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

    is_broadcast = obj.get("is_broadcast")
    if is_broadcast is not None and not isinstance(is_broadcast, bool):
        return False, "is_broadcast must be boolean or null"
    
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
            is_broadcast=bool(obj.get("is_broadcast", False)),
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
    mode: str | None = None,
) -> tuple[RecipientAnalysis, bool]:
    """Route a message to determine response responsibility.

    Args:
        session: Database session
        agent_name: Name/role of the current agent
        message: The incoming message to analyze
        tenant_id: Tenant for event logging
        run_id: Run ID for context
        context_for_memory: Optional context to store even if not responding
        mode: Optional routing mode override ("off", "flat", "hierarchy").
              If omitted, uses DUAL_LOBE_ROUTING_MODE from settings.

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

    mode = (mode or s.routing_mode or "off").strip().lower()

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
        # First, check if this message contains a rule-setting command
        # Use permissive mode (empty known_agents) to accept any agent name
        rule_result = parse_rule_command(message)
        if rule_result:
            target_agent, rule_text = rule_result
            # Add the rule (this affects future messages)
            add_rule(target_agent, rule_text)
            LOG.info("Parsed rule command: %s -> %s", target_agent, rule_text)
            
            # Even if this message sets a rule, we still need to route it normally
            # (the rule applies to FUTURE messages, not this one)
        
        # Get active rules for current agent
        active_rules = get_active_rules(agent_name)
        
        # Build routing context for the LLM
        routing_context = {
            "mode": mode,
        }

        if mode == "hierarchy":
            from ..roles import get_role_persona
            # Pass the tier persona text — B's LLM uses this to understand
            # the tier hierarchy and make routing decisions
            tier_persona = get_role_persona(agent_name)
            if tier_persona:
                routing_context["tier_info"] = tier_persona
            else:
                # Fallback to hierarchy dict if no persona
                hierarchy = parse_hierarchy_roles(s.hierarchy_roles)
                routing_context["hierarchy"] = hierarchy
        
        # Extract system prompt from messages for the routing LLM
        system_prompt = None
        for msg in (context_for_memory or {}).get("messages", []):
            if msg.get("role") in ("system", "developer") and msg.get("content"):
                system_prompt = str(msg["content"])[:500]
                break

        # Extract conversation history for context (last 6 messages)
        conv_history = (context_for_memory or {}).get("messages", [])[-6:] if context_for_memory else []

        # Call the router model with full context
        raw = await _analyze_recipient(agent_name, message, active_rules, routing_context, system_prompt=system_prompt, conversation_history=conv_history)
        analysis = _parse_recipient_analysis(raw)

        # B's LLM makes the full routing decision — no Python override.
        # The hierarchy rules are in B's prompt, B reads the agent's name
        # from the messages, and B decides should_respond directly.

        # Log the routing decision
        await repo.append_event(
            session,
            "recipient_routed",
            tenant_id,
            run_id=run_id,
            actor="lobe-b.router",
            payload={
                "agent_name": agent_name,
                "mode": mode,
                "should_respond": analysis.should_respond,
                "confidence": analysis.confidence,
                "reasoning": analysis.reasoning,
                "speaker": analysis.speaker,
                "detected_recipients": analysis.detected_recipients,
                "is_broadcast": analysis.is_broadcast,
                "active_rules_count": len(active_rules) if active_rules else 0,
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
