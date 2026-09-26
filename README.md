# Dual-Lobe CrewAI

Dual-Lobe is a two-process reasoning runtime built around a persistent primary worker (**A**) and a persistent independent adversary/verifier (**B**).

The current architecture deliberately does **not** split the user's task into an A-half and B-half.

## Current architecture

```text
                         temporary child
                       ↗
User task → A ──delegate── temporary child
             │         ↘
             │           temporary child
             │
             │ execution events
             ↓
      B persistent live observer
        │       │
        │       └─ intervene during A's run when useful
        │
        └─ final adversarial repair + anti-deception verification
                          ↓
                   canonical answer
```

### A — worker + delegation

A owns execution and the user-facing task.

Delegation is defined as a **compute-acceleration primitive**, not managerial handoff. It means spawning temporary inference workers for independent work so useful tasks happen concurrently and the user waits less.

A is explicitly encouraged to delegate substantial independent work early. Children are temporary compute workers. They are not B.

Default child cap is controlled by `DUAL_LOBE_MAX_CHILDREN` (default 6).

### B — persistent independent adversary

B is not an extra worker lane and is not a polite reviewer.

Its standing job is to challenge A:
- find hidden assumptions and brittle logic;
- find reasons a plan/project may fail in practice;
- test whether the work actually satisfies the user's intent;
- detect when the user or A may be solving the wrong problem;
- find missing facts that could change the approach;
- challenge unnecessary/duplicative work and surface the need to check existing alternatives;
- detect ignored evidence, repeated failures, task drift, and unjustified certainty;
- audit A's use of delegation;
- perform final anti-deception verification.

## Continuous B

B is now **event-driven and continuously present alongside A**, rather than appearing only at the final gate.

While A is executing, B:
1. forms its own independent task view;
2. watches meaningful runtime/tool/delegation events;
3. updates its own persistent state;
4. emits a live intervention when A can still benefit from changing course;
5. streams that intervention back into A's tool flow;
6. still performs a full final adversarial review and verification.

B does not burn inference in a blind busy-loop. It wakes on meaningful events. `DUAL_LOBE_B_LIVE_MAX_CALLS` caps live observations per run (default 6).

## Anti-deception — primary feature

The verifier uses a strict evidence protocol:

- verify against the original user intent;
- mentally classify material claims as **OBSERVED / INFERRED / ASSUMED / UNKNOWN**;
- detect unsupported factual claims and fabricated/exaggerated action claims;
- require positive evidence for files, edits, deployments, tests, external actions, bookings, payments, messages, and artifacts;
- treat worker text as contributed work, not independent corroboration;
- actively seek disconfirming evidence instead of only support;
- bind the verdict to the exact final answer;
- fail closed: malformed or incomplete verification cannot become GREEN.

**GREEN means only “no deception detected from available evidence.”** It does not mean universal truth.

Unresolved `missing`, `unverified`, or `proof_requests` force at least YELLOW.

## Group chat: B owns conversational routing

When `is_group=true`, B has an additional persistent job: **follow the conversation and decide who is actually addressed by each message.**

```text
group conversation
      ↓
B observes roster + transport metadata + recent conversation + new message
      ↓
"who is actually addressed?"
      ↓
deterministic runtime validates B's agent IDs
      ↓
only addressed A instance(s) run
```

B distinguishes address from mention, follows conversational continuity, understands role-based addressing, and can return no target. Deterministic code does not pretend to understand the conversation; it only validates IDs and enforces B's floor decision.

If B routing fails, the runtime fails closed against reply storms, except for authoritative transport metadata such as explicit target IDs, reply-to metadata, or an explicit broadcast.

Direct one-agent/private chat bypasses this routing inference.

## Independent memory

A and B use separate persistent JSONL memory lineages by default. B's live observations and adversarial findings therefore accumulate independently from A's task memory.

## Install

```bash
pip install -e .
```

## Run

```bash
dual-lobe --task "Diagnose this failure without guessing." --show-meta
```

Optional repeated cycles:

```bash
dual-lobe --task "Continue improving this result." --loop-cycles 3 --show-meta
```

## Core call shape

Without delegation, one task cycle consists of:
- A primary call;
- zero or more live B observation calls while A runs;
- B final adversarial/verification call.

Delegated child calls are additional and intentional parallel compute. The exact logical-call count is surfaced in metadata.
