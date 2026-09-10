"""Deterministic artifact sensing and observer evidence persistence.

Reads only; never executes. Paths are contained under a configured root with no
symlink escape. Gathered material is stored to the observer's own shared-memory
space as reported evidence (recorded statements, never verified truth).
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import insert

from ..core.engine import tenant_session
from ..core.models import MemoryEntry, MemorySpace
from ..core.settings import get_settings
from .prompts import head_tail

LOG = logging.getLogger("dual_lobe.b.artifacts")


def _blocked(path: Path) -> bool:
    name = path.name
    if name in {".env", ".env.local", "id_rsa", "id_dsa"}:
        return True
    return any(name.endswith(ext) for ext in (".pem", ".key", ".p12", ".pfx"))


def resolve_artifact(ref: str, root: str | None) -> Path | None:
    """Resolve an artifact reference inside root with containment + no symlink escape."""
    try:
        base = Path(root).expanduser().resolve() if root else None
    except OSError:
        return None
    if base is None:
        return None
    try:
        candidate = (base / ref).resolve(strict=False)
    except OSError:
        return None
    if not candidate.is_relative_to(base):
        LOG.warning("Observer artifact outside root: %s", ref)
        return None
    if _blocked(candidate) or candidate.is_symlink():
        return None
    return candidate


async def read_artifact(ref: str, *, max_bytes: int = 6000) -> dict:
    s = get_settings()
    path = resolve_artifact(ref, s.artifact_root)
    if path is None:
        return {"ok": False, "tool": "read_artifact", "label": ref,
                "error": "artifact not readable (no root configured or outside root)"}
    try:
        if not path.is_file():
            return {"ok": False, "tool": "read_artifact", "label": ref,
                    "error": "path exists but is not a file"}
        raw = path.read_text(encoding="utf-8", errors="ignore")
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes] + "\n[truncated]"
        return {"ok": True, "tool": "read_artifact", "label": str(path),
                "text": raw, "truncated": truncated, "path": str(path)}
    except OSError as exc:
        return {"ok": False, "tool": "read_artifact", "label": ref,
                "error": f"artifact read failed: {type(exc).__name__}"}


def sensed_text(results: list[dict]) -> str:
    entries = []
    for r in results:
        excerpt = head_tail(r.get("text") or r.get("error") or "", 6000)
        entries.append({"tool": r.get("tool"), "label": r.get("label"),
                        "source": r.get("url") or r.get("path") or r.get("label"),
                        "ok": r.get("ok", False), "text": excerpt})
    if not entries:
        return ""
    return "SENSED_TOOL_RESULTS:\n" + __import__("json").dumps(entries, ensure_ascii=False)


async def record_evidence_memory(tenant_id: int, run_id: str, results: list[dict]) -> None:
    """Persist gathered observer evidence into its own shared-memory space.

    Runs on its own connection so a slow insert never holds the caller's
    transaction open. Tagged source=observer/kind=evidence_fetched so cross-run
    retrieval can tell reported material from any authoritative truth.
    Best-effort; failures log.
    """
    s = get_settings()
    space = s.observer_memory_space
    if not space or not results:
        return
    payload: dict[str, Any] = {
        "kind": "evidence_fetched", "source": "observer",
        "run_id": run_id, "results": [{
            "tool": r.get("tool"), "label": r.get("label"),
            "source": r.get("url") or r.get("path") or r.get("label"),
            "ok": r.get("ok", False),
            "status": r.get("status"),
            "error": r.get("error"),
            "excerpt": head_tail(r.get("text") or r.get("error") or "", 4000),
        } for r in results],
    }
    search = head_tail(__import__("json").dumps(payload, ensure_ascii=False), 64000)
    try:
        async with tenant_session(tenant_id) as mem_session:
            # Typed inserts (not raw text): the driver serializes the JSONB payload.
            await mem_session.execute(
                insert(MemorySpace).values(tenant_id=tenant_id, name=space)
                .on_conflict_do_nothing(index_elements=["tenant_id", "name"]))
            await mem_session.execute(
                insert(MemoryEntry).values(tenant_id=tenant_id, space=space,
                                           run_id=uuid.UUID(run_id), call_id=uuid.uuid4(),
                                           payload=payload, search_text=search)
                .on_conflict_do_nothing(index_elements=["tenant_id", "call_id"]))
            await mem_session.commit()
    except Exception as exc:  # the observation pipeline never blocks on memory
        LOG.warning("Observer evidence memory write failed run=%s error_type=%s",
                    run_id, type(exc).__name__)