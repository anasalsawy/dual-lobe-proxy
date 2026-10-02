# Dual-Lobe Proxy

An OpenAI-compatible inference proxy with two supported model variants. Both variants use the same two-lobe conversation router, tool handoff, peer consultation, verification meter, and shared-memory interface.

## The two models

| Model ID | Use it for | Lobe A | Lobe B |
|---|---|---|---|
| `sawii/dl-bidirectional` | General work | Configured provider model | Configured provider model; both lobes can speak and use caller tools; B verifies A by default |
| `sawii/dl-secure` | Sensitive/private work | Configured provider model; receives tokenized sensitive values | Local-only model; checks input before A, can speak/use tools, and verifies A |

The proxy exposes only these two public model IDs in `GET /v1/models` and rejects every other model ID at the chat endpoint. Internal A/B provider targets are not separate products.

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

The secure path requires `DUAL_LOBE_SECURE_B_BASE_URL` (or the configured B URL) to resolve to localhost, loopback, or `host.docker.internal`. Production rejects a remote B endpoint. `DUAL_LOBE_TESTING_MODE=true` with `DUAL_LOBE_SECURE_B_LOCAL_ONLY=false` is only for tests.

This is a proxy privacy boundary, not a formal data-loss-prevention guarantee. Detection can miss sensitive values, and downstream caller tools still receive resolved values they need to perform an action. Review tool permissions and retention at the connected runtime.

## Latency and calls

- Ordinary, unaddressed requests on the general model stay on the existing gated A/B verification path. Explicit routing uses a local check and adds no separate router-model call.
- `sawii/dl-bidirectional` keeps unaddressed turns on the ordinary gated path; explicit addressing opts into the routed speaker flow. The routing check is local and adds no router-model call when unused.
- `sawii/dl-secure` adds a local B input-gate call on every request, then runs the routed speaker/verifier flow. Tool-result continuations are gated again.
- A peer consultation adds a private lobe call. A handoff adds a call to the new speaker. Client tools run outside the proxy and return through the next request.

Call provider/model details and the privacy-gate decision are included in the `dual_lobe` response metadata. Never treat a GREEN meter as proof of truth; it means the verifier found adequate support in the evidence it received.

## Configuration

Set the general provider pair with `DUAL_LOBE_A_MODEL`, `DUAL_LOBE_A_BASE_URL`, `DUAL_LOBE_A_API_KEY` and, optionally, `DUAL_LOBE_B_MODEL`, `DUAL_LOBE_B_BASE_URL`, `DUAL_LOBE_B_API_KEY`. If B is omitted, it inherits A. Configure provider round-robin slots as documented in `.env.example`.

For the secure model, configure `DUAL_LOBE_SECURE_B_MODEL`, `DUAL_LOBE_SECURE_B_BASE_URL`, and `DUAL_LOBE_SECURE_B_API_KEY` for a local OpenAI-compatible model endpoint. The public model ID `sawii/dl-secure` selects the secure route. No service-wide engine mode is used.

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

## Companion and multi-brain (proposals)

- **docs/COMPANION.md** - one persona, many surfaces. Persona = API key: any
  surface presenting the key shares one memory. Phase 1 (persona column +
  scope) is implemented.
- **docs/DUAL_PERSPECTIVE_MEMORY.md** - one event, two memory lines: A's journal
  plus B's contemporaneous observation, so recall carries a second perspective.
- **docs/THREE_BRAINS.md** - two parallel non-emotional reasoners + one emotional
  observer; builds on the existing A/B seam via configuration.

## Episodic memory

At the end of a turn, B also acts as a quiet observer: it records small, personal
things the user said that nobody followed up on, each carrying the user's own
verbatim words, and A can recall them when the same topic returns. Notes are
quotable and auditable - B cannot manufacture a memory. Disable with
`DUAL_LOBE_EPISODIC_MEMORY=false`. See [Episodic memory](docs/EPISODIC_MEMORY.md).

## Design notes

The implementation details, request-by-request routing matrix, privacy gate contract, token lifecycle, and test coverage are in [Two model variants](docs/TWO_MODEL_VARIANTS.md). The repository contains only the two variants documented here; group-chat routing and A/B conversation routing are separate features described in [Recipient routing](docs/RECIPIENT_ROUTING.md).
