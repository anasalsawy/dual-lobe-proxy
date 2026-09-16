"""Release policy for scoped evidence."""

from __future__ import annotations

from collections.abc import Iterable

from .models import EvidenceCoverage, ReleaseDecision

_BLOCKING = {"UNKNOWN", "STALE", "CONFLICTING"}


def decide_release(
    coverage: Iterable[EvidenceCoverage],
    *,
    enabled: bool = False,
) -> ReleaseDecision:
    """Return a conservative decision; disabled gates never block normal mode."""

    items = list(coverage)
    if not enabled:
        return ReleaseDecision(
            decision="QUALIFY",
            blocking_criteria=[],
            user_visible_reason="The release gate is disabled; evidence remains advisory.",
        )
    blocked = [item.criterion_id for item in items if item.status in _BLOCKING]
    partial = [item.criterion_id for item in items if item.status == "PARTIAL"]
    if blocked:
        return ReleaseDecision(
            decision="HOLD",
            blocking_criteria=blocked,
            user_visible_reason="Required criteria lack fresh, non-conflicting evidence.",
        )
    if partial:
        return ReleaseDecision(
            decision="QUALIFY",
            blocking_criteria=partial,
            user_visible_reason="Some criteria have only partial evidence.",
        )
    return ReleaseDecision(
        decision="RELEASE",
        blocking_criteria=[],
        user_visible_reason="All criteria have matching, fresh, non-conflicting evidence.",
    )
