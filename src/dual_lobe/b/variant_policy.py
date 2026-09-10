"""Explicit design policies used for head-to-head comparisons.

The policy name is configuration, never inferred from model output.  The
default keeps the existing non-blocking observer behavior; strict-gatekeeper is
the deliberately fail-closed comparison where B must approve a response
before it is released.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DesignVariantName = Literal[
    "peer-observer", "strict-gatekeeper", "parallel-debate",
    "strategist-executor", "director",
]


@dataclass(frozen=True)
class VariantPolicy:
    name: DesignVariantName
    description: str
    synchronous_gate: bool = False
    visible_director: bool = False
    exclusive_roles: bool = False


POLICIES: dict[str, VariantPolicy] = {
    "peer-observer": VariantPolicy(
        "peer-observer",
        "A answers; B independently reviews the same canonical snapshot and is fail-open.",
    ),
    "strict-gatekeeper": VariantPolicy(
        "strict-gatekeeper",
        "B must explicitly establish proof coverage before A's response is released.",
        synchronous_gate=True,
    ),
    "parallel-debate": VariantPolicy(
        "parallel-debate",
        "B uses an independent-debate prompt over the same canonical snapshot; review remains background and fail-open.",
    ),
    "strategist-executor": VariantPolicy(
        "strategist-executor",
        "B uses a strategist/verifier prompt while A remains the action lane; this profile does not add a hard execution lock.",
        exclusive_roles=True,
    ),
    "director": VariantPolicy(
        "director",
        "B is visible and takes the user's conversational place while directing A.",
        visible_director=True,
    ),
}


def policy(name: str | None) -> VariantPolicy:
    return POLICIES.get(name or "peer-observer", POLICIES["peer-observer"])


def gate_allows(review) -> bool:
    """Strict mode's only release condition; missing fields fail closed."""
    return (getattr(review, "gate_decision", None) == "ALLOW"
            and getattr(review, "proof_coverage", "unknown") == "complete")
