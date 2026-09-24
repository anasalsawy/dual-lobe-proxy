# `sawii/dialogue` — free A↔B co-author + always-visible deception guard

This is the second Dual-Lobe model beside `sawii/dl-gated`.

## Intended experience

```text
USER
  ↓
B upstream
  ├─ sees canonical user message first
  ├─ removes B-directed conversational material from A's private user view
  └─ talks to A with an independent broadening/co-author thought
  ↓
A answers
  ↓
B downstream
  ├─ replies directly to A if A addressed B
  ├─ replies directly to user if user addressed B
  ├─ adds independent co-author material
  └─ ALWAYS verifies A for unsupported/fabricated/conflicting claims
  ↓
A gets one immediate reaction boundary when B spoke to A privately/directly
  ↓
VISIBLE SHARED CONVERSATION
```

Typical output:

```text
A: I think the deployment succeeded...

B: I disagree with that part. The available tool result does not confirm success.
I checked the rest of A's answer and found no other material unsupported claim.

A: You're right — success should remain unverified until a real result confirms it.
```

## Upstream contract

B returns:

- `user_for_a` — faithful A-facing version of the newest user message.
- `to_a` — B's independent broadening/co-author thought to A.
- `b_only` — user material/questions specifically addressed to B, retained so B can answer directly downstream.

Canonical history is never deleted.

## Downstream contract

B returns four distinct conversational functions:

- `reply_to_a` — direct response when A talked to B.
- `reply_to_user` — direct response when the user talked to B.
- `coauthor_to_a` / `coauthor_to_user` — independent substantive contribution.
- `verification` — mandatory deception/evidence check on A every turn.

Verification checks include fabricated tool use, claimed inspection/execution that did not occur, invented results/artifacts, contradictions with evidence, unsupported certainty, silent task drift, and false claims of verification/completion.

`verification.to_user` is never allowed to be empty. A clean response therefore still gets a visible B verification instead of a silent PASS.

## Evidence rules

- Tool results, artifacts, and explicit user-provided facts outrank A/B prose.
- A/B statements are claims, not evidence.
- Missing evidence means unsupported, not necessarily false.
- B guards the output; it does not infer deceptive intent or motive.

## Visible format

Completed responses are lobe-labelled:

```text
A: ...

B: ...

A: ...   # only when A has a useful same-call reaction to B
```

## Headers

- `X-Dual-Lobe-Coauthor: on`
- `X-Dual-Lobe-Coauthor-Action: ...`
- `X-Dual-Lobe-Meter: GREEN|YELLOW|RED|UNAVAILABLE`
- `X-Dual-Lobe-Meter-Rationale: ...`
- `X-Dual-Lobe-B-Visible: always`
- `X-Dual-Lobe-Upstream: ok|unavailable`
- `X-Dual-Lobe-Downstream: ok|unavailable`
- `X-Dual-Lobe-A-Reaction: yes|no`

## Validation performed

- Modified files compile inside the full `dual-lobe-proxy` tree.
- GREEN/PASS deterministically produces a visible B verification if the model omits one.
- `reply_to_a`, `reply_to_user`, co-author material, and verification remain distinct internally and combine into B's visible message.
- B's private co-author + verification guidance combines into A's peer handoff.
- Prompts explicitly require direct B→User and B→A responses and mandatory visible verification.

The repo's normal pytest harness could not run in this environment because its global `tests/conftest.py` requires the unavailable `testcontainers` package. The focused contract checks were therefore run directly with Python against the full source tree and passed. This is not yet a live provider/Railway test.
