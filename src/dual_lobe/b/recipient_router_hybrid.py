"""Correct hybrid recipient routing: B decides simple intent, code enforces rules.

B (LLM) handles ONLY simple intent classification:
1. Follow-up vs New message
2. Addressed vs Broadcast  
3. Truly addressed vs Mentioned

Python handles ALL rule enforcement deterministically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry
from ..state import repositories as repo
from ..roles import get_role_name, get_role_rank
from .rules import parse_rule_command, add_rule, get_active_rules

LOG = logging.getLogger("dual_lobe.b.recipient_router")

# Tokens used for simple intent analysis (very lightweight)
INTENT_ANALYSIS_MAX_TOKENS = 150


class IntentAnalysis:
    """Simple intent analysis from B - only binary decisions."""
    
    def __init__(self, 
                 is_follow_up: bool,
                 is_broadcast: bool, 
                 is_truly_addressed: bool,
                 is_mentioned_only: bool,
                 confidence: float,
                 reasoning: str,
                 detected_names: list[str] | None = None):
        self.is_follow_up = is_follow_up
        self.is_broadcast = is_broadcast
        self.is_truly_addressed = is_truly_addressed
        self.is_mentioned_only = is_mentioned_only
        self.confidence = confidence
        self.reasoning = reasoning
        self.detected_names = detected_names or []


class RecipientAnalysis:
    """Final recipient routing analysis after rule application."""

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


def extract_agent_names_from_messages(messages: list[dict]) -> list[str]:
    """Extract potential agent names from system prompts and conversation context."""
    names = []
    
    # Extract names from system prompts
    for msg in messages:
        if msg.get("role") in ("system", "developer") and msg.get("content"):
            content = str(msg["content"])
            name_patterns = [
                r"You are (\w+)",
                r"Your name is (\w+)",
                r"you are (\w+)",
                r"your name is (\w+)",
                r"^\s*(\w+)\s*$"
            ]
            for pattern in name_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    name = match.strip()
                    if name and len(name) > 1 and name not in names:
                        names.append(name)
    
    # Extract names from conversation history (names used by other agents)
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("name"):
            name = msg.get("name")
            if name and name not in names:
                names.append(name)
    
    return names


# SIMPLIFIED B PROMPT - only intent classification
INTENT_ANALYZER_SYSTEM = """You are a simple intent classifier for multi-agent conversations.
Your job is to analyze the incoming message and answer ONLY these questions:

1. Is this a FOLLOW-UP message? (continuation of recent direct contact with this agent)
2. Is this a BROADCAST? (explicitly includes all agents like "everyone", "all of you")  
3. Is this TRULY ADDRESSED to this agent? (direct command/question meant for this agent)
4. Is this agent only MENTIONED? (referenced but not the intended recipient)

Look at the conversation history to understand context.
Use the agent names provided to identify if this agent is addressed.

Your ONLY output is a JSON object with these fields:
{
  "is_follow_up": true|false,
  "is_broadcast": true|false,
  "is_truly_addressed": true|false, 
  "is_mentioned_only": true|false,
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation (max 50 chars)",
  "detected_names": ["names", "found", "in", "message"]
}""".strip()


def parse_hierarchy_roles(raw: str) -> dict[str, int]:
    """Parse 'chief:0,l1:1,l2:2' into a role -> rank map."""
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
    if role in hierarchy:
        return hierarchy[role]
    if "/" in role:
        short = role.split("/")[-1]
        if short in hierarchy:
            return hierarchy[short]
    return None


def apply_hierarchy_broadcast_rules(
    speaker_is_human: bool,
    speaker: str | None,
    agent_name: str,
    hierarchy: dict[str, int],
    detected_recipients: list[str]
) -> RecipientAnalysis:
    """Apply deterministic hierarchy broadcast rules."""
    
    if speaker_is_human:
        # Human broadcast: only highest-ranked agents respond
        candidates = [r for r in detected_recipients if rank_of(r, hierarchy) is not None]
        if not candidates:
            # No specific recipients mentioned - only highest tier responds
            agent_rank = rank_of(agent_name, hierarchy)
            if agent_rank is not None and agent_rank == 0:
                return RecipientAnalysis(
                    should_respond=True,
                    confidence=1.0,
                    reasoning=f"broadcast_human_highest:{agent_name}",
                    is_broadcast=True,
                )
            return RecipientAnalysis(
                should_respond=False,
                confidence=1.0,
                reasoning=f"broadcast_human_not_highest:{agent_name}",
                is_broadcast=True,
            )

        # Specific recipients mentioned - only highest among them responds
        best = min(candidates, key=lambda r: rank_of(r, hierarchy))  # type: ignore[arg-type]
        agent_rank = rank_of(agent_name, hierarchy)
        best_rank = rank_of(best, hierarchy)
        should = agent_rank is not None and best_rank is not None and agent_rank <= best_rank
        return RecipientAnalysis(
            should_respond=should,
            confidence=1.0,
            reasoning=f"broadcast_human_specific:{best}",
            is_broadcast=True,
        )
    else:
        # Agent-to-agent broadcast: enforce DOWN hierarchy direction only
        speaker_rank = rank_of(speaker, hierarchy) if speaker else None
        agent_rank = rank_of(agent_name, hierarchy)
        
        if speaker_rank is not None and agent_rank is not None:
            if speaker_rank < agent_rank:
                # Higher agent broadcasting to lower agent - ALLOW
                if agent_name in detected_recipients or "all" in [r.lower() for r in detected_recipients]:
                    return RecipientAnalysis(
                        should_respond=True,
                        confidence=1.0,
                        reasoning=f"broadcast_down:{speaker}",
                        is_broadcast=True,
                    )
                return RecipientAnalysis(
                    should_respond=False,
                    confidence=1.0,
                    reasoning=f"broadcast_down_not_for_me:{speaker}",
                    is_broadcast=True,
                )
            else:
                # Same level or upward broadcast - BLOCK
                return RecipientAnalysis(
                    should_respond=False,
                    confidence=1.0,
                    reasoning=f"broadcast_blocked:{speaker}",
                    is_broadcast=True,
                )
        else:
            # Unknown ranks - safe default: treat as noise
            return RecipientAnalysis(
                should_respond=False,
                confidence=1.0,
                reasoning="broadcast_agent_unknown",
                is_broadcast=True,
            )


async def _analyze_intent(agent_name: str, message: str, conversation_history: list[dict], extracted_names: list[str]) -> str:
    """Call B to perform simple intent classification only."""
    
    if not message or not isinstance(message, str):
        raise ValueError("message must be a non-empty string")
        
    message = message.strip()[:10000]
    if not message:
        raise ValueError("message becomes empty after sanitization")
    
    # Build simple prompt for intent analysis
    prompt_parts = [
        f"Current agent names: {', '.join(extracted_names) if extracted_names else 'None'}",
        ""
    ]
    
    # Add recent conversation history for follow-up detection
    if conversation_history:
        history_lines = []
        for msg in conversation_history[-4:]:  # Last 4 messages for context
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            text = str(content or "")[:150]
            if msg.get("name"):
                text = f"[{msg['name']}] {text}"
            history_lines.append(f"  [{role}] {text}")
        if history_lines:
            prompt_parts.append("Recent conversation:")
            prompt_parts.extend(history_lines)
            prompt_parts.append("")
    
    prompt_parts.extend([
        "Message to analyze:",
        message,
        "",
        "Analyze this message for simple intent classification only."
    ])
    
    prompt = "\n".join(prompt_parts)

    try:
        async with asyncio.timeout(8):
            response = await get_registry().adapter("lobe-b").buffered(
                NormalizedRequest(
                    messages=[
                        {"role": "system", "content": INTENT_ANALYZER_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    max_tokens=INTENT_ANALYSIS_MAX_TOKENS,
                    timeout=8,
                )
            )
    except asyncio.TimeoutError as exc:
        LOG.warning("Intent analyzer timed out: %s", exc)
        raise
    except Exception as exc:
        LOG.warning("Intent analyzer provider error: %s", exc)
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


def _parse_intent_analysis(content: str, extracted_names: list[str]) -> IntentAnalysis:
    """Parse B's intent analysis response."""
    if not content or not content.strip():
        LOG.warning("Intent analyzer returned empty content")
        # Safe default: assume it's addressed (better to over-respond)
        return IntentAnalysis(
            is_follow_up=False,
            is_broadcast=False,
            is_truly_addressed=True,
            is_mentioned_only=False,
            confidence=0.5,
            reasoning="empty_response_safe_default",
        )
    
    try:
        obj = json.loads(content)
    except json.JSONDecodeError as exc:
        LOG.warning("Intent analysis JSON parse failed: %s", exc)
        return IntentAnalysis(
            is_follow_up=False,
            is_broadcast=False,
            is_truly_addressed=True,
            is_mentioned_only=False,
            confidence=0.5,
            reasoning="json_parse_error",
        )
    
    if not isinstance(obj, dict):
        LOG.warning("Intent analyzer response not a JSON object: %s", type(obj))
        return IntentAnalysis(
            is_follow_up=False,
            is_broadcast=False,
            is_truly_addressed=True,
            is_mentioned_only=False,
            confidence=0.5,
            reasoning="not_json_object",
        )
    
    try:
        return IntentAnalysis(
            is_follow_up=bool(obj.get("is_follow_up", False)),
            is_broadcast=bool(obj.get("is_broadcast", False)),
            is_truly_addressed=bool(obj.get("is_truly_addressed", False)),
            is_mentioned_only=bool(obj.get("is_mentioned_only", False)),
            confidence=float(obj.get("confidence", 0.5)),
            reasoning=str(obj.get("reasoning", ""))[:100],
            detected_names=list(obj.get("detected_names") or []),
        )
    except Exception as exc:
        LOG.exception("Unexpected error creating IntentAnalysis: %s", exc)
        return IntentAnalysis(
            is_follow_up=False,
            is_broadcast=False,
            is_truly_addressed=True,
            is_mentioned_only=False,
            confidence=0.5,
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
    """Route a message using correct hybrid architecture.
    
    B decides simple intent only. Python enforces all rules.
    """
    s = get_settings()

    if not s.recipient_routing_enabled:
        return (
            RecipientAnalysis(should_respond=True, confidence=1.0, reasoning="routing_disabled"),
            True,
        )

    mode = (mode or s.routing_mode or "off").strip().lower()

    if not message or not message.strip():
        return (
            RecipientAnalysis(should_respond=False, confidence=1.0, reasoning="empty_message"),
            False,
        )

    try:
        # Handle rule-setting commands
        rule_result = parse_rule_command(message)
        if rule_result:
            target_agent, rule_text = rule_result
            add_rule(target_agent, rule_text)
            LOG.info("Parsed rule command: %s -> %s", target_agent, rule_text)

        # Extract agent names for deterministic matching
        all_messages = (context_for_memory or {}).get("messages", [])
        extracted_names = extract_agent_names_from_messages(all_messages)
        
        # Add official role name
        role_name = get_role_name(agent_name)
        if role_name and role_name not in extracted_names:
            extracted_names.append(role_name)

        # DETERMINISTIC RULES FIRST - guaranteed to work
        
        # Rule 1: Direct name mention (simple string matching)
        message_lower = message.lower()
        for name in extracted_names:
            if name.lower() in message_lower:
                LOG.info("Direct name mention detected: %s in %s", name, message[:100])
                return (
                    RecipientAnalysis(
                        should_respond=True,
                        confidence=1.0,
                        reasoning=f"deterministic_name_mention:{name}",
                        detected_recipients=extracted_names,
                    ),
                    True,
                )

        # Get conversation history (exclude current message)
        conv_history = all_messages[-5:-1] if len(all_messages) > 1 else []

        # Get B's simple intent analysis
        raw_intent = await _analyze_intent(agent_name, message, conv_history, extracted_names)
        intent = _parse_intent_analysis(raw_intent, extracted_names)

        # Apply rules based on B's simple intent analysis
        
        # Rule 2: Follow-ups always respond
        if intent.is_follow_up:
            return (
                RecipientAnalysis(
                    should_respond=True,
                    confidence=intent.confidence,
                    reasoning=f"follow_up:{intent.reasoning}",
                ),
                True,
            )
        
        # Rule 3: Truly addressed messages always respond
        if intent.is_truly_addressed:
            return (
                RecipientAnalysis(
                    should_respond=True,
                    confidence=intent.confidence,
                    reasoning=f"truly_addressed:{intent.reasoning}",
                    detected_recipients=intent.detected_names,
                ),
                True,
            )
        
        # Rule 4: Broadcast handling
        if intent.is_broadcast:
            if mode == "off":
                return (
                    RecipientAnalysis(should_respond=True, confidence=intent.confidence, reasoning="broadcast_off_mode"),
                    True,
                )
            elif mode == "flat":
                return (
                    RecipientAnalysis(should_respond=True, confidence=intent.confidence, reasoning="broadcast_flat_mode"),
                    True,
                )
            elif mode == "hierarchy":
                # Apply deterministic hierarchy broadcast rules
                speaker_is_human = intent.reasoning and any(term in intent.reasoning.lower() 
                                                           for term in ["human", "user", "person"])
                speaker = None  # B should ideally provide this, but we'll infer
                if conv_history:
                    last_msg = conv_history[-1]
                    if last_msg.get("role") == "user":
                        speaker_is_human = True
                    elif last_msg.get("role") == "assistant" and last_msg.get("name"):
                        speaker = last_msg.get("name")
                        speaker_is_human = False
                
                return apply_hierarchy_broadcast_rules(
                    speaker_is_human, speaker, agent_name, 
                    parse_hierarchy_roles(s.hierarchy_roles),
                    intent.detected_names
                )
        
        # Rule 5: Mentioned but not addressed - suppress
        if intent.is_mentioned_only:
            return (
                RecipientAnalysis(
                    should_respond=False,
                    confidence=intent.confidence,
                    reasoning=f"mentioned_only:{intent.reasoning}",
                ),
                True,
            )
        
        # Rule 6: Ambiguous cases - safe default (respond)
        return (
            RecipientAnalysis(
                should_respond=True,
                confidence=0.5,
                reasoning="ambiguous_safe_default",
            ),
            True,
        )

    except Exception as exc:
        LOG.exception("Recipient routing failed; defaulting to respond=True")
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
    """Mark that a response was suppressed due to recipient routing."""

    def __init__(self, reason: str, analysis: RecipientAnalysis):
        self.reason = reason
        self.analysis = analysis
        self.timestamp = time.time()

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "confidence": self.analysis.confidence,
            "reasoning": self.analysis.reasoning,
            "speaker": self.analysis.speaker,
            "detected_recipients": self.analysis.detected_recipients,
        }