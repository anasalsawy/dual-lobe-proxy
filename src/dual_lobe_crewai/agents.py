from __future__ import annotations

from crewai import Agent

from .llm_factory import make_llm
from .prompts import (
    A_PERSONA,
    A_SELF_SPLIT_PERSONA,
    B_VERIFY_PERSONA,
    B_FINALIZE_PERSONA,
    B_WORKER_PERSONA,
)


def make_a(tools=None, *, merge: bool = False, self_split: bool = False) -> Agent:
    persona = A_SELF_SPLIT_PERSONA if self_split and not merge else A_PERSONA
    role = "Lobe A — Professional Splitter-Executor" if self_split and not merge else "Lobe A — Primary Worker"
    goal = (
        "Minimize wall-clock completion time by finding a valid two-way split when one exists, "
        "then execute your own independent half."
        if self_split and not merge
        else "Solve the user's task and own the final user-facing answer."
    )
    return Agent(
        role=role,
        goal=goal,
        backstory=persona,
        llm=make_llm("A_MERGE" if merge else "A"),
        tools=list(tools or []),
        verbose=False,
        allow_delegation=False,
    )


def make_b_verifier() -> Agent:
    return Agent(
        role="Lobe B — Verification and Split-Quality Peer",
        goal="Verify the final answer and grade the decomposition decision using evidence and timing telemetry.",
        backstory=B_VERIFY_PERSONA,
        llm=make_llm("B_VERIFY"),
        tools=[],
        verbose=False,
        allow_delegation=False,
    )


def make_b_finalizer(tools=None) -> Agent:
    return Agent(
        role="Lobe B — Collector, Finalizer, Verifier, and Split-Quality Peer",
        goal=(
            "Merge both completed halves into one canonical answer, repair deficiencies, verify the result, "
            "and grade the split using evidence and timing telemetry."
        ),
        backstory=B_FINALIZE_PERSONA,
        llm=make_llm("B_VERIFY"),
        tools=list(tools or []),
        verbose=False,
        allow_delegation=False,
    )


def make_b_worker(tools=None) -> Agent:
    return Agent(
        role="Lobe B — Independent Parallel Worker",
        goal="Execute one independent task half without depending on Lobe A's intermediate output.",
        backstory=B_WORKER_PERSONA,
        llm=make_llm("B_WORKER"),
        tools=list(tools or []),
        verbose=False,
        allow_delegation=False,
    )
