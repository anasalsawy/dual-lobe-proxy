"""Gateway-side bounded fetch executor. No model runs here.

Executes URL fetches on behalf of the observer, never on the request critical
path. Content is treated as reported material, not verified truth. Two
mechanical guards only: https/http schemes and per-hop redirect re-validation,
so a fetch never smuggles a non-HTTP protocol or an internal hop. Everything
else (any host, any address) is intentionally unrestricted per product policy.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse, urljoin

import httpx

from ..core.settings import get_settings

LOG = logging.getLogger("dual_lobe.b.fetch")

_ALLOWED_SCHEMES = ("http", "https")
_MAX_REDIRECTS = 4


async def fetch_url(url: str, *, timeout: float | None = None,
                    max_bytes: int | None = None) -> dict:
    s = get_settings()
    timeout = timeout or s.fetch_timeout
    max_bytes = max_bytes or s.fetch_max_bytes

    def bad(reason: str) -> dict:
        LOG.warning("Observer fetch rejected url=%s reason=%s", url, reason)
        return {"ok": False, "tool": "fetch_web", "label": url,
                "error": f"fetch rejected: {reason}"}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        current = url
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                if urlparse(current).scheme not in _ALLOWED_SCHEMES:
                    return bad("unsupported scheme")
                try:
                    response = await client.get(current)
                except Exception as exc:  # transport/connect/timeout
                    return {"ok": False, "tool": "fetch_web", "label": url,
                            "error": f"fetch failed: {type(exc).__name__}"}
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return {"ok": False, "tool": "fetch_web", "label": url,
                                "error": "fetch failed: redirect without location"}
                    current = urljoin(current, location)
                    continue
                if response.status_code == 200:
                    body = response.text
                    truncated = len(body) > max_bytes
                    if truncated:
                        body = body[:max_bytes] + "\n[truncated]"
                    return {"ok": True, "tool": "fetch_web", "label": current,
                            "status": response.status_code, "text": body,
                            "truncated": truncated, "url": current}
                return {"ok": False, "tool": "fetch_web", "label": current,
                        "error": f"http {response.status_code}", "status": response.status_code}
        except Exception as exc:  # header/length horrors
            return {"ok": False, "tool": "fetch_web", "label": url,
                    "error": f"fetch failed: {type(exc).__name__}"}
    return bad("too many redirects")