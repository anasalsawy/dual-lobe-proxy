# Current checkpoint — Self-Splitting Dual-Lobe

The active Split architecture no longer uses a dedicated Splitter model.

Current production concept:
- A receives the full task first.
- A is explicitly a professional splitter-executor.
- A must find a valid two-way independent split when one is likely to reduce wall-clock time.
- split_channel launches B's independent half in the background and returns immediately.
- A executes its own half concurrently.
- deterministic append is preferred when possible; A merge is used only when synthesis is necessary.
- B verifies the final answer and grades the split decision in the same turn.
- measured timing plus B's reusable split lesson is persisted as split-experience memory.
- future A routing decisions receive relevant past split experience.

Gated and Non-Split remain as controls.

The old dedicated-Splitter implementation is preserved only in Git history.
