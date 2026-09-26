from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class FloorMode(str, Enum):
    OBSERVE = "observe"
    RESPOND = "respond"
    BROADCAST = "broadcast"


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    display_name: str
    aliases: tuple[str, ...] = ()

    def all_names(self) -> tuple[str, ...]:
        values = [self.display_name, *self.aliases]
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            key = value.strip().casefold()
            if key and key not in seen:
                seen.add(key)
                ordered.append(value.strip())
        return tuple(ordered)


@dataclass(frozen=True)
class GroupMessage:
    text: str
    sender_id: str = "user"
    group_id: str = "default"
    reply_to_agent_id: str | None = None
    explicit_target_ids: tuple[str, ...] = ()
    broadcast: bool = False


@dataclass(frozen=True)
class AgentDelivery:
    agent_id: str
    mode: FloorMode
    can_emit: bool
    text: str
    addressed_agents: tuple[str, ...]


@dataclass
class AgentRuntimeState:
    awareness_queue: list[str] = field(default_factory=list)
    active_floor: bool = False
    current_task: str | None = None

    def enqueue_awareness(self, text: str) -> None:
        if text.strip():
            self.awareness_queue.append(text.strip())

    def drain_awareness(self) -> list[str]:
        items = list(self.awareness_queue)
        self.awareness_queue.clear()
        return items


class GroupCoordinator:
    """Portable group-awareness and floor-control proof of concept.

    The coordinator is deliberately model-agnostic. It separates message
    visibility from speaking permission, so non-addressed agents may remain
    aware without receiving a user-facing turn.
    """

    def __init__(self, identities: Iterable[AgentIdentity]):
        identities = list(identities)
        if not identities:
            raise ValueError("At least one agent identity is required.")

        ids = [x.agent_id for x in identities]
        if len(ids) != len(set(ids)):
            raise ValueError("agent_id values must be unique.")

        self.identities = {x.agent_id: x for x in identities}
        self.states = {x.agent_id: AgentRuntimeState() for x in identities}

        self._alias_to_id: dict[str, str] = {}
        for identity in identities:
            for alias in identity.all_names():
                key = self._norm(alias)
                existing = self._alias_to_id.get(key)
                if existing and existing != identity.agent_id:
                    raise ValueError(f"Alias collision: {alias!r}")
                self._alias_to_id[key] = identity.agent_id

    @staticmethod
    def _norm(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().casefold())

    @staticmethod
    def _contains_alias(text: str, alias: str) -> bool:
        escaped = re.escape(alias)
        pattern = rf"(?<!\w)@?{escaped}(?!\w)"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None

    def resolve_targets(self, message: GroupMessage) -> tuple[str, ...]:
        if message.broadcast:
            return tuple(self.identities)

        explicit = [x for x in message.explicit_target_ids if x in self.identities]
        if explicit:
            return tuple(dict.fromkeys(explicit))

        if message.reply_to_agent_id in self.identities:
            return (message.reply_to_agent_id,)  # reply metadata is authoritative

        found: list[str] = []
        text = message.text or ""
        # Longest aliases first prevents a short alias from shadowing a longer one.
        aliases = sorted(self._alias_to_id, key=len, reverse=True)
        for alias in aliases:
            if self._contains_alias(text, alias):
                agent_id = self._alias_to_id[alias]
                if agent_id not in found:
                    found.append(agent_id)

        broadcast_words = ("everyone", "everybody", "all agents", "team")
        if not found and any(self._contains_alias(text, word) for word in broadcast_words):
            return tuple(self.identities)

        return tuple(found)

    def route(self, message: GroupMessage) -> dict[str, AgentDelivery]:
        targets = self.resolve_targets(message)
        target_set = set(targets)
        is_broadcast = len(target_set) == len(self.identities) and bool(target_set)

        deliveries: dict[str, AgentDelivery] = {}
        for agent_id, identity in self.identities.items():
            if agent_id in target_set:
                mode = FloorMode.BROADCAST if is_broadcast else FloorMode.RESPOND
                can_emit = True
                self.states[agent_id].active_floor = True
                text = (
                    f"GROUP TURN. You were explicitly addressed as {identity.display_name}. "
                    "You have permission to respond to the group. Preserve your current task state."
                )
            else:
                mode = FloorMode.OBSERVE
                can_emit = False
                self.states[agent_id].active_floor = False
                text = (
                    "GROUP AWARENESS EVENT. You were not addressed. Do not respond to this message. "
                    "Keep working on your current task and incorporate the event only if relevant."
                )
                self.states[agent_id].enqueue_awareness(
                    f"{message.sender_id}: {message.text}"
                )

            deliveries[agent_id] = AgentDelivery(
                agent_id=agent_id,
                mode=mode,
                can_emit=can_emit,
                text=text,
                addressed_agents=targets,
            )
        return deliveries

    def consume_awareness_context(self, agent_id: str) -> str:
        if agent_id not in self.states:
            raise KeyError(agent_id)
        items = self.states[agent_id].drain_awareness()
        if not items:
            return ""
        body = "\n".join(f"- {item}" for item in items)
        return (
            "RECENT GROUP EVENTS:\n"
            f"{body}\n"
            "You were not granted the floor for these events. Use them only as context and continue your task."
        )

    @staticmethod
    def gate_output(delivery: AgentDelivery, generated_text: str) -> str | None:
        """Authoritative transport gate.

        Even if a non-addressed model generates text, it is not publishable.
        """
        if not delivery.can_emit:
            return None
        text = (generated_text or "").strip()
        return text or None
