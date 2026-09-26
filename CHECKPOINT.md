# Current checkpoint — Dual-Lobe

This checkpoint has one supported model architecture.

Current production concept:
- A receives the full task and acts as router + worker.
- If a useful independent two-way split exists, A starts B's peer half through `split_channel` and executes its own half concurrently.
- If no useful split exists, A completes the task single-lane.
- The runtime reconverges at B.
- B merges when needed, repairs deficiencies, verifies the exact candidate answer, grades the split decision, and emits one canonical final answer.
- Persistent task memory and split-experience memory are retained.
- Group awareness, SELF/OTHER identity separation, deterministic addressing, semantic fallback for ambiguous addressing, floor control, and output gating are included.

Legacy Gated, Non-Split, and dedicated-Splitter variants are not part of the current supported runtime. They are available only through Git history.

Verifier hardening:
- one shared full VERIFICATION_PROTOCOL is used by the current B verification/finalization paths;
- material action/artifact claims require evidence;
- unresolved missing/unverified/proof_requests cannot remain GREEN;
- malformed verification fails closed to YELLOW;
- full tool trace evidence is retained by default.

Group coordination:
- direct one-agent chat bypasses group routing;
- multi-user groups enforce addressing even with only one agent;
- non-addressed agents are not invoked;
- observers can retain awareness for a later turn;
- deterministic addressing outranks semantic fallback;
- mention and address are distinguished;
- multiple addressed agents may run concurrently.
