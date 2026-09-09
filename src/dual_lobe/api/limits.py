"""Process-local admission budgets; no Redis service or network dependency."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from ..core.settings import get_settings

_local: dict[str, list[tuple[float, int]]] = {}
_local_lock = asyncio.Lock()


async def _local_sliding(key: str, window: float, limit: int, weight: int = 1) -> tuple[bool, float]:
    async with _local_lock:
        now = time.monotonic()
        hits = [(t, w) for t, w in _local.get(key, []) if now - t < window]
        used = sum(w for _, w in hits)
        if limit > 0 and used + weight > limit:
            _local[key] = hits
            retry = window - (now - hits[0][0]) if hits else window
            return False, max(0.0, retry)
        hits.append((now, weight))
        _local[key] = hits
        # Bound inactive tenant accumulation; active keys remain rate limited.
        if len(_local) > 1000:
            for name in list(_local):
                if _local[name] and now - _local[name][-1][0] > 60:
                    del _local[name]
        return True, 0.0


@dataclass
class RateResult:
    allowed: bool = True
    retry_after: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


async def check_limits(tenant_id: int, token_estimate: int = 0) -> RateResult:
    s = get_settings()
    rpm_ok, rpm_wait = await _local_sliding(f"dl:rpm:{tenant_id}", 60, s.rpm_limit)
    tpm_ok, tpm_wait = True, 0.0
    if token_estimate and s.tpm_limit > 0:
        tpm_ok, tpm_wait = await _local_sliding(
            f"dl:tpm:{tenant_id}", 60, s.tpm_limit, weight=max(1, token_estimate)
        )
    return RateResult(allowed=rpm_ok and tpm_ok, retry_after=max(rpm_wait, tpm_wait))
