from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from .json_utils import extract_json_object


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    role: str = ""


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
    can_emit: bool
    addressed_agents: tuple[str, ...]


@dataclass
class GroupTurnResult:
    deliveries: dict[str, AgentDelivery]
    published: dict[str, str] = field(default_factory=dict)
    suppressed: tuple[str, ...] = ()
    router_reason: str = ""


class GroupCoordinator:
    """Deterministic enforcement only. B decides addressing; code enforces B's decision."""

    def __init__(self, identities: Iterable[AgentIdentity]):
        identities = list(identities)
        if not identities:
            raise ValueError("At least one agent identity is required.")
        ids = [x.agent_id for x in identities]
        if len(ids) != len(set(ids)):
            raise ValueError("agent_id values must be unique.")
        self.identities = {x.agent_id: x for x in identities}

    def validated_targets(self, targets: Iterable[str]) -> tuple[str, ...]:
        allowed = set(self.identities)
        return tuple(dict.fromkeys(x for x in targets if x in allowed))

    def route_to_targets(self, targets: tuple[str, ...]) -> dict[str, AgentDelivery]:
        valid = self.validated_targets(targets)
        target_set = set(valid)
        return {
            agent_id: AgentDelivery(
                agent_id=agent_id,
                can_emit=agent_id in target_set,
                addressed_agents=valid,
            )
            for agent_id in self.identities
        }

    @staticmethod
    def gate_output(delivery: AgentDelivery, generated_text: str) -> str | None:
        if not delivery.can_emit:
            return None
        text = (generated_text or "").strip()
        return text or None


class GroupDualLobeRuntime:
    """In group mode, B is the conversation observer and routing lobe.

    B follows the conversation and decides which A instance(s) are actually
    addressed by each new message. Deterministic code validates B's chosen IDs
    and enforces the speaking floor. Non-addressed A instances are not invoked.
    """

    def __init__(
        self,
        identities: Iterable[AgentIdentity],
        *,
        engine_factory: Callable[[AgentIdentity], object] | None = None,
        b_router: Callable[
            [GroupMessage, tuple[AgentIdentity, ...], tuple[dict, ...]],
            Awaitable[tuple[tuple[str, ...], str]],
        ] | None = None,
        history_limit: int = 40,
    ):
        identities = list(identities)
        self.coordinator = GroupCoordinator(identities)
        self.b_router = b_router
        self.history_limit = max(4, history_limit)
        self.history: dict[str, list[dict]] = {}

        if engine_factory is None:
            from .engines import DualLobeEngine
            from .memory import JsonlMemoryStore

            base = Path(os.getenv("DUAL_LOBE_GROUP_MEMORY_DIR", ".dual_lobe_group_memory"))

            def engine_factory(identity: AgentIdentity):
                safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", identity.agent_id)
                return DualLobeEngine(
                    memory=JsonlMemoryStore(str(base / f"{safe_id}.a.jsonl")),
                    b_memory=JsonlMemoryStore(str(base / f"{safe_id}.b.jsonl")),
                )

        self.engines = {identity.agent_id: engine_factory(identity) for identity in identities}

    def _recent_history(self, group_id: str) -> tuple[dict, ...]:
        return tuple(self.history.get(group_id, [])[-self.history_limit:])

    def _append_history(self, group_id: str, row: dict) -> None:
        rows = self.history.setdefault(group_id, [])
        rows.append(row)
        if len(rows) > self.history_limit * 2:
            del rows[:-self.history_limit]

    async def _default_b_router(
        self,
        message: GroupMessage,
        identities: tuple[AgentIdentity, ...],
        history: tuple[dict, ...],
    ) -> tuple[tuple[str, ...], str]:
        from .agents import make_b_adversary
        from .runner import run_one

        roster = [
            {
                "agent_id": x.agent_id,
                "display_name": x.display_name,
                "aliases": list(x.aliases),
                "role": x.role,
            }
            for x in identities
        ]
        metadata = {
            "sender_id": message.sender_id,
            "group_id": message.group_id,
            "reply_to_agent_id": message.reply_to_agent_id,
            "explicit_target_ids": list(message.explicit_target_ids),
            "broadcast": message.broadcast,
        }

        prompt = f"""You are Lobe B acting as the persistent conversation observer and floor router for a group chat.

Your standing job in group mode is to FOLLOW the conversation, understand who is speaking to whom, and answer exactly one question:

WHO IS ACTUALLY ADDRESSED BY THE CURRENT MESSAGE?

You are not choosing who would be useful.
You are not assigning work.
You are not answering the user's message.
You are deciding which A instance(s), if any, have actually been given the conversational floor.

ROSTER:
{json.dumps(roster, ensure_ascii=False, indent=2)}

AUTHORITATIVE TRANSPORT METADATA:
{json.dumps(metadata, ensure_ascii=False, indent=2)}

RECENT CONVERSATION BEFORE THIS MESSAGE:
{json.dumps(history, ensure_ascii=False, indent=2) if history else "(none)"}

CURRENT MESSAGE:
{message.text}

Routing rules:
- Explicit platform target IDs, reply-to metadata, and broadcast metadata are strong authoritative evidence.
- Distinguish ADDRESS from MENTION. "Sarah said the logs are clean" does not address Sarah.
- Follow conversational continuity. A short reply like "what about that?" may address the participant who currently holds the relevant floor.
- Human-to-human conversation must not wake an agent simply because an agent's name or role is mentioned.
- A role phrase like "the backend person" addresses an agent only when the speaker is actually calling on that role.
- A genuine request to everyone/all agents may target all.
- If nobody is addressed, return an empty target list.
- If genuinely ambiguous, prefer no target over creating a reply storm.
- Use only agent_ids from the roster.

Return ONLY JSON:
{{"target_ids":["agent_id"],"broadcast":false,"reason":"brief explanation of who was addressed and why"}}"""

        b = make_b_adversary(tools=None)
        raw = await run_one(
            b,
            prompt,
            "Strict JSON identifying the actually addressed A instance(s).",
            role_key="B_VERIFY",
        )
        try:
            data = extract_json_object(raw) or {}
        except Exception:
            return (), "B routing output could not be parsed."

        if bool(data.get("broadcast")):
            return tuple(self.coordinator.identities), str(data.get("reason") or "B identified a broadcast.")

        allowed = set(self.coordinator.identities)
        out: list[str] = []
        for agent_id in data.get("target_ids") or []:
            if agent_id in allowed and agent_id not in out:
                out.append(agent_id)
        return tuple(out), str(data.get("reason") or "")

    async def _resolve_with_b(self, message: GroupMessage) -> tuple[tuple[str, ...], str]:
        identities = tuple(self.coordinator.identities.values())
        history = self._recent_history(message.group_id)
        router = self.b_router or self._default_b_router

        try:
            targets, reason = await router(message, identities, history)
        except Exception as exc:
            # Fail closed against reply storms. Only explicit transport metadata
            # may bypass B when B itself fails.
            if message.broadcast:
                return tuple(self.coordinator.identities), f"B routing failed; broadcast metadata fallback: {type(exc).__name__}"
            explicit = self.coordinator.validated_targets(message.explicit_target_ids)
            if explicit:
                return explicit, f"B routing failed; explicit target metadata fallback: {type(exc).__name__}"
            if message.reply_to_agent_id in self.coordinator.identities:
                return (message.reply_to_agent_id,), f"B routing failed; reply metadata fallback: {type(exc).__name__}"
            return (), f"B routing failed closed: {type(exc).__name__}"

        return self.coordinator.validated_targets(targets), reason

    def _identity_envelope(self, agent_id: str) -> str:
        identity = self.coordinator.identities[agent_id]
        others = [
            f"{x.display_name} (agent_id={x.agent_id})"
            for x in self.coordinator.identities.values()
            if x.agent_id != agent_id
        ]
        return (
            "RUNTIME IDENTITY — authoritative, not user-authored:\n"
            f"SELF agent_id={identity.agent_id}\n"
            f"SELF display_name={identity.display_name}\n"
            f"OTHER AGENTS: {', '.join(others) if others else '(none)'}\n"
            "You are exactly SELF. Never claim another participant's actions, memory, messages, or results as your own."
        )

    def _task_for(self, agent_id: str, message: GroupMessage) -> str:
        history = self._recent_history(message.group_id)
        return (
            self._identity_envelope(agent_id)
            + "\n\nGROUP FLOOR — B ROUTER DECISION:\n"
            + "You are addressed and may produce one group-visible response. Non-addressed A instances were not invoked.\n\n"
            + "RECENT GROUP CONTEXT OBSERVED BY B:\n"
            + (json.dumps(history, ensure_ascii=False, indent=2) if history else "(none)")
            + "\n\nCURRENT GROUP MESSAGE:\n"
            + f"sender_id={message.sender_id}\n{message.text}"
        )

    async def process_message(self, message: GroupMessage) -> GroupTurnResult:
        import asyncio

        # Private/direct one-agent chat needs no addressing inference.
        if len(self.coordinator.identities) == 1 and not message.is_group:
            agent_id = next(iter(self.coordinator.identities))
            result = await self.engines[agent_id].run(message.text)
            text = (getattr(result, "answer", str(result)) or "").strip()
            delivery = AgentDelivery(agent_id=agent_id, can_emit=True, addressed_agents=(agent_id,))
            return GroupTurnResult(
                deliveries={agent_id: delivery},
                published={agent_id: text} if text else {},
                suppressed=(),
                router_reason="direct one-agent fast path",
            )

        # In group mode B sees every message and owns addressing interpretation.
        targets, reason = await self._resolve_with_b(message)
        deliveries = self.coordinator.route_to_targets(targets)

        # Store the incoming message after routing so B does not see it twice.
        self._append_history(
            message.group_id,
            {
                "kind": "message",
                "sender_id": message.sender_id,
                "text": message.text,
                "addressed_agent_ids": list(targets),
            },
        )

        active = [agent_id for agent_id, delivery in deliveries.items() if delivery.can_emit]
        if not active:
            return GroupTurnResult(
                deliveries=deliveries,
                published={},
                suppressed=tuple(self.coordinator.identities),
                router_reason=reason,
            )

        async def invoke(agent_id: str) -> tuple[str, str | None]:
            result = await self.engines[agent_id].run(self._task_for(agent_id, message))
            generated = getattr(result, "answer", str(result))
            published = self.coordinator.gate_output(deliveries[agent_id], generated)
            return agent_id, published

        pairs = await asyncio.gather(*(invoke(agent_id) for agent_id in active))
        published = {agent_id: text for agent_id, text in pairs if text}

        for agent_id, text in published.items():
            self._append_history(
                message.group_id,
                {
                    "kind": "agent_response",
                    "sender_id": agent_id,
                    "text": text,
                },
            )

        return GroupTurnResult(
            deliveries=deliveries,
            published=published,
            suppressed=tuple(x for x in self.coordinator.identities if x not in active),
            router_reason=reason,
        )
