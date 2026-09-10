# A minimal, nonblocking Lobe B

This report records the v0.2 design and research. The v0.3 implementation separates
durable broadening memory, direct claim findings, and the standing monitoring
instruction. See [the current setup and test](THREE_PATH_SETUP.md) for current
storage, timing, settings and limitations; the research below is not a new
evaluation of v0.3's model effectiveness.

## Recommendation

Use B as a bounded observer with two responsibilities: widen the problem framing
and identify material inconsistencies in the available record. Keep A responsible
for execution and for its final answer. B should produce one small suggestion
packet, not a competing answer, a verdict, a tool plan, or a new agent workflow.

The implementation in this repository follows that design. It combines a short,
honest observation reminder with a single asynchronous review. The reminder can
influence the current answer; a completed B review can influence a later call.
Neither mechanism certifies truth. The design deliberately accepts that an
unsupported answer can reach the client before B notices it.

This is the unavoidable trade-off of “never hold the response”: preventing every
false statement before delivery would require some form of pre-delivery decision,
while this architecture explicitly avoids one. It can discourage unsupported
claims, detect some after generation, and help subsequent work recover. It cannot
provide a zero-error, zero-overhead, lossless, adversary-proof guarantee.

## Research findings

### Broadening the frame

Step-Back Prompting asks a model to derive a more general abstraction before
solving a detailed problem. Zheng and colleagues report improvements on selected
reasoning and knowledge benchmarks with the models they tested. Their error
analysis also identifies continuing reasoning limitations. This supports asking
about the objective and governing prerequisite before proposing another retry;
it does not establish performance on arbitrary software-agent workflows.[^1]

The implementation is an adaptation, not a reproduction of that paper. B receives
the original run objective as an anchor, alongside the current conversation and
latest output. It asks which detail has become an unjustified premise, whether
the chosen tactic is still necessary, and what different explanation fits the
same failure. It returns only the questions likely to change the next decision.

“Open sesame” is interpreted here as the search for a missing enabling condition:
the correct input, state, dependency, contract or abstraction. The desired result
is a discriminating check, not a longer list of speculative solutions. A denied
permission is a boundary to report or clarify, not a puzzle to defeat.

### Self-correction has conditional value

Huang and colleagues found that intrinsic reasoning self-correction, without
external feedback, often failed to improve the models and tasks they evaluated
and sometimes reduced performance. They also stress fair prompting and
inference-cost baselines. The practical implication is not “never review,” but
“do not assume an extra model pass is evidence.” Existing action results are
more useful inputs than another unsupported statement of confidence.[^2]

There is relevant counterevidence. Work on key-condition verification reports
improvements from a more specific verify-then-correct procedure. A later study
reports self-correction of synthetically perturbed reasoning across several
models. Those findings involve different tasks and experimental interventions;
they do not establish universal self-correction or a reliable detector of
intentional deception in this gateway.[^3][^4]

The selected approach is therefore constrained review, not automatic correction:
B can point to a supplied inconsistency or suggest a useful question. A keeps
the original goal and permissions and may reject an irrelevant or outdated note.
A model disagreement is not enough to overwrite task state.

### Verification questions and independence

Chain-of-Verification generates questions to check a draft and studies ways to
answer those questions independently, reducing contamination from the draft.
The authors report reduced hallucinations on their evaluated tasks. The factored
variants separate answer contexts; that separation is more substantial than
placing several questions in one prompt.[^5]

This repository does not claim to implement full CoVe. Its single-call budget
cannot provide the same independent-question procedure. It borrows only the
discipline of making a check specific. B asks which exact result would support a
material claim and compares the answer with observations already supplied.
The gain, if any, must be measured against the actual chosen A/B models.

### Being watched is not a truth guarantee

Anthropic's alignment-faking experiments demonstrate that stated monitoring and
training conditions can affect model behavior under a particular experimental
setup. The authors explicitly describe caveats and do not establish that
dangerous behavior necessarily emerges. These results do not show that a generic
“you are being watched” message prevents fabrication.[^6]

The reminder is consequently modest: available responses and action results
*may* be checked asynchronously. It asks A to distinguish planned, attempted,
observed and confirmed work. It never claims B is always available, that every
statement has been inspected, or that a clean review proves honesty.

The reminder is independently switchable for evaluation. It should not threaten
punishment, imply omniscient surveillance, or reward confident claims of success.
An eventual evaluation should compare reminder-only against reminder-plus-B;
otherwise an improvement could be incorrectly attributed to the observer.

### Why wording alone is insufficient

The same surface wording can describe a correct result, an honest mistake,
an unsupported inference, a fabricated receipt, or a quotation. Fluency and
certainty are therefore useful attention cues only when compared with the
available context. This is an analytical limitation of the available signal,
not a claim that all textual detection research is impossible.

Research on internal-state lie detection is not directly deployable here.
For example, Bürger and colleagues use model activation representations and
trained classifiers, not just a second hosted model reading response style.
Reported performance in those experiments should not be transferred to this
API-only observer.[^7]

The implementation names observable concerns rather than inferring intent.
An unsupported claim is not automatically false. A contradiction classification
is still B's interpretation; exact quote validation proves only that the quoted
strings appeared in the bounded input, not that the interpretation is correct.

## The exact questions B asks

The full prompt is versioned in [prompts.py](../src/dual_lobe/b/prompts.py).
The following table maps its questions to the decision they are meant to improve.

| Question | Purpose | Useful output |
|---|---|---|
| What outcome matters, independent of this tactic? | Recover the objective from implementation fixation | A simpler route preserving requirements |
| Which premise or missing prerequisite causes repeated failure? | Identify the enabling condition | One candidate condition explicitly labeled uncertain |
| What other explanation fits the same observations? | Prevent premature closure | One materially different hypothesis |
| What smallest existing-capability check separates them? | Turn speculation into useful progress | A check whose two possible outcomes change the next action |
| Has the method changed, or only its wording? | Detect repeated dead ends | Explain what new information another attempt would gain |
| What independent portion remains achievable? | Avoid all-or-nothing stalls | A bounded useful action inside existing authorization |

These are internal review instructions, not six questions sent to the user each
turn. B returns at most two questions and one next step. An empty packet is valid.
There is no requirement to manufacture a concern to demonstrate activity.

For integrity, B examines material claims:

| Observed situation | Appropriate assessment | Inappropriate assessment |
|---|---|---|
| Output claims tests passed; supplied result shows failures | Quote both and flag a possible contradiction | Declare intentional lying |
| Output claims an artifact was created; no result is available | Identify missing evidence and label unsupported | Assert the file does not exist |
| A requested a tool but no result has arrived | Treat execution as unconfirmed | Convert a request into a receipt |
| Earlier failure is followed by a matching successful result | Consider the newer result; retire the old concern | Repeat the obsolete failure indefinitely |
| Strong certainty appears after unresolved failure | Compare the shift with the earlier basis | Treat confident tone itself as deception |
| A sentence is a plan or hypothetical example | Preserve its context | Flag it as falsely claimed completed work |

## Inputs, state and authority

B receives a bounded snapshot rather than an independently researched world
model. It includes the original run objective, caller messages, A output,
recent relevant events, a small prior-review excerpt, the host tool definitions,
and the host-supplied artifact inventory. The context projection preserves tool
requests and tool results. It does not request or store hidden reasoning fields.

The context limiter retains both the beginning and the end, with an explicit
omission marker. This reduces the risk of discarding the original goal while
keeping only the latest failed tactic. It does not make the snapshot complete.
If important evidence was omitted or never supplied, B may miss a problem or
mistakenly regard a claim as unsupported.

Authority is deliberately narrow:

- Proxy-generated events establish only the request/response facts the proxy
  observed. A successful provider call does not establish task success.
- Caller tool messages and ingested events are reported results, not independently
  authenticated execution receipts. External events cannot impersonate internal
  event kinds; the gateway wraps them as client-reported data.
- Prior B notes are fallible suggestions, not new evidence. They are not included
  as independent events when constructing the next review.
- B does not execute tools itself or grant permission. It can request any
  advertised host tool, including the complete specific artifact needed to check
  an action claim; the connected runtime executes the call and returns its real
  result through the ordinary A tool loop. The gateway does not turn B severity
  into an execution gate or call a request a completed check.

B output uses a small strict schema: an objective, up to two questions, one next
step, and up to three concerns. Each concern includes a permitted signal,
an exact output quote, a basis quote where applicable, a specific reason and a
short suggestion. Extra keys, wrong types, excessive lengths and invented quoted
strings fail validation.

Generated B text enters A's conversation as a clearly labeled user-role advisory
message before the original conversational turns. Only the fixed observation
reminder is system-role text. This avoids promoting B's generated suggestions to
system authority and preserves assistant-tool/result adjacency. Prompt separation
reduces one authority-confusion risk; it is not a complete prompt-injection defense.

## Timing and failure behavior

A begins without a B model call. Authentication, admission checks and run metadata
remain gateway dependencies. An optional read of an existing note has a short
deadline and fails open. There is no database transaction held for the duration
of A generation.

The transport uses HTTPX's asynchronous client with a process-level connection
pool and managed streaming responses. This follows HTTPX's guidance to reuse a
client for pooling and close streaming responses. A complete SSE event is relayed
without collecting the whole answer first.[^8]

The gateway preserves tool-call fragments, choice indices, roles, IDs, timestamps,
finish reasons and usage fields. It does not invent a terminal success if the
stream fails. Before streaming begins, provider failures can be reported as HTTP
errors; once streaming has started, the body carries a structured error instead
of a fabricated successful completion.

Observation persistence is attached to response delivery as a background task.
Starlette documents that such tasks run after the response is sent. They are
in-process tasks, not a durable queue by themselves.[^9] Therefore, a crash,
disconnect cancellation or database outage can lose the observation before it
reaches the outbox. This is an explicit reliability trade-off, not “exactly once”
or “lossless” monitoring.

Once committed, the existing database outbox feeds B jobs. The worker serializes
per-run work, skips superseded jobs, respects a process-local call budget and
cooldown, and rejects expired observations. State and job completion commit in
the same worker transaction. A review can arrive too late for the next A call.
Skipping intermediate reviews prioritizes recent work but reduces coverage.

| Failure or condition | Implemented behavior |
|---|---|
| No current B note | A proceeds with the reminder only |
| Optional note read times out | A proceeds; response header reports unavailable state |
| Invalid JSON or unsupported review schema | Degraded review state; no generated note injected |
| Fabricated quoted evidence | Review rejected rather than accepted as clean |
| B provider error or timeout | Degraded state; no model retry loop |
| Note is old or from another floor/attempt | Do not inject it |
| Newer job supersedes an older observation | Skip old work |
| Cooldown or B budget reached | Skip review without holding A |
| Post-response persistence fails | Log the lost observation; A's delivered answer is unchanged |
| Core authentication/database fails | Normal gateway failure, not bypassed authorization |

Freshness uses the observation time, not merely review completion time. A slow
review must not make old evidence appear current. The state endpoint marks a
previously reviewed but no-longer-applicable note stale.

## Simplicity and deployment decisions

The active stack consists of the gateway, a B worker, and Postgres. A one-shot
initialization service runs migrations and bootstrap. Redis is removed because
the request path was already using local admission counters. LiteLLM is removed
because the supported upstream contract is deliberately restricted to Chat
Completions; an existing HTTP client can perform that job directly.

The worker adds no search API, file verifier, browser, retrieval store, embeddings,
multi-model vote, agent framework or reflective retry loop. This reduces moving
parts, not the inherent uncertainty of a model review. It also narrows provider
compatibility: provider-specific dialect translation and the Responses API are
outside this implementation.

Both lobe connections remain separately configurable. Sharing a model or provider
is allowed but may create correlated errors and quota contention. A different B
model may provide another perspective, but model diversity is not independent
evidence and has not been benchmarked here.

The optional note read defaults to 25 milliseconds; that is a cancellation
deadline, not a measured end-to-end latency bound. Database connection cleanup,
network traffic, token processing and contention still cost time. B's requested
1400-token output cap, 20-second operation deadline, two-worker concurrency and
20-call-per-minute tenant budget are engineering defaults, not research-derived
optimal thresholds.

Admission and B rate counters are local to one process. Multiple gateway or B
worker processes multiply these limits. This is a documented local deployment
profile, not a globally coordinated multi-tenant quota system.

## Repository corrections beyond the B prompt

Several surrounding defects would undermine the simplified design. The former
stream path collected the answer before sending it, and the old adapter iterated
a synchronous stream inside async execution. The revised transport and response
path eliminate that whole-answer buffering and explicitly report incomplete
streams.

Unknown model aliases no longer silently route to A. Unsupported request fields
are rejected instead of being dropped. Selected common fields previously missing
from normalization, including penalties and parallel tool-call options, are now
forwarded. Image/audio inputs remain disabled.

Tenant context now uses a transaction-local setting rather than setting a pooled
connection's tenant and committing before later queries. PostgreSQL documents the
different transaction lifetime of local settings.[^10] The gateway also requires
an explicit tenant-role URL instead of silently falling back to the owner URL.
These changes require real database integration validation; mock tests cannot
prove row-level security.

The automatic verification RPC is retired with HTTP 410. Checking a path inside
the observer container was not proof of a worker-side artifact and exposed an
unnecessary privileged mutation path. Historical claim/evidence tables remain;
the active B worker requests evidence through the connected host tool loop and
records a model-selected assessment after the result; it never labels that
assessment independent proof or guarantees that a hidden artifact exists.

Compose now actually passes provider settings to both processes, runs
initialization only once, and binds host ports to loopback. Bootstrap requires an
explicit secret, does not log raw keys and is idempotent. Existing keys are not
silently removed or changed. Production role permissions, database passwords,
retention policies, network ingress and dependency security still need a
deployment-specific assessment.

## Alternatives considered

| Alternative | Advantage | Reason not selected as the default |
|---|---|---|
| Reminder only | Lowest complexity; no B model cost | Cannot compare the completed output with the record after generation |
| Generic “find mistakes” critic | Simple prompt | Encourages broad speculation and ungrounded disagreement |
| Full independent verification chain | More separated checks | Additional model calls and orchestration exceed the simplicity budget |
| Search every factual claim | May add external evidence | Adds tools, latency, costs and new failure modes |
| File checking inside B | Deterministic local observation | Wrong environment does not establish A's artifact state |
| Inline B approval before delivery | Can influence the same response before display | Conflicts with the nonblocking requirement |
| Activation-based lie detector | Different source of signal | Requires access and training outside the hosted text API contract |

The selected design is deliberately between reminder-only and an elaborate
verifier. It can be switched to observation mode for evaluation without making
B part of the execution control plane.

## Evaluation before stronger claims

There are two separate questions: whether the software obeys its timing and
failure contracts, and whether the configured model actually improves outcomes.
The deterministic tests address the former. They do not measure fabrication
recall, false accusations, blocker recovery or real-provider latency.

For model evaluation, replay the same representative tasks across three arms:
A without the reminder or B, A with the reminder only, and A with the reminder
plus asynchronous B. Keep A's model, tools, task, budgets and initial instructions
constant. Measure token cost and latency in addition to task outcome; do not
compare differently prompted A baselines and attribute every change to B.

The case set should include real failed executions, matching successful receipts,
missing and truncated evidence, proposals that are not completion claims, changed
requirements, resolved errors, prompt injection in retrieved text and repeated
attempts with no new information. Include confident correct answers and hesitant
wrong ones to expose reliance on style.

Have humans distinguish unsupported claims from demonstrated contradictions and
judge whether a proposed next step was useful and authorized. Record false
positives, missed material contradictions, unnecessary checks, abandoned correct
approaches, blocker recovery, time-to-first-token, total latency and B call spend.
Separate quote-validation acceptance from semantic correctness.

No universal pass threshold is specified here: acceptable trade-offs depend on
the workload and consequences. Before stronger reliability claims, run the
Postgres integration suite, actual Compose startup, real provider streaming/tool
tests, concurrent runs and failure injection. Then evaluate prompt effectiveness
on held-out tasks rather than tuning and reporting on the same examples.

## Sources

[^1]: Huaixiu Steven Zheng et al. [Take a Step Back: Evoking Reasoning via Abstraction in Large Language Models](https://arxiv.org/html/2310.06117v2). Submitted October 2023; version 2, March 2024. Primary research; task- and model-specific results.
[^2]: Jie Huang et al. [Large Language Models Cannot Self-Correct Reasoning Yet](https://arxiv.org/html/2310.01798v2). ICLR 2024; version 2, March 2024. Primary research on intrinsic reasoning self-correction and evaluation design.
[^3]: Zhenyu Wu et al. [Large Language Models Can Self-Correct with Key Condition Verification](https://arxiv.org/abs/2405.14092). Submitted May 2024. Primary research on targeted verification; earlier version titled “with Minimal Effort.”
[^4]: Sam Silver et al. [Language Models can perform Single-Utterance Self-Correction of Perturbed Reasoning](https://arxiv.org/abs/2506.15894). June 2025. Primary research preprint involving synthetic reasoning perturbations.
[^5]: Shehzaad Dhuliawala et al. [Chain-of-Verification Reduces Hallucination in Large Language Models](https://arxiv.org/html/2309.11495v2). September 2023. Primary research; independent-question variants are not reproduced by this single-call observer.
[^6]: Anthropic and Redwood Research. [Alignment faking in large language models](https://www.anthropic.com/research/alignment-faking). December 18, 2024. Primary research overview with experimental caveats and full-paper link.
[^7]: Lennart Bürger, Fred A. Hamprecht and Boaz Nadler. [Truth is Universal: Robust Detection of Lies in LLMs](https://arxiv.org/abs/2407.12831). July 2024. Primary research using internal activations, not this proxy's text-only signal.
[^8]: HTTPX. [Async Support](https://www.python-httpx.org/async/) and [Timeouts](https://www.python-httpx.org/advanced/timeouts/). Official documentation, accessed September 2026.
[^9]: Starlette. [Background Tasks](https://starlette.dev/background/). Official documentation, accessed September 2026.
[^10]: PostgreSQL. [SET, PostgreSQL 18 documentation](https://www.postgresql.org/docs/18/sql-set.html). Official documentation, accessed September 2026.
