# Project memory

## Project facts
- Dual-Lobe Proxy: OpenAI-compatible inference proxy; A speaks, B verifies, C observes (affect line). Public models: sawii/dl-bidirectional, sawii/dl-secure.
- Repo /workspace/project/repo, branch `three-brains-observer` (2026-10-03). GitHub anasalsawy/dual-lobe-proxy. Not pushed this session.
- Persona runtime: /workspace/project/repo/persona (own pyproject/Dockerfile/tests; OpenHands SDK agent per persona, proxy as its only LLM; voice WS, timesense, heartbeat, outbox, profile-as-API-key). 41 tests. Committed 52fbfeb.
- Tests: `uv sync --locked --extra dev`; start Docker (`sudo dockerd >/tmp/dockerd.log 2>&1 &` then `sudo chmod 666 /var/run/docker.sock`); `uv run --locked pytest -q` -> 310 pass. CI also: compileall src tests alembic; `python -m dual_lobe.demo`.

## Emotional chord (replaces agreed-but-superseded charge scalar)
- User's point: emotions are concurrent (bittersweet) - no single scalar/label. Shipped: chord = weights over closed axes (joy/sadness/fear/anger/tenderness/pride), parse clamps/drops unknowns, <=4 loudest voices. `src/dual_lobe/b/chord.py` pure; feeling tag demoted to rendering (`describe()`).
- Storage: memory_notes.chord JSONB, migration 0008; {} rows use legacy tag-equality fallback.
- Recall both directions: chord_state (salience-weighted avg, recent 6) cosine >= 0.45 -> surfaces quotes; lexical matches carry their chord to A. Advisory renders loud voices, forbids naming axes; never touches meter/verification.
- Strict-pydantic gotcha: chord field typed loose `dict` on the wire - strict dict[str,float] drops whole payload on int weights.
- No external embedding APIs (user constraint): local/in-perimeter only, conditionally.

## Open docs intent
- docs/THREE_BRAINS.md, COMPANION.md (Phase 2 surface tagging), EPISODIC_MEMORY.md (updated for chord), RECIPIENT_ROUTING.md, TWO_MODEL_VARIANTS.md.
- Production/public-build tasks standing; persona voice needs real ASR/TTS adapter; channel bridges (WhatsApp/Telegram) thin adapters pending.