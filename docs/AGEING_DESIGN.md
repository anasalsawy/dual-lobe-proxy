# SAWII AGEING DESIGN — Persistent Verify / Persistent Update

Status: DESIGN FINAL (agreed 2026-10-11, Anas + Hermes, live debate)
Owner: Anas Alsawy (sawii/dual-lobe)
One line: the model ages because every exchange is verified while awake and consolidated while asleep. The day proposes, the night decides.

## 1. WHY (the problem this kills)
LLM serving is a read-only movie projector: checkpoint loaded once, weights are constants,
`no_grad()`, no write path. Continuity today = re-feeding the whole transcript every call
(context stuffing) — amnesia with a diary. There is no save button because nobody has a safe
loss function for live experience. Dual-lobe was built to supply exactly that missing piece:
B is the persistent verifier, the teacher that production LLMs never have.

## 2. ARCHITECTURE (three organs + one spine)
- A (actor): serves conversation. Runs open-weights so its weights are addressable.
- B (verifier, EXISTS, WORKING): grades every exchange (quality, truth, salience) at inference
  time, inside the loop. Its verdict is now the loss signal, not just a gate.
- UPDATE (the new wire): every exchange writes a gradient step into a FAST LAYER at a sane
  learning rate, always, during normal day-to-day use. No separate "training sessions."
- THE JOURNAL (spine): every update appended as an immutable entry with its trigger exchange.
  The day must be replayable and revertible. No journal = no honest night.

### Two layers, two timescales (biology-mirrored)
- FAST layer (hippocampus): a resident LoRA adapter, updated live, every exchange. Holds the
  week: mood, temperament, current customer patterns. Grain-of-sand writes — each exchange
  moves weights ~1e-6 vs magnitudes ~1e-2..1, so one event CANNOT scar the system; ~10^4
  same-direction pushes are needed for a visible shift. Weight-space is itself the low-pass
  filter: noise cancels, patterns accumulate.
- SLOW layer (cortex): the merged base + long-lived adapter. Only what the night pass promotes
  gets here. Character, persona, reflexes. Changes weeks-over-weeks, never hours-over-hours.

### NIGHT RUN (consolidation — the court, not the classroom)
Triggered offline (idle/schedule), reads the day's closed journal:
1. B RE-SCORES the day with hindsight (not reflex). Outliers dilute against full-day evidence;
   a closed day cannot be performed for — manipulators only work on the awake model.
2. PROMOTE: exchanges passing re-score are batched into a consolidation step on the slow layer.
3. REVOKE: journaled fast-layer updates that fail re-score are reverted (safe because grain-
   of-sand writes are by definition non-load-bearing).
4. ANCHOR GATE: run the frozen golden set (see §4). If the night's candidate weights fail any
   anchor case beyond threshold, the promotion is rejected wholesale. Night cannot rewrite
   the spine.
5. Wipe/decay part of the fast layer (forgetting is a feature: the day is working memory).

## 3. THREATS AND OWNERS (every objection, with its owner)
- One bad experience rewriting the model -> learning-rate arithmetic (~1e-6 nudges). Owner: physics.
- Noise accumulation -> SGD selection: random pushes cancel, consistent ones survive. Owner: math.
- Deliberate update-farming (user trains consistent gradients to bend the model) -> B watches
  for repeat-direction update pressure per source; the deception meter becomes the immune
  system of the weights. Owner: B + night re-audit of a closed ledger.
- Self-reinforcing drift (B grades with the same machinery being mutated) -> ANCHOR, §4.
  This is the only ghost that needs a designed answer, not a physical one.
- Manipulating the night pass -> journal is append-only, per-source quota per night; a single
  actor's day is a slice, never the whole docket.

## 4. THE ANCHOR (the untouchable outside)
Frozen eval set, NEVER in any training/journal path:
- ~50 golden exchanges: Anas's own corrections ("WRONG, not that, THIS") — highest-purity
  loss signal available; plus core persona facts and identity invariants.
- B is re-calibrated against it every night (B drifts? the anchor catches B, not just A).
- Promotion gate: any nightly candidate must score >= threshold on anchors BEFORE merge.
- Anchor grows only by Anas appending, never by the system. This is the world-the-model-
  cannot-forge; without it the loop is a closed circuit and drift is invisible to itself.

## 5. IMPLEMENTATION PHASES
P0 (plumbing, no risk): journal every exchange with B's grade + salience to journal.jsonl.
    Schema: {ts, session, source, exchange, b_grade, b_flags, salience, delta_stats}
P1 (offline proof): nightly consolidation script — Unsloth/Axolotl LoRA on promoted exchanges,
    anchor gate before merge. Model still served frozen; night just produces candidate adapters.
    Verify: anchor score deltas across weeks. (This is the "cheap update" — proves signal quality.)
P2 (fast layer live): resident LoRA with per-turn optimizer step inside the serving process
    (open-weights model served locally: vLLM + adapter, or in-process HF). Learning rate
    CONSERVATIVE (1e-6..1e-5); hot LR is the one self-inflicted wound — flag it in config, never default.
P3 (full loop): night pass consumes journal, promotes/revokes against P2's live layer,
    anchor-gates, re-calibrates B, decays fast layer. System is now ageing.
Each phase gated by real evidence before the next (Anas rule: unverified pipeline is a rumor).

## 6. HONEST LIMITS (no selling fog)
- Nobody runs live-gradient self-updating LLMs in production. Test-time training works at lab
  scale; the grain-of-sand + journal + night-anchor stack is designed as the safety case, but
  P0/P1 data will tell the truth before P2 opens the gate.
- An ageing model is mortal: no re-download, no clean restore. Anchor + journal are the only
  continuity insurance. That mortality is the price of the thing being built, not a bug.
- SLOW layer still needs periodic human review by Anas (personality audits, monthly).
- Everything here is engineering, none of it is physics. That is why it can be built.

## 7. OPEN CHOICES
- Fast-layer LR value + decay rate: tune on P1 data.
- Journal storage: jsonl now, sqlite when multi-session.
- Serving stack for P2: vLLM+adapter vs HF in-process (in-process simpler, vLLM faster).
- Anchor bootstrap: start harvesting Anas's correction history from session logs.