from __future__ import annotations

from crewai import Agent

from .llm_factory import make_llm
from .prompts import A_PERSONA, B_VERIFY_PERSONA, B_WORKER_PERSONA, SPLITTER_PERSONA


def make_a(tools=None, *, merge: bool = False) -> Agent:
    return Agent(
        role="Lobe A — Primary Worker",
        goal="Solve the user's task and own the final user-facing answer.",
        backstory=A_PERSONA,
        llm=make_llm("A_MERGE" if merge else "A"),
        tools=list(tools or []),
        verbose=False,
        allow_delegation=False,
    )


def make_b_verifier() -> Agent:
    return Agent(
        role="Lobe B — Verification Peer",
        goal="Verify A's proposed answer without doing a rewrite.",
        backstory=B_VERIFY_PERSONA,
        llm=make_llm("B_VERIFY"),
        tools=[],
        verbose=False,
        allow_delegation=False,
    )


def make_b_worker() -> Agent:
    return Agent(
        role="Lobe B — Worker Peer",
        goal="Execute one bounded task fragment independently.",
        backstory=B_WORKER_PERSONA,
        llm=make_llm("B_WORKER"),
        tools=[],
        verbose=False,
        allow_delegation=False,
    )


def make_splitter() -> Agent:
    return Agent(
        role="Dedicated Splitter / Split-Mode Verifier",
        goal="Route only; never do task work. Verify merged output only in split mode.",
        backstory=SPLITTER_PERSONA,
        llm=make_llm("SPLITTER"),
        tools=[],
        verbose=False,
        allow_delegation=False,
    )
