# Current checkpoint — Self-Splitting Dual-Lobe

The active Split architecture no longer uses a dedicated Splitter model.

Current production concept:
- A receives the full task first and is the professional splitter-executor.
- If a valid time-saving two-way split exists, A defines both independent halves and calls `split_channel`.
- A executes its own half while B executes the peer half concurrently.
- A returns only its half. There is no collect tool and no A merge/finalize pass.
- The runtime waits for both halves, then sends both to B's finalizer/verifier.
- In one B call, B merges the halves, removes duplication, resolves contradictions, repairs obvious deficiencies without inventing unsupported facts, verifies the completed answer, grades the split, and emits one canonical final answer.
- Split timing plus B's reusable split lesson is persisted as split-experience memory.
- Future A routing decisions receive relevant past split experience.

Loop mode:
- every cycle ends at B with exactly one canonical state;
- that B-finalized state is fed into the next cycle;
- A and B never persist as divergent branches across cycles;
- CLI supports `--loop-cycles N`.

Gated and Non-Split remain as controls.

The old dedicated-Splitter and A-merge implementations are preserved only in Git history.
