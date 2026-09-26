# Dual-Lobe CrewAI

Dual-Lobe is a single current architecture for improving reasoning robustness in single-agent and multi-agent settings.

It is designed around several recurring failure modes:
- tunnel vision and missed context;
- unsupported or overconfident claims;
- memory loss across longer work;
- identity confusion in shared environments;
- reply storms and poor conversational coordination in groups.

## Current architecture

There is one supported Dual-Lobe runtime.

```text
task + memory -> A
                  ├─ A works directly when no useful split exists
                  └─ when useful:
                       A half ───────────────────────────────┐
                       B half via split_channel ─────────────┤ concurrent
                                                            ↓
                                           B reconvergence
                                 merge + repair + verification
                                                            ↓
                                           canonical answer
```

A receives the full task and acts as router + worker. If a substantial independent two-way split is likely to save time without hurting quality, A starts B's independent work through `split_channel` while continuing its own half. The runtime then sends the completed work to B for one final reconvergence step: merge, repair, verification, split grading, and one canonical answer.

If no useful split exists, A stays single-lane and B performs the final review.

There are no selectable Gated, Non-Split, or dedicated-Splitter product modes in the current release.

## Anti-deception verification

The B verification/finalization path checks:
- unsupported factual claims;
- fabricated or exaggerated action/tool/file claims;
- contradictions and silent task drift;
- unjustified certainty;
- missing requirements;
- whether claimed actions or artifacts are actually supported by evidence.

GREEN means **no deception detected from available evidence**, not “verified truth.”

Unresolved `missing`, `unverified`, or `proof_requests` cannot remain GREEN.

## Anti-tunnel-vision behavior

A is instructed to broaden the problem before committing:
- missing prerequisites;
- alternative explanations;
- mechanisms;
- tradeoffs;
- failure modes;
- overlooked questions;
- independent work that can be explored in parallel.

## Persistent memory

Dual-Lobe uses a durable JSONL memory store for task context and keeps split-experience memory logically separate. Relevant memory can be supplied to later turns without pretending the base model was retrained.

## Group awareness and address-aware orchestration

The runtime also supports group coordination through `GroupDualLobeRuntime`.

Core behavior:
- explicit platform target IDs, replies, @mentions, and clear direct-name addressing are routed deterministically;
- ambiguous role-based wording can use semantic fallback;
- only intended agents are invoked;
- non-addressed agents remain silent but can retain awareness for a later natural turn;
- one agent in a multi-human group does not answer merely because it is the only agent present;
- direct one-human / one-agent chat bypasses group-routing overhead;
- multiple addressed agents may run concurrently;
- each agent receives an authoritative SELF/OTHER identity envelope and separate default memory.

This is intended to reduce reply storms, addressing collapse, and identity confusion in shared conversations.

## Install

```bash
pip install -e .
```

## Run

```bash
dual-lobe --task "Diagnose this failure without guessing." --show-meta
```

Optional reconverging loop:

```bash
dual-lobe --task "Continue improving this result." --loop-cycles 3 --show-meta
```

## Group example

```bash
python examples/live_group_dual_lobe.py
```

## Release policy

The package contains only the current Dual-Lobe architecture. Historical variants remain recoverable from Git history but are not supported, selectable, exported, documented, or packaged as current models.
