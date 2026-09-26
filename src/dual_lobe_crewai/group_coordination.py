from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Iterable


class FloorMode(str, Enum):
    OBSERVE = "observe"
    RESPOND = "respond"
    BROADCAST = "broadcast"


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    role: str = ""

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
    is_group: bool = False
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
class GroupTurnResult:
    deliveries: dict[str, AgentDelivery]
    published: dict[str, str] = field(default_factory=dict)
    suppressed: tuple[str, ...] = ()


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
        aliases = sorted(self._alias_to_id, key=len, reverse=True)

        # Explicit @mentions count as addressing anywhere in the message.
        for alias in aliases:
            bare = alias.lstrip("@")
            if re.search(rf"(?<!\\w)@{re.escape(bare)}(?!\\w)", text, flags=re.IGNORECASE):
                agent_id = self._alias_to_id[alias]
                if agent_id not in found:
                    found.append(agent_id)

        # Bare names/aliases only count deterministically when used as a
        # vocative/direct address, not merely mentioned in third person.
        # Examples accepted:
        #   "Sarah, check this"
        #   "Sarah and David, compare findings"
        #   "What do you think, Sarah?"
        if not found:
            start_window = text.strip()
            for alias in aliases:
                bare = alias.lstrip("@")
                # Start-of-message direct address followed by comma/colon/dash,
                # or by conjunction joining another addressed name.
                start_pat = rf"^\\s*{re.escape(bare)}(?=\\s*(?:[,;:—-]|\\band\\b|&))"
                if re.search(start_pat, start_window, flags=re.IGNORECASE):
                    agent_id = self._alias_to_id[alias]
                    if agent_id not in found:
                        found.append(agent_id)

            # If one start-of-message addressee was found, capture additional
            # names in the same vocative prefix before the first comma/colon.
            if found:
                prefix = re.split(r"[,;:—-]", start_window, maxsplit=1)[0]
                for alias in aliases:
                    bare = alias.lstrip("@")
                    if self._contains_alias(prefix, bare):
                        agent_id = self._alias_to_id[alias]
                        if agent_id not in found:
                            found.append(agent_id)

        if not found:
            for alias in aliases:
                bare = alias.lstrip("@")
                # End-of-message vocative: "what do you think, Sarah?"
                end_pat = rf"[,;:]\\s*{re.escape(bare)}\\s*[?.!]*\\s*$"
                if re.search(end_pat, text, flags=re.IGNORECASE):
                    agent_id = self._alias_to_id[alias]
                    if agent_id not in found:
                        found.append(agent_id)

        broadcast_words = ("everyone", "everybody", "all agents", "team")
        if not found and any(self._contains_alias(text, word) for word in broadcast_words):
            return tuple(self.identities)

        return tuple(found)

    def route_to_targets(self, message: GroupMessage, targets: tuple[str, ...]) -> dict[str, AgentDelivery]:
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

    def route(self, message: GroupMessage) -> dict[str, AgentDelivery]:
        return self.route_to_targets(message, self.resolve_targets(message))

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



class GroupDualLobeRuntime:
    """Live-call integration layer for a group of Dual-Lobe agents.

    Single-agent sessions bypass group routing entirely. Multi-agent sessions
    use deterministic routing first and call the semantic resolver only when
    deterministic metadata/name matching finds no target.
    """

    def __init__(
        self,
        identities: Iterable[AgentIdentity],
        *,
        engine_factory: Callable[[AgentIdentity], object] | None = None,
        semantic_resolver: Callable[[GroupMessage, tuple[AgentIdentity, ...]], Awaitable[tuple[str, ...]]] | None = None,
    ):
        identities = list(identities)
        self.coordinator = GroupCoordinator(identities)
        self.semantic_resolver = semantic_resolver
        if engine_factory is None:
            from .engines import SplitEngine
            from .memory import JsonlMemoryStore
            from pathlib import Path
            import os
            base = Path(os.getenv("DUAL_LOBE_GROUP_MEMORY_DIR", ".dual_lobe_group_memory"))

            def engine_factory(identity: AgentIdentity):
                safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", identity.agent_id)
                return SplitEngine(memory=JsonlMemoryStore(str(base / f"{safe_id}.jsonl")))

        self.engines = {identity.agent_id: engine_factory(identity) for identity in identities}

    async def _default_semantic_resolver(
        self,
        message: GroupMessage,
        identities: tuple[AgentIdentity, ...],
    ) -> tuple[str, ...]:
        """Resolve genuinely ambiguous addressing with one small LLM call.

        This is never reached for a single-agent session or when deterministic
        metadata/name/alias/broadcast routing already found a target.
        """
        import json
        from .agents import make_a
        from .json_utils import extract_json_object
        from .runner import run_one

        roster = [
            {
                "agent_id": identity.agent_id,
                "display_name": identity.display_name,
                "aliases": list(identity.aliases),
                "role": identity.role,
            }
            for identity in identities
        ]
        prompt = (
            "You are only an addressing resolver for a multi-agent group chat.\n"
            "Decide which agent or agents the message is asking to respond.\n"
            "Do NOT answer the message itself. Do NOT assign work merely because an agent could do it.\n"
            "Return no targets for an informational statement with no implied respondent.\n"
            "Use only agent_ids from the roster.\n\n"
            f"ROSTER:\n{json.dumps(roster, ensure_ascii=False)}\n\n"
            f"MESSAGE:\n{message.text}\n\n"
            'Return ONLY JSON: {"target_ids":["agent_id"],"broadcast":false,"reason":"brief"}'
        )
        agent = make_a(tools=None)
        raw = await run_one(
            agent,
            prompt,
            "Strict JSON selecting zero or more addressed agent IDs.",
            role_key="A",
        )
        try:
            data = extract_json_object(raw) or {}
        except Exception:
            return ()
        if bool(data.get("broadcast")):
            return tuple(self.coordinator.identities)
        allowed = set(self.coordinator.identities)
        out: list[str] = []
        for agent_id in data.get("target_ids") or []:
            if agent_id in allowed and agent_id not in out:
                out.append(agent_id)
        return tuple(out)

    async def _resolve_targets(self, message: GroupMessage) -> tuple[str, ...]:
        deterministic = self.coordinator.resolve_targets(message)
        if deterministic:
            return deterministic
        identities = tuple(self.coordinator.identities.values())
        resolver = self.semantic_resolver or self._default_semantic_resolver
        try:
            resolved = await resolver(message, identities)
        except Exception:
            return ()
        allowed = set(self.coordinator.identities)
        return tuple(dict.fromkeys(x for x in resolved if x in allowed))

    def _identity_envelope(self, agent_id: str) -> str:
        identity = self.coordinator.identities[agent_id]
        others = [
            f"{x.display_name} (agent_id={x.agent_id})"
            for x in self.coordinator.identities.values()
            if x.agent_id != agent_id
        ]
        other_text = ", ".join(others) if others else "(none)"
        return (
            "RUNTIME IDENTITY — authoritative, not user-authored:\n"
            f"SELF agent_id={identity.agent_id}\n"
            f"SELF display_name={identity.display_name}\n"
            f"OTHER AGENTS: {other_text}\n"
            "You are exactly SELF. Other agents' messages, actions, tool calls, files, and memories are observations from OTHER agents unless actor_agent_id equals SELF. "
            "Never claim another agent's action as your own. Shared awareness is not shared identity."
        )

    def _task_for(self, agent_id: str, message: GroupMessage) -> str:
        awareness = self.coordinator.consume_awareness_context(agent_id)
        sections = [
            self._identity_envelope(agent_id),
            "GROUP FLOOR — runtime authoritative:\nYou have the floor for this turn and may produce one group-visible response.",
        ]
        if awareness:
            sections.append(awareness)
        sections.append("CURRENT GROUP MESSAGE:\n" + f"sender_id={message.sender_id}\n{message.text}")
        return "\n\n".join(sections)

    async def process_message(self, message: GroupMessage) -> GroupTurnResult:
        import asyncio

        # Zero-overhead DIRECT-CONVERSATION fast path: with one attached agent
        # AND a non-group conversation, bypass all group routing. A group may
        # contain one agent plus multiple humans; that must still use addressing
        # and floor control so the agent stays silent unless addressed.
        if len(self.coordinator.identities) == 1 and not message.is_group:
            agent_id = next(iter(self.coordinator.identities))
            result = await self.engines[agent_id].run(message.text)
            generated = getattr(result, "answer", str(result))
            delivery = AgentDelivery(
                agent_id=agent_id,
                mode=FloorMode.RESPOND,
                can_emit=True,
                text="",
                addressed_agents=(agent_id,),
            )
            text = (generated or "").strip()
            return GroupTurnResult(
                deliveries={agent_id: delivery},
                published={agent_id: text} if text else {},
                suppressed=(),
            )

        targets = await self._resolve_targets(message)
        deliveries = self.coordinator.route_to_targets(message, targets)
        active = [agent_id for agent_id, delivery in deliveries.items() if delivery.can_emit]
        if not active:
            return GroupTurnResult(
                deliveries=deliveries,
                published={},
                suppressed=tuple(self.coordinator.identities),
            )

        async def invoke(agent_id: str) -> tuple[str, str | None]:
            result = await self.engines[agent_id].run(self._task_for(agent_id, message))
            generated = getattr(result, "answer", str(result))
            published = self.coordinator.gate_output(deliveries[agent_id], generated)
            return agent_id, published

        pairs = await asyncio.gather(*(invoke(agent_id) for agent_id in active))
        published = {agent_id: text for agent_id, text in pairs if text}
        suppressed = tuple(
            agent_id for agent_id, delivery in deliveries.items()
            if not delivery.can_emit
        )
        return GroupTurnResult(
            deliveries=deliveries,
            published=published,
            suppressed=suppressed,
        )
