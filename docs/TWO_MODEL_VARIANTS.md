# Two model variants: routing and privacy contract

This document is the current implementation contract for the two public variants in the Dual-Lobe Proxy. Older branch/engine descriptions are historical and do not define the public model surface.

## 1. General: `sawii/dl-bidirectional`

A and B are provider-backed peer lobes. Configure them independently with the A/B settings; either can receive the user-facing turn, use caller-supplied tools, privately consult the other, and hand off the turn. Default speaker is A. The opposite lobe verifies the finished answer and the proxy adds the meter.

| User intent | Speaker | Peer action | User-visible result |
|---|---|---|---|
| No lobe addressed | A | B verifies | A answer plus meter |
| `Hey A, ...` | A | B verifies | A answer plus meter |
| `Hey B, ...` | B | A verifies | B answer plus meter |
| `A, ask B ...` | A | B privately answers A | A reports useful B input with attribution, then B verifies |
| `B, ask A ...` | B | A privately answers B | B reports useful A input with attribution, then A verifies |
| Speaker calls `handoff_to_other_lobe` | Other lobe | Former speaker verifies | New speaker's answer plus meter |
| Speaker calls caller tool | Same speaker resumes after tool result | Peer verifies when finished | Tool call is returned to caller, then final answer plus meter |

Routing detection on the ordinary gated route uses local text/regex checks only. If no explicit route is requested, that route continues through its existing code path without a routing-model call. `sawii/dl-bidirectional` explicitly selects the routed speaker/verifier path every turn.

## 2. Secure: `sawii/dl-secure`

The secure route adds the local B privacy gate in front of the routed flow. The `clinical` service setting selects this secure route for every request; despite the legacy setting name, it is a Dual-Lobe Proxy mode, not a separate CrewAI repository.

### Input boundary

1. All supplied messages, including tool results and selected shared memory, reach the local B gate first.
2. B classifies the input and returns sensitive categories and exact value spans. That gate output stays inside the proxy.
3. The proxy creates request-scoped encrypted tokens and also applies deterministic rules to known structured keys and common identifier/credential formats.
4. A receives only sanitized text and opaque tokens. A's system context says tokens are opaque and must remain unchanged.
5. B can still speak or consult A. If B receives the turn, it sees the original content locally; any message sent to remote A is sanitized.

### Tool boundary

Both lobes receive caller-supplied tool schemas when they are the speaker. The client executes tool calls and sends results on the next request. A can include an opaque token in a protected tool argument; the proxy resolves that token immediately before returning the tool call to the connected runtime. Tool results re-enter the proxy and are gated before remote A sees them. A token that cannot be resolved remains opaque; the proxy must not guess the raw value.

### Output boundary

For an A-facing turn, local B reviews A's candidate. For a B-facing turn, remote A receives a sanitized candidate and verifies it. The proxy restores tokens it knows after review and adds the usual meter. Raw values from the gate's sensitive-span output are not included in user-visible metadata.

### Local-only enforcement

The secure route calls `_assert_clinical_b_local()` before making the gate request. A B URL is considered local only for localhost, loopback IPs, or `host.docker.internal`. Production must fail closed if the configured endpoint is remote. Testing mode can explicitly relax this check; never use that override for sensitive production traffic.

### Memory and persistence

Shared memory uses a sanitized copy of incoming messages and the response. Tool-call argument bodies are withheld in that persisted copy. Use separate memory IDs for separate privacy domains. Request-scoped token keys are destroyed at the end of the response and are not a persistent secret store.

### Limitations to retain in reviews

- Sensitive-span detection depends partly on a local model and partly on deterministic patterns; neither is a formal proof that every private value was found.
- An invalid local-gate result makes A's user content fail closed for that request. A local-gate transport failure aborts before an A call.
- The client tool runtime receives resolved values when an action needs them. Its own logs, telemetry, and retention remain separate from the proxy's safeguards.
- The input gate adds one local B model call per secure request; regular general traffic does not take this gate.
- A GREEN meter is an evidence-based reviewer judgment, not a guarantee of correctness.

## 3. Provider and call accounting

`dual_lobe.calls` is the request's per-provider call trace. `dual_lobe.model_calls` should count the local gate, speaker, consultation/handoff, and verifier calls made during that request. General unaddressed gated requests do not add a routing model call. Secure requests always include the local gate call, even if no sensitive value is found.

A/B provider slots configured in the provider hub can round-robin general lobe calls. The secure B alias bypasses that hub and targets its configured local endpoint directly.

## 4. Tests

`tests/unit/test_bidirectional.py` covers default/direct speaker selection, both consultation directions, both handoff directions, tagged tool continuations, both lobes' caller-tool access, secure input masking, B-as-speaker with sanitized A verification, and proxy token resolution for a protected A tool call. `tests/unit/test_engines.py` covers the separately callable legacy clinical engine; it is not the public secure route.
