"""Per-request log of every upstream model call, for latency reporting.

``begin()`` at the start of a request; every leaf HTTP call records one entry
(stage, lobe alias, provider host, model, start offset, duration, rate-limit
wait, time to first token for streams, tokens, status/error). Engines label
their steps with ``stage("A-plan")`` etc. Tool executions can be logged with
``record_tool``. ``snapshot()`` returns the entries for the response.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from typing import Any
from urllib.parse import urlparse

_CALLS: contextvars.ContextVar[list | None] = contextvars.ContextVar("dl_calls", default=None)
_T0: contextvars.ContextVar[float] = contextvars.ContextVar("dl_t0", default=0.0)
_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar("dl_stage", default="")
_ALIAS: contextvars.ContextVar[str] = contextvars.ContextVar("dl_alias", default="")


def begin() -> None:
    _CALLS.set([])
    _T0.set(time.perf_counter())


def now_ms() -> int:
    return int((time.perf_counter() - _T0.get()) * 1000) if _T0.get() else 0


@contextlib.contextmanager
def stage(name: str):
    token = _STAGE.set(name)
    try:
        yield
    finally:
        _STAGE.reset(token)


@contextlib.contextmanager
def alias(name: str):
    token = _ALIAS.set(name)
    try:
        yield
    finally:
        _ALIAS.reset(token)


def record_call(*, target: Any, started: float, gate_wait: float, status: int | None, usage: dict | None,
                error: str = "", first_token: float | None = None, stream: bool = False) -> None:
    calls = _CALLS.get()
    if calls is None:
        return
    t0 = _T0.get()
    end = time.perf_counter()
    usage = usage or {}
    calls.append({
        "stage": _STAGE.get() or "-",
        "lobe": _ALIAS.get() or getattr(target, "alias", ""),
        "provider": (urlparse(getattr(target, "base_url", "") or "").hostname or ""),
        "model": getattr(target, "model", ""),
        "start_ms": int((started - t0) * 1000),
        "ms": int((end - started) * 1000),
        "rate_wait_ms": int(gate_wait * 1000),
        "first_token_ms": int((first_token - started) * 1000) if first_token else None,
        "stream": stream,
        "status": status,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "error": error[:200],
    })


def record_tool(name: str, started: float, *, where: str = "server") -> None:
    calls = _CALLS.get()
    if calls is None:
        return
    t0 = _T0.get()
    calls.append({"stage": _STAGE.get() or "-", "lobe": _ALIAS.get() or "", "tool": name, "where": where,
                  "start_ms": int((started - t0) * 1000), "ms": int((time.perf_counter() - started) * 1000)})


def snapshot() -> list[dict[str, Any]]:
    return list(_CALLS.get() or [])
