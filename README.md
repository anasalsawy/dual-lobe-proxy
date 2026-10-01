# Dual-Lobe Proxy

An OpenAI-compatible inference proxy with two supported model variants. Both variants use the same two-lobe conversation router, tool handoff, peer consultation, verification meter, and shared-memory interface.

## The two models

| Model ID | Use it for | Lobe A | Lobe B |
|---|---|---|---|
| `sawii/dl-bidirectional` | General work | Configured provider model | Configured provider model; both lobes can speak and use caller tools |
| `sawii/dl-secure` | Sensitive/private work | Configured provider model; receives tokenized sensitive values | Local-only model; checks input before A, can speak/use tools, and verifies A |

The proxy exposes these two variants in `GET /v1/models`. Older aliases remain temporarily resolvable for existing clients but are no longer advertised.

## Conversation routing

A is the default user-facing lobe. Address a lobe at the start of a user message to select it for that turn:

- `Hey A, ...` or `Hey B, ...` selects the speaker.
- `A, ask B what it thinks: ...` has A privately consult B, then A answers and identifies B's contribution.
- `B, ask A what it thinks: ...` works in the opposite direction.
- `handoff_to_other_lobe` transfers the user-facing turn; the new speaker gets caller tools and the former speaker verifies.

The other lobe verifies the final answer and adds the GREEN/YELLOW/RED meter. If the speaker requests a caller tool, the proxy returns the tool call; submit its result in the next Chat Completions request and the tagged call ID routes the continuation back to the same speaker. Consultation does not expose hidden conversation to the user; the final answer includes the consulted lobe's material input with attribution.

## Secure variant boundary

For `sawii/dl-secure`, every request first goes to the local B privacy gate. B returns a private classification and sensitive-value spans. The proxy stores the value-to-token mapping in an encrypted, request-scoped vault and replaces those values before any remote A call. Deterministic rules also mask common identifiers, credentials, and structured sensitive fields. A can reason over the remaining context and tokens. When A sends a token in a caller-tool argument, the proxy resolves it at the tool boundary. Incoming tool results pass through the gate before A sees them. B may receive raw input because B is constrained to a local endpoint.

When B is user-facing, it receives the original input locally and A verifies a sanitized view of B's answer. When A is user-facing, B verifies A's answer before it is returned. The proxy restores known tokens in the final user-facing response after review.

The secure path requires `DUAL_LOBE_CLINICAL_B_BASE_URL` (or the configured B URL) to resolve to localhost, loopback, or `host.docker.internal`. Production rejects a remote B endpoint. `DUAL_LOBE_TESTING_MODE=true` with `DUAL_LOBE_CLINICAL_B_LOCAL_ONLY=false` is only for tests.

This is a proxy privacy boundary, not a formal data-loss-prevention guarantee. Detection can miss sensitive values, and downstream caller tools still receive resolved values they need to perform an action. Review tool permissions and retention at the connected runtime.

## Latency and calls

- Ordinary, unaddressed requests on the existing general gated route do only local routing detection; they make no routing model call.
- `sawii/dl-bidirectional` always uses the routed speaker/verifier flow.
- `sawii/dl-secure` adds a local B input-gate call on every request, then runs the routed speaker/verifier flow. Tool-result continuations are gated again.
- A peer consultation adds a private lobe call. A handoff adds a call to the new speaker. Client tools run outside the proxy and return through the next request.

Call provider/model details and the privacy-gate decision are included in the `dual_lobe` response metadata. Never treat a GREEN meter as proof of truth; it means the verifier found adequate support in the evidence it received.

## Configuration

Set the general provider pair with `DUAL_LOBE_A_MODEL`, `DUAL_LOBE_A_BASE_URL`, `DUAL_LOBE_A_API_KEY` and, optionally, `DUAL_LOBE_B_MODEL`, `DUAL_LOBE_B_BASE_URL`, `DUAL_LOBE_B_API_KEY`. If B is omitted, it inherits A. Configure provider round-robin slots as documented in `.env.example`.

For the secure service, configure `DUAL_LOBE_CLINICAL_B_MODEL`, `DUAL_LOBE_CLINICAL_B_BASE_URL`, and `DUAL_LOBE_CLINICAL_B_API_KEY` for a local OpenAI-compatible model endpoint. Set `DUAL_LOBE_ENGINE=clinical` to make that service use the secure route for all requests. Alternatively, use `sawii/dl-secure` on a service configured with a valid local clinical B endpoint.

Shared memory is selected with `X-DL-Memory-ID`. The secure route tokenizes message and response content before writing to shared memory. Use separate memory spaces for distinct privacy domains.

## Run and verify

```sh
cp .env.example .env
# Set provider URLs/keys and a strong DUAL_LOBE_BOOTSTRAP_KEYS value.
docker compose up --build -d
uv sync --locked --extra dev
uv run --locked pytest tests/unit -q
```

For database integration tests, run `uv run --locked pytest -q` with a permitted Docker daemon. Local tests use scripted model replies and do not prove model quality or provider compatibility. A live provider test requires configured provider credentials and a running proxy.

## Design notes

The implementation details, request-by-request routing matrix, privacy gate contract, token lifecycle, and test coverage are in [Two model variants](docs/TWO_MODEL_VARIANTS.md). The older design notes under `docs/` describe retired variants and are retained as historical material; this README is the current product contract.
