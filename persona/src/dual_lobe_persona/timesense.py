"""Time sense: the persona's awareness of time passing.

Friends know how long it has been. The runtime measures two clocks per
persona, on the server, and injects both into the conversation:

  - GAP: time since the previous exchange completed ('it has been 2 days')
  - DURATION: how long each exchange took, to the second ('that took 14s')

Both live in clock.json in the persona's workspace with a running
seconds count the persona can read directly. Integrity: all times are
measured on the server clock — the persona references them, never
invents them.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock

import structlog

from .workspace import persona_workspace_dir

LOG = structlog.get_logger(__name__)


def _describe_gap(seconds: float) -> str:
    """Human-scale description of an elapsed gap, the way a friend would say it."""
    if seconds < 90:
        return "moments"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} minute{'s' if minutes >= 2 else ''}"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} hour{'s' if hours >= 2 else ''}"
    days = hours / 24
    if days < 7:
        return f"{int(days)} day{'s' if days >= 2 else ''}"
    if days < 30:
        return f"{int(days / 7)} week{'s' if days >= 14 else ''}"
    months = days / 30
    if months < 12:
        return f"{int(months)} month{'s' if months >= 2 else ''}"
    years = days / 365
    return f"{int(years)} year{'s' if years >= 2 else ''}"


def _describe_duration(seconds: float) -> str:
    """How long an exchange took. In-conversation latency is felt in seconds."""
    if seconds < 1:
        return "under a second"
    return f"{seconds:.0f} seconds"


class TimeSense:
    """Per-persona clock: gaps, turn durations, and a live seconds count."""

    def __init__(self, persona_key: str) -> None:
        self.persona_key = persona_key
        self.path: Path = persona_workspace_dir(persona_key) / "clock.json"
        self._lock = Lock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    # -- gaps -----------------------------------------------------------

    def elapsed_since_last_seen(self, now: float | None = None) -> float | None:
        """Seconds since the previous exchange completed; None on first contact."""
        with self._lock:
            data = self._read()
        last = data.get("last_seen")
        if not last:
            return None
        return (now if now is not None else time.time()) - float(last)

    def gap_line(self, now: float | None = None) -> str:
        gap = self.elapsed_since_last_seen(now)
        if gap is None:
            return ""
        return f"[time] It has been {_describe_gap(gap)} ({gap:.0f} seconds) since you two last talked."

    # -- turn durations ---------------------------------------------------

    def mark_turn_started(self, now: float | None = None) -> float:
        """Stamp the request-received instant; returns a monotonic handle."""
        now = now if now is not None else time.time()
        with self._lock:
            data = self._read()
            data["last_turn"] = {"started": now, "ended": None}
            self._write(data)
        return time.monotonic()

    def mark_turn_ended(self, mono_start: float, now: float | None = None) -> float:
        """Stamp the response-sent instant; returns the wall duration in seconds."""
        now = now if now is not None else time.time()
        duration = time.monotonic() - mono_start
        with self._lock:
            data = self._read()
            turn = data.get("last_turn") or {}
            turn["ended"] = now
            turn["duration_seconds"] = round(duration, 2)
            data["last_turn"] = turn
            data["last_seen"] = now
            self._write(data)
        return duration

    def duration_line(self, duration_seconds: float | None = None) -> str:
        """Context line describing how long the just-finished exchange took."""
        if duration_seconds is None:
            with self._lock:
                data = self._read()
            duration_seconds = (data.get("last_turn") or {}).get("duration_seconds")
        if duration_seconds is None:
            return ""
        return f"[time] That exchange took {_describe_duration(duration_seconds)}."

    # -- the visible clock -------------------------------------------------

    def context_line(self, now: float | None = None) -> str:
        """Full time context for the next turn: gap + previous duration.

        The persona feels time as a friend does (days, hours) and can read
        the raw running seconds count for precision.
        """
        now = now if now is not None else time.time()
        gap = self.elapsed_since_last_seen(now)
        with self._lock:
            data = self._read()
        turn = data.get("last_turn") or {}
        parts: list[str] = []
        if gap is not None:
            parts.append(f"it has been {_describe_gap(gap)} ({gap:.0f} seconds) "
                         f"since you two last talked")
        if turn.get("duration_seconds") is not None:
            parts.append(f"the exchange before that took "
                         f"{_describe_duration(turn['duration_seconds'])}")
        if not parts:
            return ""
        return "[time] " + "; ".join(parts) + "."

    def clock_state(self, now: float | None = None) -> dict:
        """Machine-readable clock for clients and the API."""
        now = now if now is not None else time.time()
        with self._lock:
            data = self._read()
        gap = self.elapsed_since_last_seen(now)
        return {
            "now": now,
            "seconds_since_last_seen": round(gap, 1) if gap is not None else None,
            "last_turn": data.get("last_turn") or {},
        }

    def stamp(self, now: float | None = None) -> None:
        """Compatibility: stamp an exchange as completed."""
        self.mark_turn_ended(time.monotonic(), now)