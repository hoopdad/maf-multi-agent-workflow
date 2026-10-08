import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from openai import OpenAIError

from contoso_agents import api
from contoso_agents.agents import AgentConfigurationError, DemoAgents, LiveAgents, _DeckDraft
from contoso_agents.models import ArtifactRef, DeckSpec, RouteDecision, SlideSpec
from contoso_agents.storage import LocalStore, SessionConflictError
from contoso_agents.workers import PPTX_TYPE, InProcessDispatcher


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return LocalStore(tmp_path / "store")


@pytest.fixture
async def client(store):
    agents = DemoAgents()
    app = api.create_app(
        store=store, agents=agents, dispatcher=InProcessDispatcher(store, agents)
    )
    async with client_for(app) as client:
        yield client


async def test_demo_create_revise_restart_history_and_verified_downloads(client, store):
    response = await client.post(
        "/sessions", json={"prompt": "Research Azure networking and create a Contoso PPT"}
    )
    assert response.status_code == 201
    first = response.json()
    session = first["session_id"]
    assert first["revision"] == 1 and first["action"] == "research_ppt"
    assert first["agent_mode"] == "injected"
    assert len(first["artifacts"]) == len(first["files"]) == 4
    original_ppt = next(
        file for file in first["files"] if file["artifact"]["filename"] == "presentation.pptx"
    )
    assert original_ppt["artifact"]["content_type"] == PPTX_TYPE
    for file in first["files"]:
        download = await client.get(file["download_url"])
        ref = ArtifactRef.model_validate(file["artifact"])
        assert download.status_code == 200
        assert download.content == store.read_artifact(ref)
        assert download.headers["content-type"].startswith(ref.content_type)
        assert download.headers["content-disposition"] == f'attachment; filename="{ref.filename}"'

    changed = await client.post(
        f"/sessions/{session.upper()}/turns", json={"prompt": "Add a slide about cost controls"}
    )
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    new_ppt = next(
        file for file in changed.json()["files"]
        if file["artifact"]["filename"] == "presentation.pptx"
    )
    assert new_ppt["download_url"] != original_ppt["download_url"]
    ppt_bytes = (await client.get(new_ppt["download_url"])).content
    with ZipFile(io.BytesIO(ppt_bytes)) as presentation:
        slides = [
            presentation.read(name) for name in presentation.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        ]
        assert any(b"cost controls" in slide for slide in slides)

    restarted = api.create_app(store=LocalStore(store.root), agents=DemoAgents())
    async with client_for(restarted) as restored:
        history = (await restored.get(f"/sessions/{session.upper()}")).json()
        assert history["session_id"] == session and history["revision"] == 2
        assert len(history["turns"]) == 4 and len(history["files"]) == 6
        assert len(history["turns"][1]["artifacts"]) == 4
        assert (await restored.get(original_ppt["download_url"])).content.startswith(b"PK")
        answer = await restored.post(
            f"/sessions/{session}/turns", json={"prompt": "What did we discuss?"}
        )
        assert answer.status_code == 200 and answer.json()["revision"] == 3
        assert "Add a slide about cost controls" in answer.json()["message"]
        assert answer.json()["artifacts"] == answer.json()["files"] == []


async def test_direct_ppt_uses_injected_dispatcher_and_bypasses_routing(store):
    class DirectOnly(DemoAgents):
        async def route(self, state, prompt):
            raise AssertionError("Direct PPT should not route")

        async def research(self, state, prompt):
            raise AssertionError("Direct PPT should not research")

    class RecordingDispatcher(InProcessDispatcher):
        async def invoke(self, job):
            self.job = job
            return await super().invoke(job)

    agents = DirectOnly()
    dispatcher = RecordingDispatcher(store, agents)
    app = api.create_app(store=store, agents=agents, dispatcher=dispatcher)
    async with client_for(app) as client:
        response = await client.post(
            "/sessions", json={"prompt": "Create PPT about DNS", "direct_ppt": True}
        )
    assert response.status_code == 201 and response.json()["action"] == "ppt"
    assert dispatcher.job.kind == "ppt"
    assert dispatcher.job.research_ref is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"prompt": ""},
        {"prompt": " \r\n\t "},
        {"prompt": "x" * 8001},
        {"prompt": None},
        {"prompt": 42},
        {"prompt": "Create PPT about DNS", "session_id": str(uuid4())},
    ],
)
async def test_request_validation_rejects_bad_prompts_and_extra_fields(client, body):
    assert (await client.post("/sessions", json=body)).status_code == 422


async def test_unknown_sessions_artifacts_and_invalid_uuids_do_not_create_turns(client, store):
    session = str(uuid4())
    artifact = str(uuid4())
    for method, path, body in [
        ("GET", f"/sessions/{session}", None),
        ("POST", f"/sessions/{session}/turns", {"prompt": "Create PPT about DNS"}),
        ("GET", f"/sessions/{session}/artifacts/{artifact}/presentation.pptx", None),
    ]:
        assert (await client.request(method, path, json=body)).status_code == 404
    assert store.load_session(session) is None
    assert (await client.get("/sessions/not-a-uuid")).status_code == 422
    assert (
        await client.post("/sessions/not-a-uuid/turns", json={"prompt": "Topic"})
    ).status_code == 422
    assert (
        await client.get(f"/sessions/{session}/artifacts/not-a-uuid/presentation.pptx")
    ).status_code == 422


async def test_cross_session_orphan_and_wrong_filename_downloads_are_rejected(client, store):
    first = (await client.post("/sessions", json={"prompt": "Create PPT about DNS"})).json()
    second = (await client.post("/sessions", json={"prompt": "Create PPT about costs"})).json()
    file = first["files"][-1]
    other_session_url = file["download_url"].replace(first["session_id"], second["session_id"])
    assert (await client.get(other_session_url)).status_code == 404
    orphan = store.put_artifact(first["session_id"], "orphan.txt", b"uncommitted", "text/plain")
    orphan_url = "/sessions/" + orphan.key.removeprefix("sessions/")
    assert (await client.get(orphan_url)).status_code == 404
    assert (
        await client.get(file["download_url"].replace("presentation.pptx", "other.pptx"))
    ).status_code == 404


async def test_corrupt_or_missing_download_fails_without_rolling_back_session(client, store):
    first = (await client.post("/sessions", json={"prompt": "Create PPT about DNS"})).json()
    file = first["files"][-1]
    ref = ArtifactRef.model_validate(file["artifact"])
    before = store.load_session(first["session_id"]).model_dump()
    path = Path(store.artifact_location(ref))
    path.write_bytes(b"corrupted")
    response = await client.get(file["download_url"])
    assert response.status_code == 500
    assert response.json() == {"detail": "Stored or generated data is invalid."}
    path.unlink()
    assert (await client.get(file["download_url"])).status_code == 500
    assert store.load_session(first["session_id"]).model_dump() == before


async def test_same_session_requests_keep_both_turns_and_full_prior_history(store):
    class DelayedAgents(DemoAgents):
        def __init__(self):
            self.history_lengths = []

        async def route(self, state, prompt):
            self.history_lengths.append(len(state.turns))
            return await super().route(state, prompt)

        async def plan_deck(self, state, prompt, research, previous):
            await asyncio.sleep(0.02)
            return await super().plan_deck(state, prompt, research, previous)

    agents = DelayedAgents()
    app = api.create_app(store=store, agents=agents)
    async with client_for(app) as client:
        first = (await client.post("/sessions", json={"prompt": "Create PPT about DNS"})).json()
        session = first["session_id"]
        responses = await asyncio.gather(
            client.post(f"/sessions/{session}/turns", json={"prompt": "Add costs"}),
            client.post(f"/sessions/{session}/turns", json={"prompt": "Add monitoring"}),
        )
        assert all(response.status_code == 200 for response in responses)
        assert sorted(response.json()["revision"] for response in responses) == [2, 3]
    assert agents.history_lengths == [0, 2, 4]
    state = store.load_session(session)
    assert len(state.turns) == 6 and state.revision == 3
    deck = DeckSpec.model_validate_json(store.read_artifact(state.latest_deck))
    assert {slide.title for slide in deck.slides}.issuperset({"costs", "monitoring"})


async def test_live_model_boundary_receives_committed_history_and_failure_does_not_commit(
    store, monkeypatch, caplog
):
    class ModelBoundary:
        def __init__(self, task):
            self.task, self.calls, self.fail = task, [], False

        async def run(self, messages, options=None):
            self.calls.append(messages)
            context = json.loads(messages[-1].text)
            if self.fail:
                raise OpenAIError("Do not expose provider-secret-token in an HTTP response")
            if self.task == "route":
                value = RouteDecision(action="ppt", instruction=context["request"])
            else:
                assert options["response_format"] is _DeckDraft
                previous = context["previous_deck"]
                if previous:
                    value = _DeckDraft.model_validate(
                        DeckSpec.model_validate(previous).model_dump(exclude={"sources"})
                    )
                    value.slides = [*value.slides, SlideSpec(title="Cost", bullets=["Controls"])]
                else:
                    value = _DeckDraft(
                        title="Executive briefing",
                        slides=[SlideSpec(title="DNS", bullets=["Overview"])],
                    )
            return SimpleNamespace(value=value, text=value.model_dump_json())

    backend = object.__new__(LiveAgents)
    backend.orchestrator, backend.planner = ModelBoundary("route"), ModelBoundary("plan_deck")
    monkeypatch.setenv("CONTOSO_AGENT_MODE", "live")
    monkeypatch.setattr(api, "create_agents", lambda mode: backend)
    app = api.create_app(store=store)
    async with client_for(app) as client:
        first_prompt = "Give executives a DNS briefing, with a clear visual narrative."
        first = await client.post("/sessions", json={"prompt": first_prompt})
        assert first.status_code == 201 and first.json()["agent_mode"] == "live"
        session = first.json()["session_id"]
        second = await client.post(
            f"/sessions/{session}/turns", json={"prompt": "Keep that narrative and cover cost."}
        )
        assert second.status_code == 200 and second.json()["revision"] == 2
        assert len(backend.orchestrator.calls) == len(backend.planner.calls) == 2
        for boundary in [backend.orchestrator, backend.planner]:
            assert boundary.calls[1][0].text == first_prompt
            assert boundary.calls[1][1].text == first.json()["message"]
            context = json.loads(boundary.calls[1][-1].text)
            assert context["session"]["revision"] == 1
            assert len(context["committed_turn_metadata"]) == 2
        previous = json.loads(backend.planner.calls[1][-1].text)["previous_deck"]
        assert previous["slides"][0]["title"] == "DNS"

        before = store.load_session(session).model_dump()
        backend.planner.fail = True
        failed = await client.post(f"/sessions/{session}/turns", json={"prompt": "Revise again"})
        assert failed.status_code == 502 and "provider-secret-token" not in failed.text
        assert "OpenAIError" in caplog.text
        assert "provider-secret-token" not in caplog.text
        assert "artifacts" not in failed.json()
        assert store.load_session(session).model_dump() == before


async def test_known_revision_conflict_returns_409_and_keeps_committed_state(
    client, store, monkeypatch
):
    first = (await client.post("/sessions", json={"prompt": "Create PPT about DNS"})).json()
    session = first["session_id"]
    before = store.load_session(session).model_dump()

    def conflict(state):
        raise SessionConflictError("Private storage details should not be returned")

    monkeypatch.setattr(store, "save_session", conflict)
    response = await client.post(f"/sessions/{session}/turns", json={"prompt": "Add costs"})
    assert response.status_code == 409 and "Private storage" not in response.text
    assert store.load_session(session).model_dump() == before


def test_exact_cwd_dotenv_default_live_explicit_demo_and_environment_precedence(store, monkeypatch):
    calls = []

    def configured_agents(mode):
        calls.append(mode)
        if mode != "demo":
            raise AgentConfigurationError("Missing live configuration; no demo fallback")
        return DemoAgents()

    monkeypatch.setattr(api, "create_agents", configured_agents)
    monkeypatch.setenv("CONTOSO_AGENT_MODE", "live")
    monkeypatch.delenv("CONTOSO_AGENT_MODE")
    parent = Path.cwd()
    (parent / ".env").write_text("CONTOSO_AGENT_MODE=demo\n")
    child = parent / "run"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(AgentConfigurationError, match="no demo fallback"):
        api.create_app(store=store)
    assert calls == ["live"]
    (child / ".env").write_text("CONTOSO_AGENT_MODE=demo\n")
    monkeypatch.setenv("CONTOSO_AGENT_MODE", "live")
    with pytest.raises(AgentConfigurationError):
        api.create_app(store=store)
    assert calls == ["live", "live"]
    monkeypatch.delenv("CONTOSO_AGENT_MODE")
    app = api.create_app(store=store)
    assert calls == ["live", "live", "demo"]
    assert app.state.agents.__class__ is DemoAgents


async def test_health_and_openapi_describe_routes_without_an_extra_model_call(client):
    assert (await client.get("/health")).json() == {"status": "ok", "agent_mode": "injected"}
    assert (await client.get("/docs")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert "/sessions/{session_id}/turns" in schema["paths"]
    assert schema["components"]["schemas"]["TurnRequest"]["additionalProperties"] is False


def test_module_import_does_not_load_dotenv_create_store_or_connect_model(store):
    Path(".env").write_text("CONTOSO_AGENT_MODE=demo\n")
    env = {key: value for key, value in os.environ.items() if key != "CONTOSO_AGENT_MODE"}
    code = (
        "import os; from contoso_agents import api; "
        "assert 'CONTOSO_AGENT_MODE' not in os.environ; assert not hasattr(api, 'app')"
    )
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not Path(".data").exists()
