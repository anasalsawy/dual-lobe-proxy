# Three brains: two reasoners and one observer

A proposal to split the work into **two parallel non-emotional reasoners** and
**one emotional observer**, instead of today's speaker/verifier pair.

## The shape

```
        user turn
            |
   +--------+--------+
   |                 |
Reasoner 1        Reasoner 2          <- non-emotional, parallel, peers
   |                 |
   +--------+--------+
            |
      converge / compare
            |
        the answer  +  a disagreement signal
            |
   Emotional observer  ------------> memory line only
   (watches everything)              (never touches the meter)
```

## Why three, not two

Today B is a **verifier of A** - it reacts to A's turn and checks it. A verifier
can be fooled: if A is confidently wrong in a plausible way, a single verifier
can agree. Two **parallel peers** are stronger, because neither is subordinate and
their **disagreement is itself the signal**: two independent reasoners who do not
agree cannot both be fooled the same way.

The third brain is separated by *function*, not by speaker: it is purely the
**affect/relationship line**. It never judges correctness, so affect can never
contaminate verification. That is a real safety property, not a style choice.

## The cost, stated honestly

- **Latency and money**: three model calls per turn instead of two.
- **The parallel pair only pays off if they genuinely diverge.** If both reasoners
  are the same model with the same prompt, you have paid double for one opinion.
  They must differ in model, temperature, or reasoning path. This is the single
  decision that makes or breaks the design - and it is empirical: you can only know
  by running it.

## It maps onto what exists

- The **seam is already there**: normalised messages in, an answer out, a side
  observer at turn end. Today that is `A + B-verify`; the three-brain shape is the
  same seam with the roles re-parameterised.
- The **emotional observer is the episodic memory's author** - B's notebook already
  is an emotional line. Making it a first-class brain 3 is a promotion, not a
  new concept.
- The **two reasoners replace the single speaker** with a pair, then converge.

## The rule that keeps this buildable

Which model, temperature, and role each brain uses must be **config, not code**:

```
DUAL_LOBE_BRAINS=reasoner1:modelA,reasoner2:modelB,observer:modelC
```

So `A + B-verify` today becomes `reasoner1 + reasoner2 + observer` tomorrow by
changing configuration, with no rewrite of the pipeline. Build the seam first;
the expensive part (the parallel pair) waits until the divergence question is
answered.

## Decided

- **The two non-emotional brains are different from each other, and both reason
  about DATA - facts and structure - not emotions.** Their divergence is over the
  substance of the answer, not over how it feels.
- **The emotional brain is the separate affect line** (the episodic notebook). It
  does not reason about data and never touches the meter.

The payoff ties directly into dual-perspective memory: because both non-emotional
brains reason about data, **recall carries valuable data too** - when A recalls a
moment he gets his own memory, the *other data-brain's angle* on the same moment,
and the observer's affect fragment. Recall is therefore a source of new *facts*,
not only new feeling.

## Decisions still open

1. Convergence: **agreement vote**, **one adjudicating**, or **surface the
   disagreement to the user**?
2. Does B *become* the observer, or is the observer a new brain with B staying as
   verifier?
3. Which models back the two data reasoners (they must differ - different model,
   temperature, or reasoning path - for divergence to be real).

Until these are answered, only the config seam should be built.
