from __future__ import annotations

from crewai import Agent

from .llm_factory import make_llm
from .prompts import A_PERSONA, B_ADVERSARY_PERSONA, CHILD_PERSONA


def make_a(tools=None) -> Agent:
    return Agent(
        role="Lobe A — Primary Worker and Delegator",
        goal="Solve the user's task with minimum wall-clock delay, delegating independent work whenever that can save the user time.",
        backstory=A_PERSONA,
        llm=make_llm("A"),
        tools=list(tools or []),
        verbose=False,
        allow_delegation=False,
    )


def make_child_worker(tools=None) -> Agent:
    return Agent(
        role="Temporary Delegated Inference Worker",
        goal="Execute the assigned independent subtask quickly and return a self-contained result to Lobe A.",
        backstory=CHILD_PERSONA,
        llm=make_llm("A_CHILD"),
        tools=list(tools or []),
        verbose=False,
        allow_delegation=False,
    )


def make_b_adversary(tools=None) -> Agent:
    return Agent(
        role="Lobe B — Independent Adversary and Anti-Deception Verifier",
        goal=(
            "Attack A's reasoning, goal-fit, assumptions, feasibility, and evidence; find what A or the user may be missing; "
            "then repair the answer where possible and verify the exact canonical result."
        ),
        backstory=B_ADVERSARY_PERSONA,
        llm=make_llm("B_VERIFY"),
        tools=list(tools or []),
        verbose=False,
        allow_delegation=False,
    )
