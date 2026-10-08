import json
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pptx import Presentation

from contoso_agents.cli import DeliveryError, dispatch, main, parser
from contoso_agents.models import SessionState, WorkerJob
from contoso_agents.storage import LocalStore
from contoso_agents.workers import ProcessDispatcher, put_json


def run_cli(tmp_path: Path, *arguments: str) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "contoso_agents",
            *arguments,
            "--store",
            "local",
            "--data-dir",
            str(tmp_path / "data"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "CONTOSO_AGENT_MODE": "demo"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_fresh_processes_restore_history_and_share_research_files(tmp_path):
    first = run_cli(
        tmp_path,
        "invoke",
        "--agent-mode",
        "demo",
        "--worker-mode",
        "process",
        "--output-dir",
        str(tmp_path / "output"),
        "--prompt",
        "Research Azure networking and create a Contoso PPT.",
    )
    assert first["action"] == "research_ppt"
    assert len(first["files"]) == 4
    ppt_file = next(
        f["local_path"] for f in first["files"] if f["artifact"]["filename"].endswith(".pptx")
    )
    assert Presentation(BytesIO(Path(ppt_file).read_bytes())).slides
    second = run_cli(
        tmp_path,
        "invoke",
        "--agent-mode",
        "demo",
        "--worker-mode",
        "process",
        "--output-dir",
        str(tmp_path / "output"),
        "--session",
        first["session_id"],
        "--prompt",
        "Add a slide about cost controls",
    )
    assert second["revision"] == 2
    assert second["session_id"] == first["session_id"]
    assert second["files"][-1]["artifact"]["key"] != first["files"][-1]["artifact"]["key"]
    clarification = run_cli(
        tmp_path,
        "invoke",
        "--agent-mode",
        "demo",
        "--session",
        first["session_id"],
        "--prompt",
        "What did we discuss?",
    )
    assert clarification["action"] == "clarify"
    assert clarification["files"] == []
    state = run_cli(tmp_path, "show-session", "--session", first["session_id"])
    assert state["revision"] == 3
    assert len(state["turns"]) == 6
    assert len(state["turns"][1]["artifacts"]) == 4
    exported = run_cli(
        tmp_path,
        "export",
        "--session",
        first["session_id"],
        "--kind",
        "ppt",
        "--output-dir",
        str(tmp_path / "restored"),
    )
    assert Path(exported["file"]).is_file()
    assert Path(ppt_file).is_file()


async def test_delivery_failure_returns_committed_session_and_generated_files(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CONTOSO_STORE", "local")
    monkeypatch.setenv("CONTOSO_DATA_DIR", str(tmp_path / "data"))

    async def fail_upload(*_):
        request = httpx.Request("PUT", "https://graph.microsoft.com/example")
        response = httpx.Response(403, request=request)
        response.raise_for_status()

    monkeypatch.setattr("contoso_agents.cli.upload_to_onedrive", fail_upload)
    args = parser().parse_args(
        [
            "invoke",
            "--agent-mode",
            "demo",
            "--direct-ppt",
            "--onedrive",
            "--prompt",
            "Create a Contoso PPT about Azure networking.",
            "--output-dir",
            str(tmp_path / "output"),
        ]
    )
    with pytest.raises(DeliveryError) as caught:
        await dispatch(args)
    failure = caught.value.response
    assert failure["status"] == "delivery_failed"
    assert failure["revision"] == 1
    assert failure["session_id"]
    assert len(failure["files"]) == 2
    state = run_cli(tmp_path, "show-session", "--session", failure["session_id"])
    assert state["revision"] == 1


def test_malformed_worker_request_exits_nonzero(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "contoso_agents",
            "worker",
            "--request",
            "[]",
            "--agent-mode",
            "demo",
            "--store",
            "local",
            "--data-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)["status"] == "failed"


def test_no_dotenv_cli_defaults_to_offline_demo(tmp_path):
    environment = {key: value for key, value in os.environ.items() if key != "CONTOSO_AGENT_MODE"}
    environment["AZURE_AI_FOUNDRY_ENDPOINT"] = "invalid-live-config-unused-in-demo"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "contoso_agents",
            "invoke",
            "--direct-ppt",
            "--prompt",
            "Create PPT about Python",
            "--store",
            "local",
            "--data-dir",
            str(tmp_path / "data"),
            "--output-dir",
            str(tmp_path / "output"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["agent_mode"] == "demo"
    assert len(response["files"]) == 2


def test_cli_loads_exact_cwd_dotenv_before_dispatch_without_overriding_env(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONTOSO_AGENT_MODE", "demo")
    monkeypatch.setenv("CONTOSO_STORE", "local")
    monkeypatch.delenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", raising=False)
    (tmp_path / ".env").write_text(
        "CONTOSO_AGENT_MODE=live\nCONTOSO_STORE=azure\n"
        "AZURE_AI_MODEL_DEPLOYMENT_NAME=dotenv-deployment\n",
        encoding="utf-8",
    )
    seen = {}

    async def capture(args):
        seen.update(
            mode=os.environ["CONTOSO_AGENT_MODE"],
            store=os.environ["CONTOSO_STORE"],
            deployment=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        )
        return {"loaded": True}

    monkeypatch.setattr("contoso_agents.cli.dispatch", capture)
    monkeypatch.setattr(sys, "argv", ["contoso-agents", "invoke", "--prompt", "A topic"])
    main()
    assert seen == {"mode": "demo", "store": "local", "deployment": "dotenv-deployment"}
    assert json.loads(capsys.readouterr().out) == {"loaded": True}


def test_cli_does_not_search_parent_directories_for_dotenv(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("CONTOSO_AGENT_MODE=live\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    monkeypatch.delenv("CONTOSO_AGENT_MODE", raising=False)

    async def capture(args):
        assert "CONTOSO_AGENT_MODE" not in os.environ
        return {}

    monkeypatch.setattr("contoso_agents.cli.dispatch", capture)
    monkeypatch.setattr(sys, "argv", ["contoso-agents", "invoke", "--prompt", "A topic"])
    main()
    assert json.loads(capsys.readouterr().out) == {}


def test_dotenv_foundry_and_store_settings_are_inherited_by_process_worker(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    settings = {
        "CONTOSO_STORE": "local",
        "CONTOSO_DATA_DIR": str(tmp_path / "data"),
        "CONTOSO_AGENT_MODE": "live",
        "AZURE_AI_FOUNDRY_ENDPOINT": "https://contoso.services.ai.azure.com/openai/v1/",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "worker-deployment",
    }
    for name in settings:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "\n".join(f"{name}={value}" for name, value in settings.items()), encoding="utf-8"
    )

    class OfflineProcess:
        returncode = 1

        async def communicate(self):
            return b"", b"offline-worker-stub"

    async def spawn(*arguments, **kwargs):
        assert kwargs["env"].items() >= settings.items()
        assert arguments[-2:] == ("--agent-mode", "live")
        return OfflineProcess()

    async def capture(args):
        store = LocalStore(tmp_path / "data")
        session = str(uuid4())
        ref = put_json(store, session, "state.json", SessionState(session_id=session))
        job = WorkerJob(session_id=session, kind="ppt", prompt="Topic", state_ref=ref)
        with pytest.raises(RuntimeError, match="offline-worker-stub"):
            await ProcessDispatcher(store, "live").invoke(job)
        return {"inherited": True}

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("contoso_agents.cli.dispatch", capture)
    monkeypatch.setattr(sys, "argv", ["contoso-agents", "invoke", "--prompt", "Topic"])
    main()
    assert json.loads(capsys.readouterr().out) == {"inherited": True}
