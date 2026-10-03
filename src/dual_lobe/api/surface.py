"""Surface context (Companion Phase 2): where the user is talking from.

A surface may tell the server where it is - device, app - so the persona can be
aware of context. This is **ephemeral request context**: it shapes the current
turn, it is never written to long-term memory as content. The rule from
COMPANION.md: *memory is what the user said; surface is where they said it.*

Provenance columns on memory notes record the surface so cross-surface recall
("you were on your phone when...") can work, but the surface itself never
becomes memory content.
"""
from __future__ import annotations

import re

_SURFACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
MAX_SURFACE = 64


class SurfaceContext:
    """Validated, ephemeral (device, app) pair for one request."""

    __slots__ = ("device", "app")

    def __init__(self, device: str = "", app: str = "") -> None:
        self.device = device
        self.app = app

    def __bool__(self) -> bool:
        return bool(self.device or self.app)

    def __eq__(self, other) -> bool:
        return (isinstance(other, SurfaceContext)
                and self.device == other.device and self.app == other.app)

    def __repr__(self) -> str:
        return f"SurfaceContext(device={self.device!r}, app={self.app!r})"

    def ephemeral_line(self) -> str:
        """One bounded line A may see this turn only. No history, no memory."""
        parts = []
        if self.device:
            parts.append(f"device: {self.device}")
        if self.app:
            parts.append(f"app: {self.app}")
        if not parts:
            return ""
        return ("The user is talking to you from a surface this turn "
                f"({'; '.join(parts)}). This is where they are, not what they said; "
                "use it only if immediately relevant.")


def _clean(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value:
        return ""
    match = _SURFACE_RE.search(value)
    return match.group(0) if match else ""


def parse_surface(headers) -> SurfaceContext:
    """Read X-DL-Surface-Device / X-DL-Surface-App headers.

    Bounded to 64 chars of safe charset; anything else is dropped, never an
    error - surface context is an enhancement and must never reject a request.
    """
    return SurfaceContext(device=_clean(headers.get("X-DL-Surface-Device")),
                           app=_clean(headers.get("X-DL-Surface-App")))