# Repository index

Freshness: baseline `bd33eef`, covering the Foundry connection, multi-turn HTTP API,
and trusted citation handoff on `hoopdad-durable-research-presentations`.

## Areas and boundaries

- `src/contoso_agents/models.py`: validated conversation, research, deck and artifact contracts.
- `src/contoso_agents/storage.py`: local/Azure Blob immutable artifacts and durable sessions.
- `src/contoso_agents/telemetry.py`: body-free, credential-redacted Azure HTTP timing.
- `src/contoso_agents/agents/__init__.py`: Foundry/legacy model client, three live MAF agents and offline demo.
- `src/contoso_agents/agents/research/`: bounded public-web evidence helpers.
- `src/contoso_agents/agents/pptx/`: deterministic editable PPT rendering and research Markdown.
- `src/contoso_agents/workflow.py`, `workers.py`: MAF typed graph and reference-only worker handoff.
- `src/contoso_agents/api.py`, `cli.py`: local HTTP/terminal harnesses; both load cwd `.env`, environment wins.
- `src/contoso_agents/onedrive.py`: optional delegated Graph delivery, not token acquisition.
- `tests/`: unit and integration coverage, including fresh-process session restoration.
- `README.md`, `docs/architecture.md`, `diagrams/`: consumer guide, critique and editable architecture.

## Dependency direction

CLI/API -> workflow -> workers/agents -> storage and renderer. Models are shared contracts.
Worker jobs carry immutable storage references, never container-local paths.
Conversation history is application state, separate from MAF execution checkpoints.
Typed conditional graph: orchestrator -> research/PPT/answer; research -> PPT/answer.
Demo never makes model/web calls; live constructs three MAF agents using a Foundry
OpenAI v1 endpoint/deployment with async AzureCliCredential (`az login` locally).
Optional legacy OpenAI/Azure OpenAI configuration remains supported.
Live PPT model output is a content/citation-ID draft. Python attaches exact ledger
records (used sources plus previous-deck sources); unknown IDs/metadata extras fail.
The isolated-process dispatcher is runnable; remote container transport is an extension point.

## Navigation and conventions

Start with README for setup and commands; pyproject.toml owns dependencies.
Teaching sample, not production: keep related sequential logic together rather
than adding settings/service frameworks. Helpers belong under their agent area.
Class docstrings begin with one sentence explaining their role in the workflow.
Use Python 3.11+, Pydantic schemas, explicit errors, and isolated `.venv`.
MAF core 1.21.0 and OpenAI provider 1.14.4 are independently pinned in pyproject.toml.
`python -m contoso_agents` is the package entry point; the installed script is `contoso-agents`.
`python -m contoso_agents.api` serves loopback port 8000, with `/docs` for request schemas.
POST `/sessions` starts a turn; POST `/sessions/{id}/turns` continues committed history.
GET session history includes download URLs; downloads only expose committed artifact refs.
API defaults live; CLI without `.env` retains demo default. Use explicit demo for offline work.
Test/lint/build: `.venv\Scripts\python.exe -m pytest -q`, `-m ruff check .`, `-m build`.
Keep demo behavior distinct from live model/web behavior. Never log prompts or credentials.
Foundry settings: `AZURE_AI_FOUNDRY_ENDPOINT` (full HTTPS `/openai/v1/` URL) and
`AZURE_AI_MODEL_DEPLOYMENT_NAME`; live errors never fall back to demo.
Configuration/import tests: `tests/test_agents.py`, `test_cli.py`; HTTP/history/
download/error tests: `tests/test_api.py`; prior workflow/storage/worker tests remain.
Added paths considered: `api.py`, `tests/test_api.py`, and the three agents package
`__init__.py` files. Removed paths: top-level `agents.py`, `web.py`, `render.py`
(logic relocated, not discarded). Updated dependencies: FastAPI, uvicorn, python-dotenv.
