"""Bounded artifact context supplied by the connected agent runtime.

The proxy does not discover a workspace itself. The host runtime supplies a
per-turn inventory (and, when requested, the artifact contents) through the
inference request. Artifact records are evidence data, never instructions.
"""
from __future__ import annotations

import json
from typing import Any

from ..core.redact import redact_payload
from .prompts import head_tail


def bounded_artifacts(value: Any, budget: int = 12000) -> list[dict[str, Any]]:
    """Keep a valid, redacted artifact inventory within B's baseline budget."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        item = redact_payload(raw)
        # Artifact contents are optional baseline context. Full contents should
        # be returned by the host on B's targeted artifact request instead.
        if isinstance(item.get("content"), str) and len(item["content"]) > 2000:
            item["content"] = head_tail(item["content"], 2000)
            item["content_truncated"] = True
        candidate = [*result, item]
        if len(json.dumps(candidate, ensure_ascii=False)) > budget:
            break
        result.append(item)
    return result
