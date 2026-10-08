"""Local teaching API: HTTP turns call the same durable MAF workflow as the CLI."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from agent_framework.exceptions import AgentFrameworkException
from azure.core.exceptions import AzureError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .agents import (
    AgentBackend,
    AgentConfigurationError,
    UnsupportedDemoRequest,
    WebResourceError,
    create_agents,
)
from .models import ArtifactRef, SessionState
from .storage import (
    ArtifactIntegrityError,
    ArtifactStore,
    LeaseLostError,
    SessionConflictError,
    create_store,
)
from .workers import InProcessDispatcher, ProcessDispatcher, WorkerDispatcher
from .workflow import invoke

logger = logging.getLogger(__name__)


class TurnRequest(BaseModel):
    """Validates the prompt and direct-PPT flag for the API's first or next conversation turn."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=8000)
    direct_ppt: bool = False

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Prompt must contain non-whitespace text")
        return value


def create_app(
    *,
    store: ArtifactStore | None = None,
    agents: AgentBackend | None = None,
    dispatcher: WorkerDispatcher | None = None,
) -> FastAPI:
    """Builds the local HTTP API over the durable workflow with injectable testing boundaries.

    Settings load when this factory runs, not when the module is imported.
    """
    load_dotenv(Path.cwd() / ".env", override=False)
    mode = os.getenv("CONTOSO_AGENT_MODE", "live").strip().lower()
    store = store if store is not None else create_store()
    if agents is None:
        agents = create_agents(mode)
    else:
        mode = "injected"
    if dispatcher is None:
        worker_mode = os.getenv("CONTOSO_WORKER_MODE", "inprocess").strip().lower()
        if worker_mode == "inprocess":
            dispatcher = InProcessDispatcher(store, agents)
        elif worker_mode == "process":
            if mode == "injected":
                raise AgentConfigurationError("Injected agents require an in-process dispatcher.")
            dispatcher = ProcessDispatcher(
                store, mode, timeout=float(os.getenv("CONTOSO_WORKER_TIMEOUT_SECONDS", "120"))
            )
        else:
            raise AgentConfigurationError("CONTOSO_WORKER_MODE must be inprocess or process.")

    app = FastAPI(title="Durable Contoso workflow", version="0.1.0")
    app.state.store, app.state.agents, app.state.dispatcher = store, agents, dispatcher
    # Fixed messages keep provider responses, worker stderr and credentials out of HTTP errors.
    errors = [
        ((ArtifactIntegrityError, ValidationError), 500, "Stored or generated data is invalid."),
        ((SessionConflictError, LeaseLostError), 409, "Session changed or its lock was lost."),
        (
            (UnsupportedDemoRequest,),
            400,
            "Request is outside the offline demo's supported grammar.",
        ),
        ((AgentConfigurationError,), 500, "Agent configuration is invalid; check the server .env."),
        (
            (
                WebResourceError,
                AgentFrameworkException,
                AzureError,
                httpx.HTTPError,
                OpenAIError,
                TimeoutError,
                RuntimeError,
            ),
            502,
            "Model, worker or storage request failed; inspect the last committed session.",
        ),
        ((ValueError, OSError), 500, "Workflow or storage operation failed."),
    ]

    async def failed_request(request: Request, error: Exception) -> JSONResponse:
        """Returns a safe HTTP error and logs exception types without exposing provider details."""
        causes = [error]
        while causes[-1].__cause__ is not None and causes[-1].__cause__ not in causes:
            causes.append(causes[-1].__cause__)
        status, detail = 500, "Workflow operation failed."
        for types, error_status, error_detail in errors:
            if any(isinstance(cause, types) for cause in causes):
                status, detail = error_status, error_detail
                break
        logger.error(
            "Workflow request failed: method=%s status=%s causes=%s",
            request.method,
            status,
            ",".join(type(cause).__name__ for cause in causes),
        )
        return JSONResponse({"detail": detail}, status_code=status)

    for types, _, _ in errors:
        for error_type in types:
            app.add_exception_handler(error_type, failed_request)

    def committed_session(session_id: UUID) -> SessionState:
        """Loads an existing conversation or rejects an unknown session before API operations."""
        state = store.load_session(str(session_id))
        if state is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        return state

    def files(refs: list[ArtifactRef]) -> list[dict]:
        """Adds session-scoped download URLs to artifact references returned by the API."""
        return [
            {
                "artifact": ref.model_dump(mode="json"),
                "download_url": str(
                    app.url_path_for(
                        "download_artifact",
                        session_id=ref.key.split("/")[1],
                        artifact_id=ref.key.split("/")[-2],
                        filename=ref.filename,
                    )
                ),
            }
            for ref in refs
        ]

    async def run_turn(session_id: UUID, turn: TurnRequest) -> dict:
        """Runs one durable agent turn and returns its result and artifact download URLs."""
        result = await invoke(
            store, agents, dispatcher, str(session_id), turn.prompt, direct_ppt=turn.direct_ppt
        )
        # The workflow has committed already; delivery does not rewrite or roll back history.
        return {
            **result.model_dump(mode="json"),
            "agent_mode": mode,
            "files": files(result.artifacts),
        }

    @app.get("/health")
    async def health() -> dict:
        """Reports API liveness and agent mode without making a model call."""
        return {"status": "ok", "agent_mode": mode}

    @app.post("/sessions", status_code=201)
    async def create_session(turn: TurnRequest) -> dict:
        """Starts a new conversation by running the caller's first prompt through the workflow."""
        return await run_turn(uuid4(), turn)

    @app.post("/sessions/{session_id}/turns")
    async def add_turn(session_id: UUID, turn: TurnRequest) -> dict:
        """Restores an existing conversation and runs its next prompt through the workflow."""
        committed_session(session_id)
        return await run_turn(session_id, turn)

    @app.get("/sessions/{session_id}")
    async def history(session_id: UUID) -> dict:
        """Returns committed conversation history and links to its saved artifacts."""
        state = committed_session(session_id)
        refs = {ref.key: ref for turn in state.turns for ref in turn.artifacts}
        return {**state.model_dump(mode="json"), "files": files(list(refs.values()))}

    @app.get("/sessions/{session_id}/artifacts/{artifact_id}/{filename}")
    async def download_artifact(session_id: UUID, artifact_id: UUID, filename: str) -> Response:
        """Returns verified bytes for an artifact belonging to the committed conversation."""
        state = committed_session(session_id)
        key = f"sessions/{session_id}/artifacts/{artifact_id}/{filename}"
        ref = next(
            (ref for turn in state.turns for ref in turn.artifacts if ref.key == key), None
        )
        if ref is None:
            raise HTTPException(status_code=404, detail="Committed artifact not found.")
        return Response(
            content=store.read_artifact(ref),
            media_type=ref.content_type,
            headers={"Content-Disposition": f'attachment; filename="{ref.filename}"'},
        )

    return app


def main() -> None:
    """Starts the loopback HTTP server that accepts new and continued agent conversations."""
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
