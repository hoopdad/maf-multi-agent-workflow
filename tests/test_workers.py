import asyncio
import json
from uuid import uuid4

import pytest

from contoso_agents.models import SessionState, WorkerJob, WorkerResult
from contoso_agents.storage import LocalStore
from contoso_agents.workers import (
    ProcessDispatcher,
    extract_job_reference,
    put_json,
    response_envelope,
    validate_worker_result,
)


def test_hosted_agent_envelope_forms(tmp_path):
    store = LocalStore(tmp_path)
    ref = put_json(store, str(uuid4()), "state.json", SessionState(session_id=str(uuid4())))
    payload = {"job": ref.model_dump(mode="json")}
    for request in (payload, {"input": payload}, {"input": json.dumps(payload)}):
        assert extract_job_reference(request) == ref
    with pytest.raises(ValueError):
        extract_job_reference({"input": "[]"})
    with pytest.raises(ValueError):
        extract_job_reference({"input": {"job": {}, "unexpected": True}})


def test_worker_manifest_rejects_cross_session_and_partial_outputs(tmp_path):
    store = LocalStore(tmp_path)
    session = str(uuid4())
    state_ref = put_json(store, session, "state.json", SessionState(session_id=session))
    job = WorkerJob(session_id=session, kind="ppt", prompt="Topic", state_ref=state_ref)
    partial = WorkerResult(session_id=session, kind="ppt", artifacts=[state_ref])
    with pytest.raises(ValueError, match="manifest"):
        validate_worker_result(job, partial)
    unrelated = WorkerResult(session_id=str(uuid4()), kind="ppt", artifacts=[state_ref])
    with pytest.raises(ValueError, match="match"):
        validate_worker_result(job, unrelated)
    envelope = response_envelope(partial)
    assert envelope["status"] == "completed"
    assert envelope["metadata"]["result"]["artifacts"][0]["key"] == state_ref.key


async def test_process_timeout_terminates_worker_and_reports_failure(tmp_path, monkeypatch):
    class WaitingProcess:
        returncode = None
        calls = 0
        killed = False

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(60)
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -1

    process = WaitingProcess()

    async def spawn(*_, **__):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    store = LocalStore(tmp_path)
    session = str(uuid4())
    ref = put_json(store, session, "state.json", SessionState(session_id=session))
    job = WorkerJob(session_id=session, kind="ppt", prompt="Topic", state_ref=ref)
    dispatcher = ProcessDispatcher(store, "demo", timeout=0.01)
    with pytest.raises(TimeoutError, match="exceeded"):
        await dispatcher.invoke(job)
    assert process.killed
    assert process.calls == 2


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 3601])
def test_invalid_worker_timeout_rejected(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        ProcessDispatcher(LocalStore(tmp_path), "demo", timeout=timeout)
