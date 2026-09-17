"""Dynamic rule engine for agent behavior modification.

Allows user to issue runtime commands like:
- "Research agent, keep working and don't speak."
- "IT agent, only respond if there's an emergency."

Rules are stored in memory and applied during message routing.
Agent B interprets the rule conditions using its reasoning capabilities.
"""
from __future__ import annotations

import re
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

LOG = logging.getLogger("dual_lobe.b.rules")


@dataclass
class AgentRule:
    """A dynamic rule applied to an agent."""
    agent_name: str
    rule_text: str  # Full rule text (e.g., "don't speak unless directly asked")
    active: bool = True
    created_at: float = field(default_factory=lambda: __import__('time').time())


# Global in-memory rule store
# Key: agent_name (normalized), Value: list of active rules
_DYNAMIC_RULES: Dict[str, List[AgentRule]] = {}


def normalize_agent_name(name: str) -> str:
    """Normalize agent name for matching (lowercase, strip spaces)."""
    return name.strip().lower()


def parse_rule_command(message: str) -> Optional[Tuple[str, str]]:
    """Parse a user message for rule-setting commands.
    
    Returns (agent_name, rule_text) if detected, else None.
    
    Examples:
        "Research agent, keep working and don't speak" -> ("Research agent", "keep working and don't speak")
        "IT agent, only respond if there's an emergency" -> ("IT agent", "only respond if there's an emergency")
    """
    # Pattern: "AGENT_NAME, RULE_TEXT"
    # Match agent name followed by comma and rule text
    pattern = r"^([^,]+),\s*(.+)$"
    match = re.match(pattern, message.strip(), re.IGNORECASE)
    
    if match:
        agent = match.group(1).strip()
        rule_text = match.group(2).strip()
        
        # Only consider it a rule command if the rule text contains
        # words indicating behavioral modification
        behavioral_keywords = [
            'don\'t', 'do not', 'stop', 'mute', 'silence', 'quiet', 
            'only', 'just', 'respond', 'speak', 'talk', 'work',
            'keep working', 'continue', 'ignore', 'wait', 'hold',
            'unless', 'if', 'when', 'emergency', 'urgent'
        ]
        
        rule_lower = rule_text.lower()
        if any(kw in rule_lower for kw in behavioral_keywords):
            return (agent, rule_text)
    
    return None


def _agent_matches_known(candidate: str, known_agents: List[str]) -> bool:
    """Check if candidate agent name matches any known agent (fuzzy).
    
    If no known agents provided, always return True (permissive mode).
    """
    if not known_agents:
        return True  # Permissive: accept any agent name
        
    candidate_norm = normalize_agent_name(candidate)
    for known in known_agents:
        known_norm = normalize_agent_name(known)
        if candidate_norm == known_norm or candidate_norm in known_norm or known_norm in candidate_norm:
            return True
    return False


def add_rule(agent_name: str, rule_text: str) -> None:
    """Add a rule for an agent."""
    norm_name = normalize_agent_name(agent_name)
    rule = AgentRule(agent_name=agent_name, rule_text=rule_text)
    
    if norm_name not in _DYNAMIC_RULES:
        _DYNAMIC_RULES[norm_name] = []
    
    # Replace any existing rule (only one active rule per agent for now)
    _DYNAMIC_RULES[norm_name] = [rule]
    LOG.info("Added rule: %s -> %s", agent_name, rule_text)


def get_active_rules(agent_name: str) -> List[AgentRule]:
    """Get active rules for an agent."""
    norm_name = normalize_agent_name(agent_name)
    return _DYNAMIC_RULES.get(norm_name, [])


def apply_rules_to_analysis(
    analysis: 'RecipientAnalysis', 
    agent_name: str, 
    message: str,
    original_message: str  # The full original user message that set the rule
) -> 'RecipientAnalysis':
    """Apply active rules to modify recipient analysis.
    
    This function should be called with Agent B's reasoning to interpret
    whether the current message satisfies the agent's active rule condition.
    """
    # Rules are interpreted by Agent B in the main routing logic
    # This function just returns the analysis unchanged
    # (actual rule interpretation happens in the router's LLM call)
    return analysis


def clear_rules(agent_name: str) -> None:
    """Clear all rules for an agent."""
    norm_name = normalize_agent_name(agent_name)
    if norm_name in _DYNAMIC_RULES:
        del _DYNAMIC_RULES[norm_name]
        LOG.info("Cleared rules for %s", agent_name)