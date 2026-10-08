# Durable research and Contoso presentation workflow

A runnable **Microsoft Agent Framework (MAF) Python workflow** with an orchestrator,
a web researcher, and a PowerPoint agent. A small CLI is the user/backend harness;
there is deliberately no UI.

Two entry paths share the same typed MAF graph:

| Use case | Path | Result |
| --- | --- | --- |
| Direct presentation | User request -> PPT executor, bypassing model routing | Editable Contoso-themed `.pptx`, deck JSON, local file paths |
| Orchestrated conversation | Orchestrator -> research, PPT, research + PPT, or clarification | Research JSON/Markdown and/or PPT, with durable conversation history |

The orchestrator knows each worker's capabilities. Workers communicate through
**immutable artifact references**, not container-local paths. Local disk works for
development; Azure Blob Storage provides the shared data plane for separate
containers. The CLI restores the entire committed conversation when `--session`
is reused, including per-turn artifact references and the latest research/deck.

![Workflow and reference-only handoff](diagrams/workflow.svg)

The editable diagram is [diagrams/workflow.drawio](diagrams/workflow.drawio).
See [the architecture and design critique](docs/architecture.md) for the
cross-container boundary, adopted hosted-agent patterns, and production gaps.

## Quick start: no model or cloud credentials needed

Use Python 3.11 or later. The sample pins `agent-framework-core==1.21.0` and the
independently versioned `agent-framework-openai==1.14.4` because workflow and
agent APIs evolve; it does not emulate MAF.

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --prefer-binary -e ".[dev]"

# Use case 1: supplied content directly to the PPT agent.
.\.venv\Scripts\python.exe -m contoso_agents invoke --agent-mode demo --direct-ppt `
  --prompt "Create a Contoso PPT about Azure networking. Include private endpoints and DNS."
```

The JSON response contains `session_id`, `action`, `revision`, immutable artifact
references, storage locations, and **absolute `local_path` values for generated
files**. The actual PowerPoint file is exported under `output`; the response is
not a claim that a file was generated without providing it.

`demo` is an **offline deterministic harness**: no model calls, no web access,
and no fabricated web evidence. It exercises the real MAF graph and actual PPT
generation. Use `live` for natural-language routing and web-backed research.
`.env.example` documents settings; the CLI reads environment variables and does
not automatically load `.env`.

### Calling the workflow from a Python backend

The CLI is a thin adapter over the same callable that a future backend can use:

```python
import asyncio
from uuid import uuid4

from contoso_agents.agents import create_agents
from contoso_agents.storage import create_store
from contoso_agents.workers import InProcessDispatcher
from contoso_agents.workflow import invoke

async def main():
    store = create_store()
    agents = create_agents("demo")  # "live" requires configured model credentials
    dispatcher = InProcessDispatcher(store, agents)
    session_id = str(uuid4())      # Return this to the caller; reuse it for later turns
    result = await invoke(
        store, agents, dispatcher, session_id,
        "Research Azure networking and create a Contoso PPT.",
    )
    print(result.model_dump_json(indent=2))
    # Artifact bytes are fetched with store.read_artifact(ref), not a worker-local path.

asyncio.run(main())
```

A backend must authorize session access and stream or export the verified
artifact bytes to its caller; an Azure Blob URI alone is not a public download
link. The CLI already performs local export and reports the resulting paths.

### Use case 2: research, create, then revise after a restart

```powershell
$first = .\.venv\Scripts\python.exe -m contoso_agents invoke --agent-mode demo `
  --worker-mode process `
  --prompt "Research Azure networking and create a Contoso PPT." | ConvertFrom-Json
$session = $first.session_id
$first.files | Select-Object local_path

# Each command is a NEW process. No prior in-memory agent is required.
.\.venv\Scripts\python.exe -m contoso_agents invoke --agent-mode demo `
  --worker-mode process --session $session --prompt "Add a slide about cost controls"
.\.venv\Scripts\python.exe -m contoso_agents invoke --agent-mode demo `
  --session $session --prompt "Remove cost controls"
.\.venv\Scripts\python.exe -m contoso_agents invoke --agent-mode demo `
  --session $session --prompt "Re-create the PPT"
.\.venv\Scripts\python.exe -m contoso_agents invoke --agent-mode demo `
  --session $session --prompt "What did we discuss?"

# Inspect all committed turns, or retrieve a generated artifact later.
.\.venv\Scripts\python.exe -m contoso_agents show-session --session $session
.\.venv\Scripts\python.exe -m contoso_agents export --session $session --kind ppt
.\.venv\Scripts\python.exe -m contoso_agents export --session $session --kind research
```

The demo supports this intentionally small vocabulary; it is not a substitute
for a language model. A clarification produces an answer without regenerating a
PPT. Revisions read the saved editable deck specification, generate a new PPT
artifact, and preserve older versions rather than overwriting them.

## Live agents and web resources

For OpenAI:

```powershell
$env:OPENAI_API_KEY = "<set securely in this process>"
$env:OPENAI_MODEL = "gpt-4.1-mini"
.\.venv\Scripts\python.exe -m contoso_agents invoke --agent-mode live `
  --prompt "Research Azure Private Link using Microsoft Learn and create a Contoso PPT for architects."
```

Alternatively configure `AZURE_OPENAI_ENDPOINT` and
`AZURE_OPENAI_CHAT_DEPLOYMENT_NAME` and authenticate with Azure identity. The
identity needs access to that Azure OpenAI deployment; a model's deployment
name is not necessarily its model name. See `agents.py` for the actual client
construction. Never commit API keys or bearer tokens.

The live orchestrator, researcher and PPT planner are separate MAF model agents.
Model calls receive conversation context; structured Pydantic outputs constrain
routing, findings, slides, and citations. The renderer is deterministic Python,
not model-generated executable code.

The researcher uses bounded public web tools and records retrieved evidence:
source ID, URL, title, retrieval timestamp, and excerpt. Web access is restricted
to allowed public domains; unsupported URLs and failed fetches are errors, not
silently invented research. This is a bounded research sample, **not a general
web crawler**. Treat web page content as untrusted data, not agent instructions.
Search uses Wikipedia by default. `CONTOSO_WEB_ALLOWED_HOSTS` is an exact,
comma-separated allowlist (defaults: `en.wikipedia.org`, `learn.microsoft.com`,
`docs.python.org`). `CONTOSO_WEB_URLS` can seed evidence as a JSON array or
comma/newline-separated HTTPS URLs, for example:

```powershell
$env:CONTOSO_WEB_URLS = '["https://learn.microsoft.com/en-us/azure/private-link/private-link-overview"]'
```

Fetches reject redirects, explicit ports, URL credentials and non-public
destinations; public DNS addresses are checked/pinned. Bytes, fetch duration,
tool requests, source count and excerpts are bounded. Widening the allowlist
is a deliberate configuration change, not something web content can request.

Research is saved in both `research.json` (machine contract) and `research.md`
(human review). The PPT worker reads the exact supplied research reference,
verifies its integrity, and uses its source IDs. Decks retain source citations
and a references appendix; a supplied-content-only deck must not pretend to
have web citations.

**For research consumers:** inspect the Markdown, source URLs, retrieval times
and limitations before sharing the deck. A valid source ID proves that the
source was retrieved, not that every inference is correct. Review factual
claims, relevance, freshness, requested edits and branding. The Contoso theme
is illustrative; no official Contoso logo/template was supplied.

## Shared Azure Storage and agents in separate containers

Create a **private** Blob container and grant the worker/orchestrator identities
appropriate container-scoped Storage Blob Data Contributor access. This sample
does not create resources, change permissions, or configure networking.

```powershell
$env:CONTOSO_STORE = "azure"
$env:AZURE_STORAGE_ACCOUNT_URL = "https://<account>.blob.core.windows.net"
$env:AZURE_STORAGE_CONTAINER = "contoso-workflow"
# DefaultAzureCredential supports a development login or an Azure-hosted identity.
# For a user-assigned identity, configure AZURE_CLIENT_ID as appropriate.
# Azure-hosted workers can explicitly select the managed identity credential:
# $env:CONTOSO_AZURE_CREDENTIAL = "managed_identity"

.\.venv\Scripts\python.exe -m contoso_agents invoke --agent-mode live `
  --worker-mode process `
  --prompt "Research Azure networking and create a Contoso PPT."
```

All participants must use the same storage account/container and deployed
version of the contracts. A reference carries `key`, `sha256`, `size`,
`content_type`, `filename`, and `schema_version`; it carries **no SAS token**.
Workers authenticate independently, download by key, verify size/hash/schema
and session ownership, then write their own immutable results.

The control flow is explicit:

1. The orchestrator stores a `WorkerJob` and a conversation snapshot.
2. The researcher receives only the job reference, reads its inputs, and writes
   research JSON/Markdown to shared Blob Storage.
3. Its result manifest returns research references to the orchestrator.
4. The PPT job includes that exact `research_ref`; the PPT worker downloads it
   even when its container has never seen the researcher's filesystem.
5. The successful turn atomically updates the session's conversation and latest
   references. The harness exports the PPT locally and returns its file path.

`--worker-mode process` exercises isolated worker processes on one machine.
`CONTOSO_WORKER_TIMEOUT_SECONDS` bounds each process invocation (default 120;
maximum 3600 seconds); timed-out/cancelled workers are terminated explicitly.
The `worker` command is also a fixed-code entry point for an externally deployed
worker. It accepts these invocation envelopes:

```json
{"job": {"schema_version": 1, "key": "...", "sha256": "...", "size": 123,
         "content_type": "application/json", "filename": "job.json"}}
```

The same payload may be inside `{"input": {...}}`, or `input` may be its JSON
string. Pass a real job reference emitted/stored by the workflow; ellipses above
are illustrative, not a runnable reference.

```powershell
.\.venv\Scripts\python.exe -m contoso_agents worker --agent-mode live --request $requestJson
```

The worker returns a correlated response envelope with a result manifest.
`WorkerDispatcher` is the extension point for HTTP, a queue, or a hosted-agent
invocation transport. **This sample does not deploy or automatically schedule
remote containers.** Blob Storage solves shared data, not dispatch, delivery
acknowledgments or queue semantics. A local store only spans containers if they
share a correctly configured persistent volume; use Blob for independent hosts.

### Durability and failure semantics

Application conversation persistence and MAF execution checkpoints are
different concerns. This sample persists **completed turns** and all their
artifact references. Restart a process/container, provide the same session ID,
and it loads that history and the prior deck before running a new MAF graph.
The model context is subject to the selected deployment's context limit;
unbounded history is not silently dropped.

Per-session locking and revision/ETag checks prevent concurrent turns from
losing each other's updates. Worker exceptions, schema/hash mismatches and
storage/auth failures fail the invocation; no dummy file or model-free result
is substituted in live mode. A failed turn leaves the last committed session
unchanged. Immutable artifacts written before a failure can remain orphaned.

There is **no automatic mid-turn checkpoint resume, exactly-once model call,
queue retry/idempotency, tenant authorization layer or orphan cleanup service**.
These are explicit production extensions, not capabilities inferred from the
use of MAF or Blob Storage.

## Bonus: OneDrive delivery

After local/shared-storage generation, `--onedrive` uploads the generated PPT
through Microsoft Graph. Supply an existing **delegated** Graph token with
`Files.ReadWrite`; `/me/drive` does not accept a managed-identity app-only token.
The sample deliberately does not implement sign-in, consent, or token storage.

```powershell
$env:ONEDRIVE_ACCESS_TOKEN = "<existing delegated token; do not commit>"
.\.venv\Scripts\python.exe -m contoso_agents invoke --agent-mode demo --direct-ppt `
  --onedrive --prompt "Create a Contoso PPT about Azure networking."
```

The response includes the OneDrive item ID and `web_url`, never a credential or
preauthenticated download URL. Upload failures are surfaced; the already
generated durable artifact can still be retrieved with `export`. An immutable
artifact ID in the uploaded filename prevents unrelated files being replaced.
The adapter uses [Graph simple upload](https://learn.microsoft.com/en-us/graph/api/driveitem-put-content?view=graph-rest-1.0);
larger files require an upload-session implementation.

## Build and validation

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build
```

Tests run offline: the **real MAF runtime**, generated PPT ZIP/content, durable
session restoration, isolated workers, artifact integrity, revision conflicts,
and mocked web/Azure/Graph boundaries. They do not prove a particular tenant's
RBAC, network access, model behavior, or OneDrive consent. Live cloud/model calls
require your credentials and an explicit run.

## Code map and authoritative references

| File | Responsibility |
| --- | --- |
| `models.py` | Versioned, validated session/research/deck/reference contracts |
| `workflow.py` | MAF typed executors, conditional edges and per-turn persistence |
| `agents.py` | Separate live MAF agents and clearly labeled offline demo |
| `web.py`, `render.py` | Bounded web evidence tools and editable PPT creation |
| `storage.py` | Local/Azure durable sessions and immutable artifacts |
| `workers.py` | Reference-only handoff, envelopes and fixed-code process workers |
| `cli.py`, `onedrive.py` | User harness, local file delivery, optional Graph upload |

- [MAF workflow concepts](https://learn.microsoft.com/en-us/agent-framework/concepts/workflows/?pivots=programming-language-python)
- [Workflow Builder and execution](https://learn.microsoft.com/en-us/agent-framework/concepts/workflows/builder-and-execution?pivots=programming-language-python)
- [Azure Blob Python overview](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-quickstart-blobs-python)
- [Hosted-agent reference patterns](https://github.com/hoopdad/foundry-hosted-agent-profiled/blob/main/server_host.py)

The hosted-agent reference informed the managed-identity/Blob boundary, typed
invocation envelope, temporary work and correlated timing pattern. This sample
does **not** download and execute scripts, log raw requests, or upload stdout
when a promised artifact is missing.
