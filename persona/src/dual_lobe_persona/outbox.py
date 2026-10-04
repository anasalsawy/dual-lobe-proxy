"""Outbound mailbox for proactive persona messages.

Channel-agnostic by design: the runtime never talks to WhatsApp, email, or
SMS itself. It appends outbound messages to a per-persona outbox; channel
bridges (one per transport, with the operator's own credentials) drain the
outbox and deliver. Rate limits and quiet hours live here so no adapter
can spam a user even if it wants to.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

import structlog

from .workspace import persona_slug, persona_workspace_dir

LOG = structlog.get_logger(__name__)

MAX_OUTREACH_PER_DAY = int(os.environ.get("DUAL_LOBE_PERSONA_OUTREACH_MAX_PER_DAY", "3"))
QUIET_HOURS = os.environ.get("DUAL_LOBE_PERSONA_QUIET_HOURS", "22:00-08:00")


def _same_day(a: float, b: float) -> bool:
    return time.strftime("%Y-%m-%d", time.localtime(a)) == time.strftime(
        "%Y-%m-%d", time.localtime(b)
    )


def _in_quiet_hours(now: float | None = None) -> bool:
    """True inside the configured quiet window (server local time).

    The window may span midnight (22:00-08:00). Malformed config = never
    quiet (fail open on delivery, fail closed on spam via rate limit).
    """
    try:
        start_s, end_s = QUIET_HOURS.split("-")
        start = tuple(int(p) for p in start_s.strip().split(":"))
        end = tuple(int(p) for p in end_s.strip().split(":"))
        if not (len(start) == len(end) == 2):
            return False
    except Exception:  # noqa: BLE001
        return False
    import datetime

    now_t = datetime.datetime.fromtimestamp(now) if now else datetime.datetime.now()
    minutes = now_t.hour * 60 + now_t.minute
    s = start[0] * 60 + start[1]
    e = end[0] * 60 + end[1]
    if s <= e:
        return s <= minutes < e
    return minutes >= s or minutes < e


@dataclass
class OutboxMessage:
    id: str
    persona: str
    channel: str
    to: str
    text: str
    created_at: float
    sent_at: float | None = None
    reason: str = "heartbeat"
    meta: dict = field(default_factory=dict)


class Outbox:
    """JSONL-backed outbox, one file per persona, thread-safe.

    Volume is a handful of messages per day per persona; a JSONL file with
    an in-process lock is auditable by humans and trivially backed up with
    the workspace volume. Swap for a queue when volume demands it.
    """

    def __init__(self, persona_key: str) -> None:
        self.persona_key = persona_key
        self.slug = persona_slug(persona_key)
        self.path = persona_workspace_dir(persona_key) / "outbox.jsonl"
        self._lock = Lock()
        self.path.touch(exist_ok=True)

    def _load(self) -> list[OutboxMessage]:
        rows: list[OutboxMessage] = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rows.append(OutboxMessage(**json.loads(line)))
            except (TypeError, ValueError):
                continue
        return rows

    def _write(self, rows: list[OutboxMessage]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(asdict(r)) + "\n" for r in rows))
        tmp.replace(self.path)

    def outreach_today(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        with self._lock:
            return sum(1 for r in self._load() if _same_day(r.created_at, now))

    def append(self, channel: str, to: str, text: str, reason: str = "heartbeat",
               now: float | None = None) -> OutboxMessage | None:
        """Queue one outbound message unless limits say otherwise.

        Returns the queued message, or None when the append was refused
        (daily cap reached or quiet hours). Refusals are policy, not
        errors: the persona simply does not reach out.
        """
        now = now if now is not None else time.time()
        if _in_quiet_hours(now):
            return None
        with self._lock:
            rows = self._load()
            # The cap counts everything the persona said today — queued or
            # already delivered. A slow bridge must not become a spam loophole.
            spoken_today = sum(1 for r in rows if _same_day(r.created_at, now))
            if spoken_today >= MAX_OUTREACH_PER_DAY:
                return None
            import uuid

            msg = OutboxMessage(
                id=f"out-{uuid.uuid4().hex[:20]}",
                persona=self.slug,
                channel=channel,
                to=to,
                text=text,
                created_at=now,
                reason=reason,
            )
            rows.append(msg)
            self._write(rows)
            LOG.info("outbox_appended", persona=self.slug, channel=channel, reason=reason)
            return msg

    def pending(self) -> list[OutboxMessage]:
        with self._lock:
            return [r for r in self._load() if r.sent_at is None]

    def drain(self, limit: int = 10) -> list[OutboxMessage]:
        """Hand unsent messages to a bridge and mark them sent."""
        with self._lock:
            rows = self._load()
            out = [r for r in rows if r.sent_at is None][:limit]
            now = time.time()
            for r in out:
                r.sent_at = now
            self._write(rows)
            return out