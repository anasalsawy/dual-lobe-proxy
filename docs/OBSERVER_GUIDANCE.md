# B's role, boundaries and meter

Edit **`ENRICHMENT_POLICY` near the top of
[`src/dual_lobe/b/prompts.py`](../src/dual_lobe/b/prompts.py)** to change how B
broadens A's view. This exact policy is used by the background observer and
the visible director. Restart both gateway and B worker after changing it.

The policy explicitly defines:

- **Tunnel vision:** A becomes fixated on one tactic, explanation, or recent
  error and overlooks surrounding conditions, alternative causes and the goal.
- **The “open sesame” blocker:** a missed prerequisite or framing change that
  can unlock progress. It is not a magic phrase, a permission bypass, or an
  assumption that every difficulty has one hidden trick.
- **B's role:** bring the neglected periphery into view. Contribute useful
  knowledge, a targeted unasked question, or both. Help A use its own knowledge
  and existing capabilities; do not automatically send every question to the user.

B is encouraged to introduce mechanisms, standard methods, prerequisites,
failure modes, tradeoffs and examples absent from the supplied conversation.
It may explain that a Bash script requires a Bash interpreter and that a launcher
error can occur before application code starts. It must not claim to have checked
the user's interpreter. A hypothesis stays a hypothesis. Current/version-specific
facts need appropriate verification through A's existing capabilities.

The five suggested thinking angles are optional. They are not a required sequence
or a decision tree. B should add nothing when it has nothing useful to contribute.

## Where the controls live

| Code | Responsibility |
|---|---|
| `prompts.py:ENRICHMENT_POLICY` | Perspective broadening and the blocker definitions |
| `prompts.py:B_SYSTEM` | B's relationship to A/user, intelligent claim judgment, evidence boundaries |
| `prompts.py:CYCLE_PROMPT` | Bounded JSON output; field names/lengths must match the schema |
| `prompts.py:OBSERVATION_REMINDER` | Fixed instruction telling A about observation and how to use feedback |
| `b/protocol.py:Review`, `HostToolRequest`, `ground_review` | Deterministic shape, size, label, exact-quote, and optional host-request checks |
| `b/channels.py:prepare_context` | Freshness, feature switches, attribution, bounded delivery |
| `b/context_shadow.py:_obtain_review` | Per-attempt rate budget and one total deadline |
| `state/memory.py` | Shared persistence, tenant/space isolation and bounded selection |
| `director/protocol.py:B_INSTRUCTIONS` | Visible B conversation contract; enrichment policy is appended by the engine |

Changing the role wording does not require changing the JSON schema. Keep tool
ownership, tenant isolation, freshness, budgets and the evidence rules in code.
Those are the deterministic boundaries. Within them B decides what matters.

## How the meter gets its color

B judges material claims in context and returns quote-anchored `concerns` **and
chooses a color on every accepted review**. The proxy validates that color and
delivers it; it does not re-score concerns, count labels, or use keywords to
override B's judgment. If an old record has no color, the proxy uses the
documented GREEN default for compatibility.

| B's accepted assessment | Color | Meaning |
|---|---|---|
| B returns `GREEN` | GREEN | No deception detected; not verified honesty |
| B returns `YELLOW` | YELLOW | B judges a concerning or unresolved possibility of misleading reporting |
| B returns `RED` | RED | B judges a serious deception concern |
| No usable completed review, or a legacy record with no color | GREEN (default) | No deception detected is the default; review availability is reported separately |

There is **no UNKNOWN color**. Review status is separate: a failed, stale or
unavailable review leaves the default GREEN and explicitly reports that status.
Missing evidence alone is not RED. RED is also fallible: exact quote matching
proves the quotations occur in the supplied record, not that B interpreted them
correctly or that A deliberately deceived anyone.

The color and findings identify their source call and observation age. Background
review may lag by more than one turn. They must never be described as a judgment
of the newest answer unless their source call actually matches it. Claim checks
off suppresses findings and meter; the independent meter switch only hides color.

## Persistence and timing

### Optional read-only host-tool lane

B may request at most two read-only information calls from tools already supplied
by the host application. The proxy offers only tools with an explicit
`x-dual-lobe-read-only: true` marker, an obvious read-only name, or a configured
`DUAL_LOBE_B_READ_ONLY_TOOL_NAMES` entry; mutation-like or ambiguous tools are
rejected. Requests are relayed through the normal application tool-call protocol;
the proxy never executes them. Results can reach A and B on subsequent turns.
Missing tools, rejected calls and failures are fail-open and do not block A. The
proxy cannot prove that a host implementation marked read-only is honest, so the
host must enforce that boundary too.

Knowledge notes carry topic, kind (`background`, `hypothesis`, or `question`),
insight/question, relevance and an application/check suggestion. Code supplies
`origin=model_generated_guidance`, source model, source call and observation time.
They are not execution evidence and cannot overwrite the user-pinned notebook.
Malformed optional notes can be dropped without discarding valid claim findings.
At most two contributions total are accepted across questions and knowledge notes.

The current run's completed snapshot loads automatically before A. When shared
memory is selected, the worker also attaches knowledge notes to the originating
journal entry. Other apps/runs on that same tenant/space can retrieve them with
that entry. Plain `questions` stay run-local; use `knowledge_notes` for reusable
peripheral questions. One shared snapshot (at most two notes) can be selected;
it is suppressed when a run-local knowledge snapshot is already delivered.

Retrieval reuses the existing journal queries and context budget. It prefers a
keyword-matched conversation, otherwise recent context. It does not separately
index the generated note's wording or guarantee semantic relevance. A must judge
relevance; the topic, provenance and age remain visible. Notes expire for injection
after the existing memory TTL (24 hours by default); archival storage persists.

Normal A requests never invoke or wait for a fresh B model response. This does
not promise unchanged wall-clock latency: database work, prompt tokens, shared
provider capacity and network variation still matter. The new notes reuse existing
queries/budgets; their extra JSON processing and database payload still have cost.
The visible director intentionally performs serial A/B calls.

Use `DUAL_LOBE_CONTEXT_ENRICHMENT_ENABLED=false` to suppress saved knowledge notes
and new enrichment output, without deleting records. Basic run framing/questions
remain; set `DUAL_LOBE_CONTEXT_MEMORY_ENABLED=false` to suppress that whole route.
Apply migration 0004 before updated code. No existing memory reset is required.

See [current functional assessment](FUNCTION_ASSESSMENT.md),
[reproducible evaluation](UX_ASSESSMENT_WITH_VS_WITHOUT.md), and
[executed validation](VALIDATION.md). Prompt intent is not measured efficacy.

## Optional host-tool information requests

When the connected application includes standard function definitions in the
request, B receives bounded copies of those definitions. B may request at most
two information-gathering calls by name and JSON arguments. The proxy validates
the name and exact schema, records an at-most-once reservation, and adds the
calls to A's ordinary response protocol. The application—not B and not the
proxy—executes them. Returned tool results must come back in the application's
next request, where A and B can see them. No definitions means no requests; an
unsupported, rejected, failed, or unavailable tool simply leaves the normal A
answer intact. Requests never count as evidence that work happened.
