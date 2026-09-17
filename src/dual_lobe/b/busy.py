"""Busy state tracking for agents.

Tracks whether agents are currently busy with tasks and should avoid interruptions.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional
from dataclasses import dataclass, field

LOG = logging.getLogger("dual_lobe.b.busy")


@dataclass
class AgentBusyState:
    """Represents an agent's busy state."""
    agent_name: str
    busy: bool = False
    task_description: str = ""  # What the agent is working on
    since: float = field(default_factory=lambda: __import__('time').time())


# Global busy state tracker
_BUSY_STATES: Dict[str, AgentBusyState] = {}


def normalize_agent_name(name: str) -> str:
    """Normalize agent name for matching."""
    return name.strip().lower()


def set_agent_busy(agent_name: str, task_description: str = "") -> None:
    """Mark an agent as busy."""
    norm_name = normalize_agent_name(agent_name)
    _BUSY_STATES[norm_name] = AgentBusyState(
        agent_name=agent_name,
        busy=True,
        task_description=task_description
    )
    LOG.info("Agent %s marked as busy: %s", agent_name, task_description)


def set_agent_available(agent_name: str) -> None:
    """Mark an agent as available."""
    norm_name = normalize_agent_name(agent_name)
    if norm_name in _BUSY_STATES:
        _BUSY_STATES[norm_name].busy = False
        _BUSY_STATES[norm_name].task_description = ""
        LOG.info("Agent %s marked as available", agent_name)


def is_agent_busy(agent_name: str) -> bool:
    """Check if an agent is currently busy."""
    norm_name = normalize_agent_name(agent_name)
    state = _BUSY_STATES.get(norm_name)
    return state.busy if state else False


def get_busy_state(agent_name: str) -> Optional[AgentBusyState]:
    """Get the busy state for an agent."""
    norm_name = normalize_agent_name(agent_name)
    return _BUSY_STATES.get(norm_name)


def clear_busy_state(agent_name: str) -> None:
    """Clear busy state for an agent."""
    norm_name = normalize_agent_name(agent_name)
    if norm_name in _BUSY_STATES:
        del _BUSY_STATES[norm_name]
        LOG.info("Cleared busy state for %s", agent_name)


def detect_busy_from_context(agent_name: str, conversation_history: list) -> bool:
    """Detect if an agent is likely busy based on conversation context.
    
    Looks for patterns like:
    - Agent recently said they're working on something
    - Agent hasn't completed a declared task
    - Long-running task indicators
    """
    # For now, return current explicit state
    # Could be enhanced with LLM-based context analysis
    return is_agent_busy(agent_name)