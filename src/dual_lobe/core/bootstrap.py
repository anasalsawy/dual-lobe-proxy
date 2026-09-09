"""Seed a tenant + scoped API keys from ``DUAL_LOBE_BOOTSTRAP_KEYS``.

Format (``;``-separated): ``<raw-key>|<scope1,scope2>|<tenant-slug>``.
No predictable default key is created. Re-running bootstrap is idempotent.
"""
from __future__ import annotations

import asyncio
from sqlalchemy import select, func

from .engine import admin_session_factory, dispose_engines
from .models import ApiKey, Tenant
from .settings import get_settings
from ..api.auth import SCOPE_INFERENCE_INVOKE, SCOPE_EVENTS_WRITE, SCOPE_STATE_READ, SCOPE_RUNS_ADMIN, SCOPE_PROVIDERS_ADMIN, SCOPE_STATE_DEBUG, hash_key, new_key

ALL_SCOPES = [SCOPE_INFERENCE_INVOKE, SCOPE_EVENTS_WRITE, SCOPE_STATE_READ, SCOPE_STATE_DEBUG, SCOPE_RUNS_ADMIN, SCOPE_PROVIDERS_ADMIN]


async def ensure_tenant(session, slug: str, name: str) -> Tenant:
    res = await session.execute(select(Tenant).where(Tenant.slug == slug))
    tenant = res.scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(slug=slug, name=name)
        session.add(tenant)
        await session.flush()
    return tenant


async def seed() -> list[str]:
    s = get_settings()
    created: list[str] = []
    async with admin_session_factory()() as session:
        tenant = await ensure_tenant(session, s.seed_tenant_slug, s.seed_tenant_name)
        n_keys = (await session.execute(select(func.count()).select_from(ApiKey))).scalar_one()
        if n_keys == 0 and not s.bootstrap_keys:
            raise ValueError("Set DUAL_LOBE_BOOTSTRAP_KEYS to an explicit secret before first startup")
        for entry in (p for p in s.bootstrap_keys.split(";") if p.strip()):
            parts = [x.strip() for x in entry.split("|")]
            raw = parts[0]
            scopes = parts[1].split(",") if len(parts) > 1 and parts[1] else ALL_SCOPES
            slug = parts[2] if len(parts) > 2 and parts[2] else s.seed_tenant_slug
            t = await ensure_tenant(session, slug, slug)
            existing = (await session.execute(select(ApiKey).where(
                ApiKey.key_hash == hash_key(raw)
            ))).scalar_one_or_none()
            if existing:
                if existing.tenant_id != t.id:
                    raise ValueError("Bootstrap key already belongs to a different tenant")
                continue
            if len(raw) < 24 or raw.startswith("REPLACE_"):
                raise ValueError("Bootstrap API keys must contain at least 24 characters")
            session.add(ApiKey(tenant_id=t.id, key_hash=hash_key(raw), label="bootstrap", scopes=scopes))
            created.append(raw)
        await session.commit()
    return created


def main() -> None:
    async def _run() -> list[str]:
        created = await seed()
        await dispose_engines()
        return created

    created = asyncio.run(_run())
    print(f"Bootstrap complete: {len(created)} key(s) created; secrets are not logged.")


if __name__ == "__main__":
    main()
