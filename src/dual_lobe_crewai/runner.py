from __future__ import annotations

import asyncio
import os

from crewai import Crew, Process, Task

from .llm_factory import make_llm, resolve_role_specs
from .provider_control import RATE_CONTROLLER, ProviderSpec


def _infer_role_key(agent) -> str:
    role = str(getattr(agent, "role", "")).lower()
    if "splitter" in role:
        return "SPLITTER"
    if "verification" in role or "verifier" in role:
        return "B_VERIFY"
    if "worker peer" in role or "lobe b" in role:
        return "B_WORKER"
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
    """Resilient CrewAI call with adaptive pacing, failover and backoff.

    Primary behavior is unchanged when the provider is healthy. Pacing activates only
    when limits are configured/learned. Paid/unknown tiers with no learned limit run
    unthrottled. On a rate-limit/transient failure, configured/cross-role fallbacks are
    tried before sleeping on the blocked model.
    """
    role_key = (role_key or _infer_role_key(agent)).upper()
    specs = resolve_role_specs(role_key)
    max_rounds = max(1, int(os.getenv("DUAL_LOBE_RETRY_ROUNDS", "3")))
    failover = os.getenv("DUAL_LOBE_FAILOVER_ON_RATE_LIMIT", "true").lower() in {"1", "true", "yes", "on"}
    last_exc = None

    for round_no in range(1, max_rounds + 1):
        candidates = specs if (round_no == 1 or failover) else specs[:1]
        for idx, original_spec in enumerate(candidates):
            input_est = RATE_CONTROLLER.estimate_input_tokens(description + "\n" + expected_output)
            spec = RATE_CONTROLLER.fit_output_budget(original_spec, input_est)
            estimated_total = input_est + spec.max_tokens
            await RATE_CONTROLLER.acquire(spec, estimated_total)
            _set_llm(agent, make_llm(role_key, spec=spec))
            try:
                out = await _single_call(agent, description, expected_output)
                if out is None or not str(out).strip():
                    raise ValueError("Invalid response from LLM call - None or empty.")
                return str(out)
            except Exception as exc:
                last_exc = exc
                kind = RATE_CONTROLLER.classify_error(exc)
                RATE_CONTROLLER.learn_from_error(spec, exc)
                # Auth failures can still fail over to another configured key/provider.
                if idx + 1 < len(candidates) and kind in {"rate_limit", "transient", "auth"}:
                    continue
                if kind not in {"rate_limit", "transient"}:
                    break

        if round_no < max_rounds and last_exc is not None:
            # If all live alternatives failed, wait only as long as the blocked provider says is needed.
            await asyncio.sleep(RATE_CONTROLLER.retry_delay(specs[0], round_no))

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("No LLM candidates available")
