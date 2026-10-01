"""Provider registry: logical aliases -> ProviderTarget.

Aliases ``lobe-a``/``lobe-b`` are resolved from the environment contract
(``DUAL_LOBE_A_*`` / ``DUAL_LOBE_B_*``); additional entries come from the
``provider_registry`` table (admin-managed). Providers declare capabilities
(stream/tools/responses/…) so routing is data-driven.
"""
from __future__ import annotations

from typing import Any

from dataclasses import fields
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.models import ProviderRegistry
from . import hub
from .adapters import ProviderTarget, make_adapter


def env_targets() -> dict[str, ProviderTarget]:
    from ..core.settings import get_settings

    s = get_settings()
    base = ProviderTarget(
        alias="sawii/dual-lobe-old",
        base_url=s.a_base_url,
        api_key=s.a_api_key,
        model=s.a_model,
        kind=s.a_dialect,
        capabilities={"stream": True, "tools": True, "responses": False},
    )
    base_fields = {f.name: getattr(base, f.name) for f in fields(base)}
    base_fields.pop("alias", None)
    bidirectional = ProviderTarget(alias="sawii/dl-bidirectional", **base_fields)
    secure = ProviderTarget(alias="sawii/dl-secure", **base_fields)
    # Internal target retained for the director RPC; never listed as a model.
    dual_lobe = ProviderTarget(alias="sawii/dual-lobe", **base_fields)
    # Internal aliases for B-lobe shadow cycles and director mode.
    # Not exposed in /v1/models (filtered out by user_facing_models set).
    lobe_a = ProviderTarget(
        alias="lobe-a",
        base_url=s.a_base_url,
        api_key=s.a_api_key,
        model=s.a_model,
        kind=s.a_dialect,
        capabilities={"stream": True, "tools": True, "responses": False},
    )
    lobe_b = ProviderTarget(
        alias="lobe-b",
        base_url=s.resolved_b_base_url,
        api_key=s.resolved_b_api_key,
        model=s.resolved_b_model,
        kind=s.b_dialect,
        capabilities={"stream": False, "tools": True, "responses": False},
    )
    # Clinical B (model 2): patient data goes only to this configured endpoint,
    # never through the round-robin hub.
    lobe_b_clinical = ProviderTarget(
        alias="lobe-b-clinical",
        base_url=s.clinical_b_base_url or s.resolved_b_base_url,
        api_key=s.clinical_b_api_key if s.clinical_b_api_key is not None else s.resolved_b_api_key,
        model=s.clinical_b_model or s.resolved_b_model,
        kind=s.b_dialect,
        capabilities={"stream": False, "tools": True, "responses": False},
    )
    return {
        "lobe-b-clinical": lobe_b_clinical,
        "sawii/dual-lobe": dual_lobe,
        "sawii/dl-bidirectional": bidirectional,
        "sawii/dl-secure": secure,
        "lobe-a": lobe_a,
        "lobe-b": lobe_b,
    }


async def load_db_targets(session: AsyncSession) -> dict[str, ProviderTarget]:
    res = await session.execute(
        select(ProviderRegistry).where(ProviderRegistry.enabled.is_(True)).order_by(ProviderRegistry.alias)
    )
    out: dict[str, ProviderTarget] = {}
    for row in res.scalars().all():
        out[row.alias] = ProviderTarget(
            alias=row.alias,
            base_url=row.base_url,
            api_key="",
            model=row.model,
            kind=row.kind,
            capabilities=row.capabilities or {},
            enabled=row.enabled,
        )
    return out


# Env-configured aliases go through the round-robin hub when hub slots exist;
# lobe-b rotates over B's slots, every other env alias is lobe A.
_HUB_ROLES = {alias: "a" for alias in ("sawii/dual-lobe", "lobe-a")}
_HUB_ROLES["sawii/dl-bidirectional"] = "a"
_HUB_ROLES["sawii/dl-secure"] = "a"
_HUB_ROLES["lobe-b"] = "b"


class Registry:
    def __init__(self) -> None:
        self._targets: dict[str, ProviderTarget] = {}
        self._adapters: dict[str, Any] = {}

    def register(self, target: ProviderTarget) -> None:
        self._targets[target.alias] = target
        role = _HUB_ROLES.get(target.alias) if target.api_key else None
        slots = hub.configured_slots() if role else []
        if role and slots and target.kind == "chat_completions":
            self._adapters[target.alias] = hub.HubAdapter(target, role, slots)
        else:
            self._adapters[target.alias] = make_adapter(target)

    def refresh(self, targets: dict[str, ProviderTarget]) -> None:
        self._targets = {}
        self._adapters = {}
        for target in targets.values():
            self.register(target)

    def target(self, alias: str | None = None) -> ProviderTarget:
        return self._targets[alias or "sawii/dl-bidirectional"]

    def adapter(self, alias: str | None = None):
        target = self.target(alias)
        return self._adapters[target.alias]

    def models(self) -> list[dict[str, Any]]:
        # Only expose the two supported user-facing product variants.
        user_facing_models = {"sawii/dl-bidirectional", "sawii/dl-secure"}
        result = [
            {
                "id": t.alias,
                "object": "model",
                "owned_by": "local",
                "logical_model": t.model,
                "kind": t.kind,
                "capabilities": t.capabilities,
            }
            for t in self._targets.values()
            if t.enabled and t.alias in user_facing_models
        ]
        return result


_registry: Registry | None = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry()
        _registry.refresh(env_targets())
    return _registry


async def refresh_registry_from_db() -> None:
    from ..core.engine import admin_session_factory

    registry = get_registry()
    targets = env_targets()
    async with admin_session_factory()() as session:
        targets.update(await load_db_targets(session))
    registry.refresh(targets)
