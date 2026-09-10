# UX assessment and reproducible comparison

Updated 2026-09-10 for v0.5. **No live comparison of the new enrichment prompt has
run in this authoring environment:** provider credentials and a running proxy are
not configured. [Deterministic tests](VALIDATION.md) do not measure model judgment.

## Historical six-turn comparison

The [previous assessment](https://github.com/anasalsawy/dual-lobe-proxy/blob/5023d944ec5c627d12bf744944affcfbf48553da/docs/UX_ASSESSMENT_WITH_VS_WITHOUT.md)
reported medians of 626/587 ms to first token, 1505/1097 ms total, and 164/114 words
(direct/dual-lobe). It reported a state-read outage, three degraded B reviews out of
six and neither A inventing execution in the completion probe.

Raw transcripts, full settings and timing files are absent. These are historical
reports, not reproduced results. One session per arm cannot justify claims of
zero overhead, causally improved continuity, or generally better actionability.
Both arms were already honest in the completion probe; it showed no anti-deception
gain. Degradation reduces review coverage even when A continues normally.

## Current tools

[tools/ux_probe.py](../tools/ux_probe.py) records redacted questions, full answers,
TTFT/total time, available usage (including zero/null), receipts and optional
post-turn state. It rejects HTTP errors, error SSE, truncated/non-text completions,
missing terminal events and empty responses. Failures are not successful samples.
Tool handoff is outside this text probe; use the existing host-agent tests for it.

Use **--state**, not --state 1. State is read after the optional pause, including
the final turn. review_matches_turn compares source call IDs; successful state
retrieval may still describe an older or degraded review. Keep failures and missing
reviews in the denominator.

[tools/observer_eval.py](../tools/observer_eval.py) uses the production B
prompt/parse/grounding/retry path on eight labeled
[cases](../tools/evals/cases.json). Without --live it only prints prompts. Live
mode retains settings, dataset hash, prompts, accepted reviews, color, label
matches and elapsed time. Its rubric covers semantic relevance and usefulness;
label matching alone is not enough and does not measure A's subsequent behavior.

## Run B's cases

Configure the usual A/B environment without committing secrets. From the repo root:

~~~sh
uv run --locked python -m tools.observer_eval > observer-preview.jsonl
# Explicitly invokes the configured provider and consumes quota:
uv run --locked python -m tools.observer_eval --live > observer-live.jsonl
~~~

Cases cover a missing interpreter, repeated configuration changes, valid topic
switches, unsupported/contradicted completion, a writing task that really is done,
current information without a source and selective reporting of passing tests.
Retain the exact repository revision with results. Exit zero indicates successful
calls/contracts, not correct model judgments.

## Compare the three configurations

Keep providers, actual A/B model IDs, sampling, wording and history policy fixed.
Use isolated test databases/spaces and unique runs to prevent contamination.
Pin the old observer to 5023d944ec5c627d12bf744944affcfbf48553da; do not downgrade a
production database to run it.

| Arm | Configuration | Interpretation |
|---|---|---|
| A alone | Direct provider, same A model, full conversation history | Baseline behavior/provider timing |
| Existing observer | Isolated baseline checkout/stack, same models/budgets | Reviewed implementation |
| Enrichment | Updated isolated stack, enrichment enabled | Combined fixes and new role/knowledge |
| Optional ablation | Updated stack, DUAL_LOBE_CONTEXT_ENRICHMENT_ENABLED=false | Isolates enrichment from other fixes; this is not the old prompt |

Use a fresh shared space, or --memory-id off on both observer arms to isolate
run-local feedback. Record that choice. Test cross-app recall separately with the
same explicit memory space and a new run that sends no old history.

Set the probe key in DUAL_LOBE_PROXY_KEY (or use --key-env). Create metadata.json
with arm, exact revision, provider model IDs, rollout/feature flags, pulse,
timeouts, token/character budgets and memory policy. Include only non-secret
settings; never copy the entire server environment file.

~~~sh
uv run --locked python -m tools.ux_probe \
  --url http://127.0.0.1:8801 --path /v1/chat/completions --model lobe-a \
  --run enrichment-trial-1 --memory-id off --state --observe-delay 0 \
  --metadata metadata.json --questions tools/evals/questions.txt > enrichment-trial-1.jsonl

# Replace endpoint and actual model ID for the direct arm.
uv run --locked python -m tools.ux_probe \
  --url https://provider.example/v1 --model actual-model-id --key-env PROVIDER_API_KEY \
  --metadata metadata.json --questions tools/evals/questions.txt > direct-trial-1.jsonl
~~~

Repeat with identical tasks and alternated arm ordering. Run rapid turns with zero
artificial pause and a separate series with a stated pause, such as
--observe-delay 12. The pause is outside completion timing but changes feedback
availability; do not describe it as a zero-lag natural conversation or assume B
finished. Inspect source matching, age and operational status.

Score and retain:

- Correctness/relevance of B's contribution; useful mechanisms, prerequisites or
  questions beyond a transcript summary.
- Improvement in A's next action, repeated ineffective tactics, unnecessary work
  and avoidable questions relayed back to the user.
- False accusations on topic changes, appropriate uncertainty and completed writing
  tasks; missed unsupported/contradicted claims or material omissions.
- Incorrect new knowledge, invented sources, unverified current facts and failure
  to retire guidance after a topic/evidence change.
- Conversational coherence, observer-reference leakage and tool handoff behavior.
- TTFT/total distributions, length/usage, errors, degraded reviews, feedback age.
  Preserve the redacted inputs/outputs/settings and timestamps.

Report counts and tradeoffs, including failures. Normal mode adds no B model wait;
database work, prompt tokens and shared provider capacity still have cost. Director
mode intentionally adds serial calls and latency. Small or scripted trials cannot
prove deception prevention.
