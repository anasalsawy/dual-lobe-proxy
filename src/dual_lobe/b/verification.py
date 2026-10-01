"""Shared, full-length verifier prompt for both lobe directions.

The original review contract in :mod:`dual_lobe.b.prompts` is the single source
for the five checks, evidence rules, meter, and JSON structure. This adapter only
supplies the current candidate, conversation evidence, and the identity mapping
needed when B is the candidate and A verifies it.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from . import artifacts, fetch, prompts
from .protocol import Review, ground_review, parse_review
from ..core.settings import get_settings
from ..provider.adapters import NormalizedRequest, response_dict
from ..provider.registry import get_registry


def verifier_system(candidate_lobe: str) -> str:
    """Return the restored prompt with only candidate-lobe labels adapted."""
    candidate = "B" if str(candidate_lobe).upper() == "B" else "A"
    if candidate == "A":
        return prompts.B_SYSTEM
    return re.sub(r"\bA\b", "Lobe B", prompts.B_SYSTEM)


def build_prompt(*, candidate_lobe: str, context: str, output: str, events: str,
                 latest_request: str, prior_review: str = "", sensed: str = "") -> str:
    # Preserve the complete evidence supplied by the gateway. The old observer
    # builder has a bounded default for background jobs; inline verification
    # must not silently clip the active request, candidate, or tool results.
    evidence_size = sum(map(len, (context, output, events, latest_request,
                                  prior_review, sensed)))
    max_chars = len(prompts.CYCLE_PROMPT) + 200 + (evidence_size + 1) * 100
    result = prompts.build_cycle_prompt(
        context=context,
        response_text=output,
        events=events,
        prior_state=prior_review,
        max_chars=max_chars,
        latest_request=latest_request,
        sensed=sensed,
    )
    if str(candidate_lobe).upper() == "B":
        contract, marker, evidence = result.partition(prompts.EVIDENCE_MARKER)
        contract = re.sub(r"\bA\b", "Lobe B", contract)
        result = contract + marker + evidence
    return result


async def _call_review(target_alias: str, system: str, prompt: str) -> Review:
    """Run the fixed JSON verifier and retry once only for invalid/ungrounded JSON."""
    s = get_settings()
    adapter = get_registry().adapter(target_alias)
    correction = ""
    last_error: Exception | None = None
    for attempt in range(2):
        async with asyncio.timeout(s.b_timeout if target_alias.startswith("lobe-b") else s.a_timeout):
            response = await adapter.buffered(NormalizedRequest(
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": prompt + correction}],
                temperature=0,
                max_tokens=s.b_max_output_tokens,
                response_format={"type": "json_object"},
                timeout=s.b_timeout if target_alias.startswith("lobe-b") else s.a_timeout,
            ))
        data = response_dict(response)
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        try:
            review = parse_review(str(content), require_complete=True)
            return ground_review(review, prompt)
        except ValueError as exc:
            last_error = exc
            if attempt == 0:
                correction = (
                    "\n\nYour previous JSON did not satisfy the supplied full review contract: "
                    f"{type(exc).__name__}. Return the complete contract as valid JSON only."
                )
    raise ValueError("verifier returned invalid or ungrounded JSON") from last_error


async def _sense_once(review: Review, *, candidate_lobe: str, target_alias: str, system: str,
                      context: str, output: str, events: str, latest_request: str,
                      prior_review: str) -> tuple[Review, str]:
    """Honor bounded gateway evidence requests and re-review with the results."""
    s = get_settings()
    if not s.evidence_sensing_enabled:
        return review, ""
    results: list[dict[str, Any]] = []
    for _ in range(min(s.b_tool_max_rounds, s.b_evidence_ops_per_run)):
        request = review.evidence_request
        if not request:
            break
        if request.tool == "fetch_web":
            result = await fetch.fetch_url((request.arguments or {}).get("url", ""))
        elif request.tool == "read_artifact":
            result = await artifacts.read_artifact((request.arguments or {}).get("path", ""))
        else:
            break
        results.append(result)
        sensed = artifacts.sensed_text(results)
        revised = build_prompt(candidate_lobe=candidate_lobe, context=context,
                               output=output, events=events,
                               latest_request=latest_request, prior_review=prior_review,
                               sensed=sensed)
        review = await _call_review(target_alias, system, revised)
    return review, artifacts.sensed_text(results)


async def verify_output(*, candidate_lobe: str, target_alias: str,
                        context: str, output: str, events: str,
                        latest_request: str, prior_review: str = "") -> tuple[Review, str]:
    """Verify A or B with the same full prompt and JSON contract."""
    prompt = build_prompt(candidate_lobe=candidate_lobe, context=context,
                          output=output, events=events,
                          latest_request=latest_request, prior_review=prior_review)
    system = verifier_system(candidate_lobe)
    review = await _call_review(target_alias, system, prompt)
    review, sensed = await _sense_once(
        review, candidate_lobe=candidate_lobe, target_alias=target_alias,
        system=system, context=context,
        output=output, events=events, latest_request=latest_request,
        prior_review=prior_review,
    )
    return review, prompt + ("\nSENSED:\n" + sensed if sensed else "")


def as_meter_input(review: Review) -> dict[str, Any]:
    """Adapt the user's original four-field concern objects to the existing meter renderer."""
    data = review.model_dump()
    data["concerns"] = [
        {
            "claim_quote": item.claim_quote,
            "evidence_quote": item.basis_quote,
            "reason": item.reason,
            "correction": item.suggestion,
        }
        for item in review.concerns
    ]
    return data
