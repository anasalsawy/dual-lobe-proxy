# Dual-Lobe Proxy — Intended Design Architecture

Frozen design from the design conversation. Two runtime-gated architectures ship side by side; both share one tool registry and one memory store.

---

## 1. The two lobes

- **Lobe A** — the worker. Generates the answer, owns the client conversation.
- **Lobe B** — the verification peer. Grades A's output downstream (`GREEN/YELLOW/RED`), carries the handoff contract, and (in the splitter design) acts as a mid-turn worker half.
- **Proxy** — the server-side orchestrator. Owns tools, memory, injection, meter carryforward, merge. Works regardless of client: everything is enforced server-side, so any OpenAI-compatible client (Hermes, scripts, curl) gets the full behavior.

Client tools are **never executed by the proxy** — the proxy has no client environment. Client tool calls pass through untouched; on a name collision the client wins.

---

## 2. Architecture A — non-splitting (default)

```
injections → A (full task, may call proxy tools inline) → B verify (post-hoc, every turn) → response
```

- A has **three proxy tools** (see §4), executed inline during A's turn.
- B verifies downstream after A completes; meter + handoff travel in headers and next-turn carryforward.
- **All injections kept** (see §5). This design takes the "small / no split" path for every task.

## 3. Architecture B — splitting (heuristic-gated)

```
task + memory slice
      │
   heuristic pre-gate (simple turns skip the splitter entirely)
      │
   splitter (third model, dedicated — never does task work)
      │
      ├── mode: normal → A (full) → B_VERIFY            [identical to A]
      │
      └── mode: split  → A½ ∥ B½  (parallel fan-out)
                           │
                        merge (continuation call to A: append | integrate)
                           │
                        splitter verifies the merged output   ← B never grades its own half
```

Rules:

- **Splitter = a dedicated third model** (small, fast, cheap, own quota line, `DUAL_LOBE_SPLITTER_MODEL`). It must not share in the task: if it were B, B would both route and work — contradiction with the two-worker split.
- **Splitter sees the task AND the deterministic memory slice** before routing.
- **Splitter output** is a tiny routing JSON (~150 tokens): `{mode, fragments[≤2], merge: append|integrate, start}`. The splitter also consumes the merged output in split mode.
- **Two gates, both required**: (1) heuristic pre-gate — simple turns never pay the splitter; (2) economic criterion — split only when predicted wall-clock saving ≥ threshold (fan-out gain must exceed splitter + merge cost).
- **Latency math**: parallel wall-clock = max(A½, B½), not the sum. Split ≈ breakeven for pure reasoning (splitter + merge re-spend the fan-out gain); clearly wins when hiding external latency (fetch/search/tools), very long generations, or `merge: append` (self-contained halves).
- **Injections**: retires only `context_broadening` + `widen` (their advice is redundant once B works in parallel). Everything else stays (see §5).

Splitter is opt-in via `DUAL_LOBE_SPLITTER` (default off → ships as Architecture A behavior; flip on to activate §3).

---

## 4. Proxy tools (server-side, both architectures)

All three are **inline**: executed by the proxy during A's turn via `await`, no loop machinery, no hop caps. **Exactly one same-turn continuation** carries ALL proxy tool results back to A. Proxy tool artifacts are **stripped from client-visible messages** (the client never sees `proxy_*`).

| Tool | Who | Semantics | Budget |
|---|---|---|---|
| `proxy_memory_search` | A, inline | Deterministic FTS against the shared memory store; result back **same turn** | result ≤2600 chars |
| `proxy_delegate` | A → B | A offloads real **work** to B; B acts as worker; A absorbs B's output as its own voice (silent absorption — B's voice never leaks to the client) | B output ≤300 tok, cap 1/turn |
| `proxy_consult` | A → B | A asks B a **question** inline and waits — advisory only ("what's the next step?", "what do I do here?"). Not delegation. Feeds A's same-turn continuation | B output ≤200 tok, cap 1/turn |

`proxy_delegate` exists in **both** architectures. In Architecture B the splitter governs *task* routing; A-initiated delegate/consult remain available on top.

### B emitting client tool calls (downstream)

- B's contract gains `tool_calls[]`. The proxy merges it **downstream into the same assistant message** at the existing injection point — no routing store needed.
- Because the transcript is shared, **tool results reach both lobes** next turn.
- **Dedupe A↔B** by `(name, normalized args)`: same tool called by both → executed **once**, one result, both see it.
- B limits: ≤2 client tool calls per verdict, allowlist-filtered to tools present in the merged request set, **no proxy tools for B in v1**, token budget `2400 → ~2700`.

---

## 5. Injections

| Injection | Arch A | Arch B (splitting) |
|---|---|---|
| `OBSERVATION_DISCLAIMER` (fixed position, verbatim) | kept | kept |
| Role persona (hierarchy, `dl-dialogue` aliases) | kept | kept |
| Meter carryforward (YELLOW/RED warning) | kept | kept |
| Shared memory slice (user-role, after system block) | kept | kept |
| `context_broadening` | kept | **retired** |
| `widen` (handoff item) | kept | **retired** |

- Differentiation between the disclaimer and the persona is by **position and framing**, not rewording the disclaimer.
- **Meter carryforward stays in both** — it is feedback on A's own past verdicts (governance), not advice; it is not replaceable by the delegation channel. B's reasoning/handoff never travels as text advice.
- `memory_query` (B's targeted history search) stays in both designs.

---

## 6. Memory — one durable store, three access paths

**Stores:** exactly **one durable memory store** (Postgres shared spaces). Two transient companions that are not "memory": observer shadow memory (per-run, TTL, generic path only) and the in-loop meter/handoff state (dies with the run).

**Three access paths into the one store:**

1. **Auto slice** — `load_memory`, deterministic (no LLM in the loop): pinned notebook + 3 most recent + ≤4 FTS hits + first entry → composed slice ≤10k chars, injected pre-model every turn. **Fail-open** in gated/dialogue wiring (the generic path keeps its fail-closed 503).
2. **B-directed search** — B's `memory_query` (≤160 chars) → proxy runs deterministic FTS → hits ride into `memory_hits` → recorded → resurface in **the next turn's** auto slice. B only picks the words; retrieval is code.
3. **`proxy_memory_search`** — A-directed, deterministic FTS, result back **same turn** via the one continuation.

**Record is always a `BackgroundTask`** in every new wiring — the DB never adds latency to a turn.

Headers: `X-Dual-Lobe-Memory-Space`, `X-Dual-Lobe-Shared-Entries`; handoff carries `X-Dual-Lobe-Memory-Query` / `-Memory-Hits`.

**No run store / no cross-turn proxy-tool re-injection**: proxy tool effects are used same-turn (inline), and future-turn evidence persists through the normal memory pipeline (record → auto slice). Session-level tool logging is deferred (default: none).

The splitter, in Architecture B, receives the memory slice along with the task.

---

## 7. Verification & meter

- B verifies downstream every turn (Arch A) or the splitter verifies the merged output in split mode (Arch B). **B never grades its own work.**
- `deception_level` GREEN/YELLOW/RED + rationale in headers; meter line appended to A's visible content.
- Flip-back (`DUAL_LOBE_GATED_FLIP_BACK`) unchanged, default **off**.
- Handoff (next_step/missing/unverified/widen/memory_query) travels in **observability headers only**, never as advice inside A's context.

---

## 8. Latency budget

- Baseline gated turn ≈ **10s** (A + B verify).
- Proxy tools: add one B/FTS call + one continuation only when A actually uses them.
- Splitter (opt-in): +1–2s routing call, gated so simple turns pay 0; splits must beat baseline on predicted wall-clock or they don't split.

---

## 9. Build plan

### V1 — shared-memory wiring (current)
1. ✅ `api/chat.py`: load + inject + BackgroundTask record + memory headers in **dialogue** branch.
2. ✅ `api/chat.py`: same for the **gated** branch (`shared_text`/`shared_space` into `gated_response`).
3. ⬜ `coauthor/handler.py`: `shared_text` param + `inject_shared_memory` into canonical messages.
4. ⬜ Tests: render cap 3200, `widen`/`memory_query` extraction + render, memory unit + branch wiring tests.
5. ⬜ `compileall` + `pytest` green.
6. ⬜ Commit, push (GitHub unblocked), `railway up --detach`, live verify: slice present in a gated turn, records land (inspectable via memory notebook/inspect endpoints).

### V2 — proxy tools + splitter
1. `proxy/tools.py`: registry + inline executor (one continuation, strip `proxy_*`, settings/caps) + registered tool settings (`DUAL_LOBE_PROXY_TOOLS` etc.).
2. `proxy_memory_search`, `proxy_delegate`, `proxy_consult`.
3. B contract `tool_calls[]` → downstream merge → A↔B dedupe → allowlist → budget bump.
4. Splitter subsystem: `DUAL_LOBE_SPLITTER` (default off), `DUAL_LOBE_SPLITTER_MODEL`, heuristic pre-gate + economic criterion, fragment routing, merge (`append`|`integrate` continuation), splitter verification in split mode, injection retirement behind the flag.
5. Tests + `compileall` + `pytest` + deploy + live verify (client never sees `proxy_*`; delegate/consult turns; split turn end-to-end; headers; latency baseline).

### Deferred / defaults unless decided otherwise
- Session-level durable tool log: **none for now**.
- `DUAL_LOBE_A_SYSTEM_ADDENDUM` (A system-prompt note about the channel): **not yet**.
- Handoff fields stay observability-only headers.

---

## 10. Environment

- Repo `C:\Users\anasa\dual-lobe-proxy` → GitHub `anasalsawy/dual-lobe-proxy` → Railway `energetic-simplicity` / service `dual-lobe-proxy`.
- URL `https://dual-lobe-proxy-production-e78e.up.railway.app`; deploy `railway up --detach`; `railway variable set` needs `--skip-deploys`.
- A = `nvidia/nemotron-3-super-120b-a12b:free` @ OpenRouter (B shares the same key/quota line; Gemini quota exhausted).
- Test: `python -m pytest tests/ -q` (unit suite cap 3200); syntax gate: `python -m compileall src`.
