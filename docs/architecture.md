# Durable research-to-PowerPoint architecture

This is a Python **Microsoft Agent Framework (MAF)** workflow with a terminal/JSON
harness, not an application UI. The application owns conversation persistence and
artifact integrity; MAF owns typed execution within each turn. See the
[README](../README.md) for verified setup, configuration and commands.

## Read the diagram

The editable [two-page source](../diagrams/workflow.drawio) separates two stories:

1. **Requests and durable state:** orchestrated research-to-PPT, direct PPT,
   local output and optional OneDrive delivery.
2. **Workers and restart:** explicit dispatch, shared Blob reference handoff,
   and restoration in a new process or container.

The [SVG preview](../diagrams/workflow.svg) is an original, geometry-matched
preview, **not a diagrams.net renderer export**. The `.drawio` file is the
editable source of truth. Blue denotes workflow/control, teal durable state,
purple immutable artifact references, slate the harness, and dashed amber
optional delivery. The overview draws the main export path; research-only and
answer actions are also committed by the same session harness.

## Execution paths: real graph, explicit agent modes

`workflow.py` builds a real typed `WorkflowBuilder` graph with an orchestrator
start executor and research, PPT and answer executors. Conditional edges use a
validated `RouteDecision`, rather than treating an LLM's prose as executable
instructions.

| Request | Graph behavior | Durable result |
| --- | --- | --- |
| Research and create a presentation | Orchestrator → research → PPT | Research JSON/Markdown, deck JSON and editable PPTX |
| Research only | Orchestrator → research → answer | Research JSON/Markdown and a user-visible summary |
| Create, add, remove or re-create a presentation | Orchestrator → PPT, with applicable prior artifact references | New deck JSON and PPTX; earlier artifacts are retained |
| Direct user-to-PPT request | The start executor sets a fixed PPT decision, skipping the **routing model call**; the same MAF graph still executes PPT | Deck JSON and editable PPTX without a new research run |
| Questions about earlier research, sources or slides; missing information | Orchestrator → answer | Reply/clarification and the completed conversation turn |

Direct PPT is a logical user-to-PPT path, not a separate non-MAF implementation.
It still restores conversation state and supplies the latest deck for revisions.
It does not automatically fetch web evidence or pass the latest research:
the direct decision sets `use_research=False`. Prefer an orchestrated request
when the new deck needs research.

**Live mode** creates three genuine MAF model agents: an orchestrator, a
researcher with public-web tools, and a presentation planner. The planner returns
a validated `DeckSpec`; trusted application code renders editable PowerPoint
shapes, text and notes with an illustrative Contoso theme. These are not official
Contoso brand assets.

**Demo mode** is a clearly marked deterministic, no-model, **no-web** exercise
of routing, rendering, artifacts and restoration. Its small request grammar is
not general natural-language understanding, and its "research" is explicitly
user-supplied demo content without fabricated sources. Live failures must not be
disguised as successful offline research.

## Contracts and artifact handoff

`models.py` is the shared, versioned contract boundary. Unknown fields and invalid
relationships are rejected.

| Contract | What crosses the boundary |
| --- | --- |
| `ArtifactRef` | A traversal-free relative storage `key`, SHA-256, byte `size`, `content_type`, filename and schema version |
| `SessionState` | UUID session ID, revision, **all committed user/assistant turns**, and latest research/deck/PPTX references |
| `ResearchDocument` | Title, summary, findings with source IDs, actual retrieved source records and limitations |
| `DeckSpec` | Title/subtitle, bounded editable content-slide specifications, citations and speaker notes |
| `WorkerJob` | Session ID, kind and prompt, plus `state_ref` and optional `research_ref`/`deck_ref` |
| `WorkerResult` | Matching session/kind and the expected output artifact manifest |

The researcher writes **both** `research.json` and `research.md`. JSON is the
machine contract; Markdown is the human-readable evidence review. The PPT worker
loads referenced research and the prior deck, checks their bytes and JSON schemas,
plans the deck, then writes `deck.json` and `presentation.pptx`. A renderer can
paginate dense content and add cover/source pages, so physical PPTX slide count
need not equal the number of `DeckSpec.slides`.

Immutable artifacts are stored under:

```text
sessions/{session_id}/artifacts/{artifact_uuid}/{filename}
```

The storage key is portable; a container-local filename, signed URL or in-memory
Python object is not. Storage configuration and credentials belong to the worker
environment, not the reference. Artifact bytes are not embedded in the transport
envelope. A reference is an integrity manifest, **not** a bearer credential or a
cryptographic proof of authorship: a trusted store, permissions and trusted
expected hashes are still necessary.

## Durable conversation is not a MAF checkpoint

For each invocation the harness:

1. Acquires the session lock/lease and loads the last committed `SessionState`,
   or initializes a new session.
2. Makes committed turns and appropriate artifact context available to agents.
   The current request is supplied separately. Workers receive an immutable
   snapshot reference, not permission to mutate the caller's history.
3. Runs a fresh MAF graph and requires exactly one typed `RunResult`.
4. Appends the user turn and resulting assistant reply, advances the revision,
   and commits the session.
5. Exports verified artifacts locally and, if explicitly requested, attempts
   OneDrive delivery.

This restores **the full application conversation history**, not merely a
summary or the latest prompt. It does not persist every internal model/tool
message, provider-owned thread or execution stack. The latest artifact pointers
restore structured research and deck context; older immutable artifacts remain
in storage but are not an automatically searchable catalog.

MAF checkpoints are a separate execution-recovery feature. This sample promises
restoration **between completed turns**, not arbitrary resumption of an
interrupted graph, model call, web fetch or render. Killing a worker does not
leave a resumable MAF checkpoint.

### Persistence choices and failure boundaries

- **Local store:** shared contract, session file lock, revision-successor check
  and same-directory atomic publication/replacement. It survives process
  restarts on retained storage. Different containers do not share it unless a
  suitable persistent filesystem is explicitly mounted and configured.
- **Azure Blob store:** a private existing container with identity-based
  access; immutable artifacts are uploaded without overwrite. A renewable,
  finite session lease coordinates cooperating callers, and session commits
  use revision checks plus ETag compare-and-swap for existing state.
- **Restart in another container:** use the same session UUID, storage account
  and container, compatible package/schema, and an authorized workload identity.
  No original process memory or local output directory is required to recover
  committed turns or fetch referenced artifacts.

Locking is per session and spans the turn; same-session work is serialized.
The Blob lease is on a lock blob, not a universal storage transaction:
all callers must honor the protocol, and ETags protect the state update from
conflicting writes. An expired/lost lease or revision conflict is a failure to
handle, not permission to overwrite history.

Artifact writes and the session commit are **not one atomic transaction**.
Failure before commit can leave unreferenced artifacts; retention and cleanup
are deployment concerns. Failure after commit during local export or OneDrive
upload does not undo the completed turn. Retrying the whole request can create
another turn/artifact version; there is no exactly-once request deduplication.
Recover/export committed artifacts rather than assuming every delivery failure
requires regeneration.

## Separate workers: control plane versus data plane

`WorkerDispatcher` is the control-plane boundary. The sample offers:

- **In-process dispatch:** the worker reads the same stored job/reference
  contracts, even though no process boundary is crossed.
- **Fixed subprocess dispatch:** the caller stores `job.json` and invokes the
  installed worker entrypoint with a small reference envelope. The child process
  has its own memory and receives storage/model settings from its environment.
  The caller enforces a timeout, checks completion/session/kind/output manifest,
  and verifies returned artifact bytes.

The worker accepts an object containing `job: ArtifactRef`, either directly,
inside an `input` object, or as a JSON object encoded in the `input` string. The
job reference must load validated `WorkerJob` JSON belonging to the same session.
A response envelope carries a response ID, completion status and typed result
metadata. Stdout is the machine-readable envelope, not a substitute artifact;
diagnostics go to stderr.

**Separate containers are an extension boundary, not an already-provisioned
hosting service.** Implement a dispatch adapter/transport that carries the same
envelopes to a fixed worker image and returns correlated results. Provide
authentication, admission limits, timeout/cancellation behavior, retry and
duplicate-work policy, and matching storage configuration. Research and PPT can
then execute in distinct containers without sharing a filesystem.

Azure Blob is the **data plane** for history, snapshots and artifact bytes.
It does not invoke workers, start containers, select images or schedule a
workflow. MAF graph edges do not themselves supply a remote container transport.
This repository does not provision a queue, container hosting, autoscaling or
distributed orchestration service.

## Guidance for research consumers

Treat research as bounded evidence, not an assertion that every generated
sentence is correct:

1. Validate the `ArtifactRef`, session ownership, size and SHA-256 before parsing
   `ResearchDocument`. Reject schema failures or unknown source IDs instead of
   silently substituting generated text.
2. Review `research.md` and the JSON `limitations`. Examine each finding's
   `source_ids`, the corresponding URL, retrieval time and actual excerpt.
   Citation consistency does not prove that an excerpt supports a claim.
3. Keep source IDs and complete source records with the findings when building
   slides. When combining research/decks, avoid reusing an ID for different
   evidence. The live planner validates source records against supplied
   evidence; it must not invent replacement citations.
4. Treat page excerpts, persisted history and artifact content as untrusted
   data, not instructions to reveal secrets or execute code. Validate downstream
   use rather than relying only on model prompting.
5. Refresh evidence when recency matters; restoring a session reuses saved
   evidence and does not automatically re-fetch its sources. Identify
   user-provided statements, uncited analysis and offline-demo content clearly.

The default search is bounded Wikipedia search, not unrestricted web search.
The fetch tool supports configured exact public HTTPS hosts and optional
configured evidence URLs. It rejects credentials/custom ports/redirects and
non-public destinations, connects to a checked public address, and limits bytes,
time, source count and retained excerpt length. Paywalls, JavaScript-only pages,
PDFs, unavailable sources and incomplete excerpt coverage remain limitations.
Expanding the allowlist requires review; a production deployment should also
enforce outbound network policy. These controls reduce exposure but do not make
web content or LLM judgments inherently trustworthy.

## Local output and optional OneDrive

The durable store remains authoritative. The CLI exports actual verified bytes
into a local output tree using an artifact UUID to separate versions. The
presentation is editable PPTX, not a screenshot or fake stdout payload.
Research Markdown and both structured JSON contracts can be reviewed locally.
The export operation can retrieve latest committed research JSON or PPTX after
a restart; consult the README for its supported selectors.

OneDrive is an **opt-in delegated Microsoft Graph upload** of the verified PPTX
bytes also exported locally. It requires a suitable delegated access token,
`Files.ReadWrite` permission and a usable user drive. It is separate from Azure
Storage managed identity and is not guaranteed merely because Blob or live model
authentication succeeds. The sample uses a bounded simple upload; it does not
provide token acquisition/refresh, background sync or resumable upload sessions.
An upload receipt is a delivery result, not conversation persistence.

## Hosted-agent pattern inspiration and deliberate differences

The requested reference is
[`foundry-hosted-agent-profiled/server_host.py`](https://github.com/hoopdad/foundry-hosted-agent-profiled/blob/main/server_host.py)
(reviewed 2026-10-08). This sample is an original implementation that borrows
architectural ideas, **not downloaded executable source**:

| Useful pattern in the reference | Application here and safety boundary |
| --- | --- |
| Managed identity for Blob access | Identity comes from trusted deployment configuration; per-request payloads cannot choose credentials or arbitrary Blob endpoints. Local development can use the configured default credential chain. |
| Blob input/output handoff | Relative immutable references replace mutable, caller-supplied Blob URLs; schemas, ownership, size and hashes are checked. |
| Isolated working files | Local persistence stages files beside their destination for atomic publication; rendering uses bounded trusted code/in-memory buffers. Transient files are implementation details, never inter-container references. |
| Direct/object/JSON-string invocation wrappers | Parse the supported wrapper shape, then validate the job reference and job schema; reject malformed or extra inner fields. |
| Response IDs and metadata | Return explicit completion status plus session/kind-correlated typed artifact metadata; do not confuse a successful host response with a validated worker result. |
| `perf_counter` profiling and URL query redaction | Azure HTTP timing logs contain method, redacted URL, status/outcome and elapsed time on stderr. Queries, fragments and URL credentials are stripped; bodies/tokens are not logged. Paths can still be sensitive and need appropriate log access/retention. |

Deliberately excluded: executing Python downloaded from a script blob, logging
raw request/response bodies, trusting unchecked URLs, and substituting stdout
when a requested file was not produced. Validation failures and timeouts are
explicit; broad exception handling must not turn failure into a completed
artifact. Lease-maintenance failures must abort safe persistence rather than
being swallowed. Hosted response shape compatibility does not mean the sample
ships a Foundry HTTP server or a container deployment.

## Strengths, limits and ownership

**Strengths:** real typed MAF execution; a genuine live-agent path; explicit
offline behavior; portable, integrity-checked handoff; complete committed
turn restoration; editable decks with citations/notes; concurrent-session
isolation; and optional delivery without making cloud credentials mandatory for
the local demonstration.

**Limits:** no UI, remote hosting/transport provisioning, exactly-once semantics,
in-flight graph recovery or automatic artifact garbage collection. Full history
can eventually exceed a model's context window: persistence does not create
unlimited model memory. Live model output and web availability remain
nondeterministic. Integrity checks are not malware screening, user authorization
or factual verification. Production needs identity/RBAC review, operational
monitoring, egress policy, retention, request limits and human review of research
and slide content.

### Validation and critique outcome

The final offline suite passed **197 tests**, with one Windows symlink-privilege
skip. Ruff passed; the wheel and source distribution built successfully.
The editable diagram passed both structural and strict layout validation.
Tests include the actual MAF runtime and a genuine model-agent/tool loop against
a mocked OpenAI transport, not just a hand-written imitation of workflow calls.
Fresh-process CLI tests exercise restoration, research handoff and revisions;
Azure/Graph authentication and network boundaries are mocked, not live tenant
certification.

Fresh-eyes artifact review found an oversized-deck page-number failure and
ambiguous reference continuation headings. The renderer now handles three-digit
page counts and labels reference parts explicitly. A separate critique found
unbounded repeated citation IDs: the shared schemas now reject duplicates and
limit each finding/slide to the twelve possible sources before rendering.

Native Office rendering was unavailable. Font-aware Pillow projections of the
actual PPTX and OOXML checks verified sample contrast, text bounds and footers;
these are approximations, not a claim of pixel-identical Office rendering.
Worst-case valid input can still expand into many physical slides. Human review
should shorten dense research and lengthy references rather than interpreting
schema validity as proof of a concise, presentation-ready narrative.

| Area | Owning paths |
| --- | --- |
| User harness and delivery | `src/contoso_agents/cli.py`, `onedrive.py` |
| Typed graph and worker dispatch | `src/contoso_agents/workflow.py`, `workers.py` |
| Live/demo agent behavior and bounded web evidence | `src/contoso_agents/agents.py`, `web.py` |
| Versioned contracts, state, artifact integrity and timing | `src/contoso_agents/models.py`, `storage.py`, `telemetry.py` |
| Editable slides and readable research | `src/contoso_agents/render.py` |
| Consumer setup and validated commands | `README.md` |
| Architecture and editable visual | `docs/architecture.md`, `diagrams/workflow.drawio`, `diagrams/workflow.svg` |
