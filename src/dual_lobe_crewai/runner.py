from __future__ import annotations

import asyncio
import os
import threading

from crewai import Crew, Process, Task

from .llm_factory import make_llm, resolve_role_specs
from .provider_control import RATE_CONTROLLER


_RR_LOCK = threading.Lock()
_RR_POOL: list = []
_RR_NEXT = 0


def _rr_reset() -> None:
    global _RR_NEXT
    with _RR_LOCK:
        _RR_POOL.clear()
        _RR_NEXT = 0


def _spec_id(spec):
    # Same model at the same host under different API keys counts as a separate slot.
    if hasattr(spec, "model"):
        return (spec.model, spec.base_url, spec.api_key)
    return spec.label


def _round_robin_specs(role_key: str, specs):
    """Rotate one shared provider cycle across all calls (1,2,3,1,2,3...), regardless of role.

    A role that lacks the next provider in the cycle starts on the next one it has.
    """
    global _RR_NEXT
    specs = list(specs)
    if len(specs) <= 1:
        return specs
    ids = [_spec_id(s) for s in specs]
    with _RR_LOCK:
        for sid in ids:
            if sid not in _RR_POOL:
                _RR_POOL.append(sid)
        n = len(_RR_POOL)
        start = 0
        for step in range(n):
            target = _RR_POOL[(_RR_NEXT + step) % n]
            if target in ids:
                start = ids.index(target)
                _RR_NEXT = (_RR_NEXT + step + 1) % n
                break
    return specs[start:] + specs[:start]


def _infer_role_key(agent) -> str:
    role = str(getattr(agent, "role", "")).lower()
    if "adversary" in role or "verification" in role or "verifier" in role or "lobe b" in role:
        return "B_VERIFY"
    if "delegated inference worker" in role or "temporary delegated" in role:
        return "A_CHILD"
    return "A"


def _set_llm(agent, llm):
    try:
        agent.llm = llm
    except Exception:
        object.__setattr__(agent, "llm", llm)


async def _single_call(agent, description: str, expected_output: str) -> str:
    task = Task(description=description, expected_output=expected_output, agent=agent)
    crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
    result = await crew.kickoff_async()
    raw = getattr(result, "raw", None)
    return str(raw if raw is not None else result)


async def run_one(agent, description: str, expected_output: str, *, role_key: str | None = None) -> str:
    role_key = (role_key or _infer_role_key(agent)).upper()
    specs = _round_robin_specs(role_key, resolve_role_specs(role_key))
    if not specs:
        raise RuntimeError("No LLM providers configured")

    # Each call starts on the next provider in the shared cycle. If that provider
    # errors (including while being set up), try the next one in the same order.
    last_exc = None
    for original_spec in specs:
        spec = original_spec
        try:
            input_est = RATE_CONTROLLER.estimate_input_tokens(description + "\n" + expected_output)
            spec = RATE_CONTROLLER.fit_output_budget(original_spec, input_est)
            estimated_total = input_est + spec.max_tokens
            await RATE_CONTROLLER.acquire(spec, estimated_total)
            _set_llm(agent, make_llm(role_key, spec=spec))
            out = await _single_call(agent, description, expected_output)
            if out is None or not str(out).strip():
                raise ValueError("Invalid response from LLM call - None or empty.")
            return str(out)
        except Exception as exc:
            RATE_CONTROLLER.learn_from_error(spec, exc)
            last_exc = exc
            continue

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("No LLM providers available")
