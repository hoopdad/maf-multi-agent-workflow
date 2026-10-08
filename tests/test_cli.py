import json
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from pptx import Presentation

from contoso_agents.cli import DeliveryError, dispatch, parser


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
