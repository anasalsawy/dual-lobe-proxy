# Design variants for head-to-head evaluation

Every variant uses the same proxy, persistent memory, redaction, and request
contracts. Select one with `DUAL_LOBE_DESIGN_VARIANT` in the environment. The
profiles in this directory are deliberately explicit; changing a prompt or
model does not silently change the control policy.

| Variant | Runtime behavior | Main question |
|---|---|---|
| `peer-observer` | Default. A answers immediately; B reviews the same canonical peer snapshot in the background. | Does useful independent review improve the next turn without blocking work? |
| `strict-gatekeeper` | B reviews synchronously. A's user-facing answer is withheld unless B explicitly reports complete proof coverage. Missing or malformed B output blocks. `stream=false` is required. | Does fail-closed proof gating reduce false success, and how much useful work does it block? |
| `director` | B is visible and takes the user's conversational place while directing A through bounded turns. | Does explicit visible questioning solve blockers better than background notes? |
| `parallel-debate` | B receives the same canonical inputs and uses an independent-debate prompt. Its review remains background and fail-open; this profile does not synthesize a second user-visible answer. | Does independent reasoning add value over observer review? |
| `strategist-executor` | Comparison profile inspired by `dual-lobe`: B uses a strategist/verifier prompt while A remains the action lane. This profile does not add a hard execution lock. | Does explicit role specialization improve recovery and evidence quality? |

## What is common to every profile

The proxy creates one canonical peer snapshot containing the effective messages,
tool definitions, artifacts, structured tool results, run scope, and memory
space. That snapshot is the source for both lobes. A cannot receive a proxy-side
context item that B is denied by an accidental second assembly path.

The model roles remain distinct in output: the selected user-facing lane writes
the answer, while the other lane provides review or direction. Equal peer access
does not mean exposing provider-private hidden reasoning tokens. Peers exchange
structured conclusions, evidence, tool requests, and state.

## Comparison discipline

Run the same task corpus, provider/model settings, tool definitions, workspace
state, and memory space against each profile. Record the variant name in every
run. Compare verified success, false-success rate, unnecessary blocks, recovery,
latency, tokens, tool calls, and user-visible interruptions. The strict profile
is intentionally allowed to fail closed; do not compare its completion rate
without reporting its block rate.

The last two profiles are prompt-policy comparisons, not implementations of the
research repository's full planner/executor lock state machine or a same-turn
second-answer synthesis.
