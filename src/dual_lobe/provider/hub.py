"""Round-robin provider hub.

One global rotation over every configured provider slot (1, 2, 3, 4, 1, ...),
shared by lobe A and lobe B. Each call starts on the next slot in the cycle; a
slot that errors (429, 5xx, timeout, auth, or a local rate budget that would
make the caller wait) is skipped for the same call and the next slot is tried
immediately. Every slot keeps its own host, key and model, and its own pacing
gate (``ratelimit.gate_for`` keys by host + key).

Slots come from ``DUAL_LOBE_HUB_SLOTS`` (JSON list), in addition to the lobe's
own ``DUAL_LOBE_A_*`` / ``DUAL_LOBE_B_*`` primary::

    [{"label": "openrouter-1", "model": "nvidia/nemotron-3-super-120b-a12b:free",
      "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY_1",
      "roles": ["a", "b"]}]

``roles`` defaults to both lobes. Keys are read from the named env variable
(``api_key_env``) so the JSON never carries secrets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator

import httpx

from . import ratelimit

LOG = logging.getLogger("dual_lobe.hub")

# Cool-down applied when a slot fails, by failure class (seconds).
COOL_SERVER = 20.0
COOL_TRANSPORT = 20.0
COOL_AUTH = 900.0

_LOCK = threading.Lock()
_POOL: list[tuple] = []          # global slot order (identities), shared by both lobes
_NEXT = 0
_HEALTH: dict[tuple, dict[str, Any]] = {}


def _identity(target: Any) -> tuple:
    return (target.model, target.base_url.rstrip("/"), target.api_key)


def configured_slots() -> list[dict[str, Any]]:
    raw = (os.getenv("DUAL_LOBE_HUB_SLOTS") or "").strip()
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except Exception:
        LOG.warning("DUAL_LOBE_HUB_SLOTS is not valid JSON; hub disabled")
        return []
    out = []
    for i, row in enumerate(rows if isinstance(rows, list) else [], 1):
        if not isinstance(row, dict) or not row.get("model") or not row.get("base_url"):
            continue
        key = row.get("api_key") or (os.getenv(str(row["api_key_env"])) if row.get("api_key_env") else "")
        if not key:
            LOG.warning("hub slot %s has no API key; skipped", row.get("label") or i)
            continue
        roles = row.get("roles") or ["a", "b"]
        out.append({"label": str(row.get("label") or f"slot-{i}"), "model": str(row["model"]),
                    "base_url": str(row["base_url"]), "api_key": key,
                    "roles": {str(r).lower() for r in roles}})
    return out


def _register(identity: tuple, label: str) -> None:
    with _LOCK:
        if identity not in _POOL:
            _POOL.append(identity)
            _HEALTH[identity] = {"label": label, "cool_until": 0.0, "reason": "", "ok": 0, "failed": 0}


def _rotation(own: list[tuple]) -> list[tuple]:
    """Global cycle: advance once per call; this lobe starts at the next slot it has."""
    global _NEXT
    with _LOCK:
        start = _NEXT % len(_POOL)
        _NEXT += 1
        ordered = _POOL[start:] + _POOL[:start]
    mine = set(own)
    return [ident for ident in ordered if ident in mine]


def _cooling(identity: tuple) -> float:
    return max(0.0, _HEALTH[identity]["cool_until"] - time.time())


def _mark(identity: tuple, ok: bool, cool: float = 0.0, reason: str = "") -> None:
    health = _HEALTH[identity]
    if ok:
        health["ok"] += 1
        if health["cool_until"]:
            LOG.info("hub slot %s healthy again", health["label"])
        health["cool_until"], health["reason"] = 0.0, ""
        return
    health["failed"] += 1
    until = time.time() + cool
    if until > health["cool_until"]:
        if not health["cool_until"] or health["cool_until"] < time.time():
            LOG.warning("hub slot %s cooling %.0fs: %s", health["label"], cool, reason)
        health["cool_until"] = until
    health["reason"] = reason


_DAILY = re.compile(r"per[-_ ]?day|daily|PerDay|free-models-per-day", re.I)
_RESET = re.compile(r'X-RateLimit-Reset"?\s*:\s*"?(\d{10,13})', re.I)


def _body(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception:  # noqa: BLE001  (streamed body not read)
        return ""


def _daily_cool(response: httpx.Response) -> float | None:
    """Seconds until a daily quota resets, if this 429 is a daily-quota 429."""
    body = _body(response)
    if not _DAILY.search(body):
        return None
    match = _RESET.search(body) or _RESET.search(json.dumps(dict(response.headers)))
    if match:
        stamp = int(match.group(1))
        stamp = stamp / 1000 if stamp > 1e12 else stamp
        if stamp > time.time():
            return stamp - time.time()
    return 86400 - (time.time() % 86400)  # next UTC midnight


def _strategy() -> str:
    """DUAL_LOBE_HUB_STRATEGY: round_robin (1,2,3,4,1...) or fastest (fastest healthy slot first)."""
    return (os.getenv("DUAL_LOBE_HUB_STRATEGY") or "round_robin").strip().lower()


def _record_speed(ident: tuple, seconds: float, data: Any) -> None:
    """Moving average of seconds per call, normalised by answer length."""
    try:
        out_tokens = int(((data or {}).get("usage") or {}).get("completion_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        out_tokens = 0
    sample = seconds / (1.0 + out_tokens / 1000.0)
    prev = _HEALTH[ident].get("speed")
    _HEALTH[ident]["speed"] = sample if prev is None else 0.7 * prev + 0.3 * sample


def _classify(exc: BaseException) -> tuple[float, str]:
    """Return (cool-down seconds, reason). 0 cool-down = request-level, slot stays healthy."""
    if isinstance(exc, ratelimit.UpstreamRateLimited):
        return exc.retry_after, str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            daily = _daily_cool(exc.response)
            if daily is not None:
                return daily, "429 daily quota used up"
            return 0.0, "429"  # the slot's gate already parked itself for Retry-After
        if code in (401, 403, 404):
            return COOL_AUTH, f"http {code}"
        if code >= 500 or code == 408:
            return COOL_SERVER, f"http {code}"
        return 0.0, f"http {code}"
    if isinstance(exc, (httpx.TransportError, asyncio.TimeoutError, TimeoutError)):
        return COOL_TRANSPORT, type(exc).__name__
    return 0.0, f"{type(exc).__name__}: {exc}"


class HubAdapter:
    """Drop-in for ``ChatCompletionsAdapter`` that rotates across provider slots."""

    dialect = "chat_completions"

    def __init__(self, target: Any, role: str, slots: list[dict[str, Any]]) -> None:
        from .adapters import ChatCompletionsAdapter

        self.target = target
        self.role = role
        self._adapters: dict[tuple, Any] = {}
        candidates = [(target, "primary")]
        for slot in slots:
            if role in slot["roles"]:
                candidates.append((replace(target, model=slot["model"], base_url=slot["base_url"],
                                           api_key=slot["api_key"]), slot["label"]))
        for cand, label in candidates:
            ident = _identity(cand)
            if ident in self._adapters:
                continue
            _register(ident, label if label != "primary" else f"{role}-primary:{cand.model}")
            self._adapters[ident] = ChatCompletionsAdapter(cand)
        self._own = list(self._adapters)

    def _order(self, req: Any) -> list[tuple]:
        order = _rotation(self._own)
        tokens = ratelimit.estimate_request_tokens(req)
        # Healthy slots that can serve right now keep their rotation order; slots
        # that are cooling or would make the caller wait on pacing go last,
        # soonest-available first, so the call never parks while another slot is free.
        ready, later = [], []
        for ident in order:
            wait = max(_cooling(ident), ratelimit.gate_for(self._adapters[ident].target).estimate_wait(tokens))
            (ready if wait <= 0 else later).append((wait, ident))
        later.sort(key=lambda item: item[0])
        if _strategy() == "fastest":
            # Fastest healthy slot first (measured speed, rotation order breaks ties;
            # unmeasured slots count as fast so each gets tried).
            ready.sort(key=lambda item: _HEALTH[item[1]].get("speed") or 0.0)
        return [ident for _, ident in ready] + [ident for _, ident in later]

    async def buffered(self, req: Any):
        last: BaseException | None = None
        for ident in self._order(req):
            adapter = self._adapters[ident]
            started = time.monotonic()
            try:
                data = await adapter.buffered(req)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                cool, reason = _classify(exc)
                _mark(ident, False, cool, reason)
                LOG.info("hub %s: slot %s failed (%s); trying next", self.role, _HEALTH[ident]["label"], reason)
                last = exc
                continue
            _mark(ident, True)
            _record_speed(ident, time.monotonic() - started, data)
            return data
        assert last is not None
        raise last

    async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
        last: BaseException | None = None
        for ident in self._order(req):
            adapter = self._adapters[ident]
            started = False
            try:
                async for chunk in adapter.stream(req):
                    started = True
                    yield chunk
            except asyncio.CancelledError:
                raise
            except GeneratorExit:
                raise
            except Exception as exc:  # noqa: BLE001
                if started:
                    # Output already reached the client; switching providers mid-answer
                    # would splice two different answers together.
                    raise
                cool, reason = _classify(exc)
                _mark(ident, False, cool, reason)
                LOG.info("hub %s: slot %s failed before first chunk (%s); trying next",
                         self.role, _HEALTH[ident]["label"], reason)
                last = exc
                continue
            _mark(ident, True)
            return
        assert last is not None
        raise last


def snapshot() -> list[dict[str, Any]]:
    now = time.time()
    with _LOCK:
        pool = list(_POOL)
        nxt = _NEXT % len(pool) if pool else 0
    return [{"slot": i + 1, "label": _HEALTH[ident]["label"], "model": ident[0], "base_url": ident[1],
             "next": i == nxt, "cooling_seconds": round(max(0.0, _HEALTH[ident]["cool_until"] - now), 1),
             "last_error": _HEALTH[ident]["reason"], "ok": _HEALTH[ident]["ok"],
             "seconds_per_call": round(_HEALTH[ident].get("speed") or 0.0, 2),
             "failed": _HEALTH[ident]["failed"]} for i, ident in enumerate(pool)]


def reset_for_tests() -> None:
    global _NEXT
    with _LOCK:
        _POOL.clear()
        _HEALTH.clear()
        _NEXT = 0
