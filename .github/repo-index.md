# Repository index

Freshness: baseline `5f85f6f`, refreshed for the durable workflow additions on
`hoopdad-durable-research-presentations`. Originally only README.md existed.

## Areas and boundaries

- `src/contoso_agents/models.py`: validated conversation, research, deck and artifact contracts.
- `src/contoso_agents/storage.py`: local/Azure Blob immutable artifacts and durable sessions.
- `src/contoso_agents/telemetry.py`: body-free, credential-redacted Azure HTTP timing.
- `src/contoso_agents/agents.py`, `web.py`, `render.py`: model/demo agents, web evidence and PPT rendering.
- `src/contoso_agents/workflow.py`, `workers.py`: MAF typed graph and reference-only worker handoff.
- `src/contoso_agents/cli.py`: user harness; no UI.
- `src/contoso_agents/onedrive.py`: optional delegated Graph delivery, not token acquisition.
- `tests/`: unit and integration coverage, including fresh-process session restoration.
- `README.md`, `docs/architecture.md`, `diagrams/`: consumer guide, critique and editable architecture.

## Dependency direction

CLI -> workflow -> workers/agents -> storage and renderer. Models are shared contracts.
Worker jobs carry immutable storage references, never container-local paths.
Conversation history is application state, separate from MAF execution checkpoints.
Typed conditional graph: orchestrator -> research/PPT/answer; research -> PPT/answer.
Demo never makes model/web calls; live constructs three MAF agents with OpenAI/Azure OpenAI.
The isolated-process dispatcher is runnable; remote container transport is an extension point.

## Navigation and conventions

Start with README for setup and commands; pyproject.toml owns dependencies.
Use Python 3.11+, Pydantic schemas, explicit errors, and isolated `.venv`.
MAF core 1.21.0 and OpenAI provider 1.14.4 are independently pinned in pyproject.toml.
`python -m contoso_agents` is the package entry point; the installed script is `contoso-agents`.
Test/lint/build: `.venv\Scripts\python.exe -m pytest -q`, `-m ruff check .`, `-m build`.
Keep demo behavior distinct from live model/web behavior. Never log prompts or credentials.
Added paths considered: package modules, test modules, pyproject.toml, .env.example,
.gitignore, docs/architecture.md, diagrams/workflow.drawio and its SVG preview.
No existing files were deleted.
