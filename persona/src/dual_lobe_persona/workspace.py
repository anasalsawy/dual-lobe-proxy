"""Server-side workspace layout for personas.

Every persona gets an isolated directory under the sandbox root, keyed by
a hash of the persona API key. The raw key never touches disk or logs.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def workspace_root() -> Path:
    """Server-side root for per-persona workspaces (never user filesystems)."""
    root = Path(os.environ.get("DUAL_LOBE_PERSONA_SANDBOX_ROOT", "/var/lib/dual-lobe-persona/workspaces"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def persona_slug(persona_key: str) -> str:
    """Stable non-secret identifier for a persona key (for logs and paths)."""
    return hashlib.sha256(persona_key.encode("utf-8")).hexdigest()[:16]


def persona_workspace_dir(persona_key: str) -> Path:
    """One isolated workspace directory per persona, keyed by hashed API key."""
    path = workspace_root() / f"persona-{persona_slug(persona_key)}"
    path.mkdir(parents=True, exist_ok=True)
    return path