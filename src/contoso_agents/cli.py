"""A small terminal/JSON user harness, not an application UI."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from azure.core.exceptions import AzureError
from dotenv import load_dotenv
from pydantic import ValidationError

from .agents import create_agents
from .models import ArtifactRef
from .onedrive import upload_to_onedrive
from .storage import ArtifactStore, create_store
from .workers import (
    InProcessDispatcher,
    ProcessDispatcher,
    execute_job,
    extract_job_reference,
    response_envelope,
)
from .workflow import invoke


def parser() -> argparse.ArgumentParser:
    """Defines the invoke, history, export, and worker commands used by the terminal harness."""
    root = argparse.ArgumentParser(description="Durable Contoso research/PPT MAF sample")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("invoke", help="Run one turn; reuse --session for later turns")
    run.add_argument("--prompt", required=True)
    run.add_argument("--session", default=None, help="UUID; defaults to a new session")
    run.add_argument("--direct-ppt", action="store_true", help="Use case 1: bypass model routing")
    run.add_argument("--worker-mode", choices=["inprocess", "process"], default=None)
    run.add_argument("--output-dir", type=Path, default=Path("output"))
    run.add_argument("--onedrive", action="store_true", help="Opt-in Graph upload of generated PPT")
    show = commands.add_parser(
        "show-session", help="Read all committed turns and latest artifact refs"
    )
    show.add_argument("--session", required=True)
    export = commands.add_parser("export", help="Retrieve the latest PPT or research after restart")
    export.add_argument("--session", required=True)
    export.add_argument("--kind", choices=["ppt", "research"], default="ppt")
    export.add_argument("--output-dir", type=Path, default=Path("output"))
    worker = commands.add_parser("worker", help="Execute a fixed worker from a reference envelope")
    worker.add_argument(
        "--request", required=True, help="JSON direct/input wrapper containing job ref"
    )
    for sub in (run, show, export, worker):
        sub.add_argument("--store", choices=["local", "azure"], default=None)
        sub.add_argument("--data-dir", type=Path, default=None)
    for sub in (run, worker):
        sub.add_argument("--agent-mode", choices=["demo", "live"], default=None)
    return root


class DeliveryError(RuntimeError):
    """Reports export or upload failure while retaining the completed turn's artifact references."""

    def __init__(self, response: dict, cause: Exception):
        super().__init__(str(cause))
        self.response = {
            **response,
            "status": "delivery_failed",
            "error_type": type(cause).__name__,
            "error": str(cause),
        }


def export_artifact(store: ArtifactStore, ref: ArtifactRef, output_dir: Path) -> str:
    """Exports verified artifact bytes to a local output file and returns its absolute path."""
    target = output_dir / ref.key.split("/")[-2] / ref.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(store.read_artifact(ref))
    return str(target.resolve())


async def dispatch(args: argparse.Namespace) -> dict:
    """Routes CLI commands to the workflow, session lookup, artifact export, or worker execution."""
    if args.store is not None:
        os.environ["CONTOSO_STORE"] = args.store
    if args.data_dir is not None:
        os.environ["CONTOSO_DATA_DIR"] = str(args.data_dir.resolve())
    store = create_store()
    if args.command in {"show-session", "export"}:
        state = store.load_session(args.session)
        if state is None:
            raise FileNotFoundError(f"Session {args.session} does not exist")
        if args.command == "show-session":
            return state.model_dump(mode="json")
        ref = state.latest_pptx if args.kind == "ppt" else state.latest_research
        if ref is None:
            raise FileNotFoundError(
                f"No {args.kind} artifact exists for session {state.session_id}"
            )
        return {
            "session_id": state.session_id,
            "artifact": ref.model_dump(mode="json"),
            "file": export_artifact(store, ref, args.output_dir),
        }
    mode = args.agent_mode or os.environ.get("CONTOSO_AGENT_MODE", "demo")
    agents = create_agents(mode)
    if args.command == "worker":
        result = await execute_job(store, agents, extract_job_reference(json.loads(args.request)))
        return response_envelope(result)
    worker_mode = args.worker_mode or os.environ.get("CONTOSO_WORKER_MODE", "inprocess")
    if worker_mode not in {"inprocess", "process"}:
        raise ValueError("CONTOSO_WORKER_MODE must be inprocess or process")
    dispatcher = (
        ProcessDispatcher(
            store, mode, timeout=float(os.environ.get("CONTOSO_WORKER_TIMEOUT_SECONDS", "120"))
        )
        if worker_mode == "process"
        else InProcessDispatcher(store, agents)
    )
    result = await invoke(
        store,
        agents,
        dispatcher,
        args.session or str(uuid4()),
        args.prompt,
        direct_ppt=args.direct_ppt,
    )
    response = result.model_dump(mode="json")
    response["agent_mode"] = mode
    response["files"] = []
    try:
        for ref in result.artifacts:
            response["files"].append(
                {
                    "artifact": ref.model_dump(mode="json"),
                    "storage_location": store.artifact_location(ref),
                    "local_path": export_artifact(store, ref, args.output_dir),
                }
            )
        if args.onedrive:
            ppt = next(
                (ref for ref in result.artifacts if ref.filename == "presentation.pptx"), None
            )
            if ppt is None:
                raise ValueError("The completed turn produced no PPT to upload to OneDrive")
            receipt = await upload_to_onedrive(ppt, store.read_artifact(ppt))
            response["onedrive"] = receipt.model_dump(mode="json")
    except (ValueError, OSError, AzureError, httpx.HTTPError) as error:
        # Generation was committed; preserve its session/artifacts in a failed delivery response.
        raise DeliveryError(response, error) from error
    return response


def main() -> None:
    """Loads local settings and runs the selected CLI command with JSON results or errors."""
    load_dotenv(Path.cwd() / ".env", override=False)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    args = parser().parse_args()
    try:
        response = asyncio.run(dispatch(args))
    except DeliveryError as error:
        print(json.dumps(error.response), file=sys.stderr)
        raise SystemExit(1) from None
    except (
        ValueError,
        OSError,
        RuntimeError,
        TimeoutError,
        AzureError,
        httpx.HTTPError,
        ValidationError,
    ) as error:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(response, indent=2))
