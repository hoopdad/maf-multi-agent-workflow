"""Fixed worker code with a storage-reference-only process boundary."""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from typing import TYPE_CHECKING, Protocol, TypeVar
from uuid import uuid4

from .models import (
    ArtifactRef,
    Contract,
    DeckSpec,
    ResearchDocument,
    SessionState,
    WorkerJob,
    WorkerResult,
)

if TYPE_CHECKING:
    from .agents import AgentBackend
    from .storage import ArtifactStore

PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
ModelT = TypeVar("ModelT", bound=Contract)


def read_json(store: ArtifactStore, ref: ArtifactRef, model: type[ModelT]) -> ModelT:
    if ref.content_type != "application/json":
        raise ValueError("Expected an application/json artifact")
    return model.model_validate_json(store.read_artifact(ref))


def put_json(store: ArtifactStore, session_id: str, filename: str, value: Contract) -> ArtifactRef:
    return store.put_artifact(
        session_id, filename, value.model_dump_json(indent=2).encode(), "application/json"
    )


def extract_job_reference(request: object) -> ArtifactRef:
    """Accept the direct and wrapped invocation shapes used by hosted agents."""
    if not isinstance(request, dict):
        raise ValueError("Worker request must be a JSON object")
    payload = request
    if "job" not in payload:
        payload = payload.get("input")
        if isinstance(payload, str):
            payload = json.loads(payload)
    if not isinstance(payload, dict) or set(payload) != {"job"}:
        raise ValueError("Expected {'job': ArtifactRef}, directly or inside 'input'")
    return ArtifactRef.model_validate(payload["job"])


def response_envelope(result: WorkerResult) -> dict:
    return {
        "id": f"resp_{uuid4().hex}",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": f"{result.kind} worker completed; artifacts are in shared storage.",
            }
        ],
        "metadata": {"result": result.model_dump(mode="json")},
    }


async def execute_job(
    store: ArtifactStore, agents: AgentBackend, job_ref: ArtifactRef
) -> WorkerResult:
    from .agents import research_markdown
    from .render import render_deck

    job = read_json(store, job_ref, WorkerJob)
    if not job_ref.belongs_to(job.session_id):
        raise ValueError("Job reference does not belong to the worker session")
    state = read_json(store, job.state_ref, SessionState)
    if state.session_id != job.session_id:
        raise ValueError("Worker state does not match the job session")
    if job.kind == "research":
        research = await agents.research(state, job.prompt)
        refs = [
            put_json(store, job.session_id, "research.json", research),
            store.put_artifact(
                job.session_id,
                "research.md",
                research_markdown(research).encode(),
                "text/markdown",
            ),
        ]
    else:
        research = (
            read_json(store, job.research_ref, ResearchDocument) if job.research_ref else None
        )
        previous = read_json(store, job.deck_ref, DeckSpec) if job.deck_ref else None
        deck = await agents.plan_deck(state, job.prompt, research, previous)
        refs = [
            put_json(store, job.session_id, "deck.json", deck),
            store.put_artifact(job.session_id, "presentation.pptx", render_deck(deck), PPTX_TYPE),
        ]
    return WorkerResult(session_id=job.session_id, kind=job.kind, artifacts=refs)


class WorkerDispatcher(Protocol):
    async def invoke(self, job: WorkerJob) -> WorkerResult: ...


def validate_worker_result(job: WorkerJob, result: WorkerResult) -> WorkerResult:
    if result.session_id != job.session_id or result.kind != job.kind:
        raise ValueError("Worker response does not match the dispatched job")
    expected = (
        {"research.json", "research.md"}
        if job.kind == "research"
        else {"deck.json", "presentation.pptx"}
    )
    if len(result.artifacts) != 2 or {ref.filename for ref in result.artifacts} != expected:
        raise ValueError("Worker returned an incomplete or unexpected artifact manifest")
    if any(not ref.belongs_to(job.session_id) for ref in result.artifacts):
        raise ValueError("Worker returned a cross-session artifact")
    return result


class InProcessDispatcher:
    def __init__(self, store: ArtifactStore, agents: AgentBackend):
        self.store = store
        self.agents = agents

    async def invoke(self, job: WorkerJob) -> WorkerResult:
        ref = put_json(self.store, job.session_id, "job.json", job)
        return validate_worker_result(job, await execute_job(self.store, self.agents, ref))


class ProcessDispatcher:
    """Separate processes today; the same envelopes can be carried by a remote transport."""

    def __init__(self, store: ArtifactStore, agent_mode: str, timeout: float = 120):
        if not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError("Worker timeout must be between 0 and 3600 seconds")
        self.store = store
        self.agent_mode = agent_mode
        self.timeout = timeout

    async def invoke(self, job: WorkerJob) -> WorkerResult:
        ref = put_json(self.store, job.session_id, "job.json", job)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "contoso_agents",
            "worker",
            "--request",
            json.dumps({"job": ref.model_dump(mode="json")}),
            "--agent-mode",
            self.agent_mode,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except (TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise TimeoutError(f"{job.kind} worker exceeded {self.timeout:g} seconds") from None
        if process.returncode != 0:
            raise RuntimeError(
                f"{job.kind} worker failed (exit {process.returncode}): "
                f"{stderr.decode(errors='replace')[-2000:]}"
            )
        payload = json.loads(stdout)
        if payload.get("status") != "completed":
            raise ValueError("Worker response did not indicate successful completion")
        result = WorkerResult.model_validate(payload["metadata"]["result"])
        result = validate_worker_result(job, result)
        for artifact in result.artifacts:
            self.store.read_artifact(artifact)
        return result
