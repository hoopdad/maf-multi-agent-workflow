import json
from contextvars import ContextVar
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from contoso_agents.agents import (
    AgentBackend,
    AgentConfigurationError,
    DemoAgents,
    LiveAgents,
    UnsupportedDemoRequest,
    _evidence_context,
    create_agents,
)
from contoso_agents.models import (
    ArtifactRef,
    DeckSpec,
    Finding,
    ResearchDocument,
    RouteDecision,
    SessionState,
    SlideSpec,
    SourceRecord,
    Turn,
)
from contoso_agents.web import WebResources


def state():
    return SessionState(
        session_id=str(uuid4()),
        turns=[
            Turn(role="user", content="Remember an executive audience."),
            Turn(role="assistant", content="I will use an executive audience."),
        ],
    )


def reference(session, name):
    return ArtifactRef(
        key=f"sessions/{session.session_id}/artifacts/{name}.json",
        sha256="a" * 64,
        size=1,
        content_type="application/json",
        filename=f"{name}.json",
    )


def source(identifier="S1", title="Python", url="https://docs.python.org/3/"):
    return SourceRecord(
        id=identifier,
        title=title,
        url=url,
        excerpt="Verified source evidence.",
        retrieved_at="2026-10-08T15:00:00+00:00",
    )


def research():
    return ResearchDocument(
        title="Python",
        summary="Grounded overview.",
        findings=[Finding(text="A cited finding.", source_ids=["S1"])],
        sources=[source()],
        limitations=["Bounded evidence."],
    )


def deck():
    return DeckSpec(
        title="Python",
        subtitle="Research discussion",
        slides=[
            SlideSpec(
                title="Overview",
                bullets=["Original point."],
                notes="Original notes.",
                source_ids=["S1"],
            ),
            SlideSpec(title="Conclusion", bullets=["Original conclusion."]),
        ],
        sources=[source()],
    )


@pytest.mark.parametrize(
    ("prompt", "action"),
    [
        ("Research Python", "research"),
        ("Research Python and create PPT", "research_ppt"),
        ("Research Azure networking and create a Contoso PPT.", "research_ppt"),
        ("Create PPT about Python", "ppt"),
        ("Create a Contoso PPT about Azure networking.", "ppt"),
        ("Create a Contoso PPT about Azure networking. Include private endpoints and DNS.", "ppt"),
        ("Create a Contoso PPT", "clarify"),
        ("Create a presentation", "clarify"),
        ("Create PPT", "clarify"),
        ("What did we discuss?", "clarify"),
        ("What sources did you use?", "clarify"),
        ("Translate to French", "clarify"),
        ("Add a slide", "clarify"),
        ("Re-create PPT", "clarify"),
    ],
)
async def test_demo_routes_only_supported_intents(prompt, action):
    assert (await DemoAgents().route(state(), prompt)).action == action


async def test_multiturn_revisions_use_previous_and_preserve_unrequested_information():
    backend = DemoAgents()
    session = state()
    doc = await backend.research(session, "Research Python")
    original = await backend.plan_deck(session, "Create PPT about Python", doc)
    session.latest_research = reference(session, "research")
    session.latest_deck = reference(session, "deck")
    session.turns += [
        Turn(role="user", content="Create PPT about Python"),
        Turn(role="assistant", content="Saved the offline Python presentation."),
    ]
    assert (await backend.route(session, "Add Deployment considerations")).action == "ppt"
    added = await backend.plan_deck(session, "Add Deployment considerations", doc, original)
    assert added.slides[:-1] == original.slides
    assert added.slides[-1].bullets == ["Deployment considerations"]
    removed = await backend.plan_deck(session, "Remove Deployment considerations", doc, added)
    assert removed == original
    recreated = await backend.plan_deck(session, "Re-create PPT", doc, removed)
    assert recreated == original and recreated is not original
    answer = await backend.clarify(session, "What slides are in the presentation?", doc, added)
    assert "Deployment considerations" in answer
    assert session.turns[-1].content == "Saved the offline Python presentation."


async def test_parent_offline_cli_example_uses_only_prior_committed_history():
    backend = DemoAgents()
    session = SessionState(session_id=str(uuid4()))
    prompt = "Create a Contoso PPT about Azure networking. Include private endpoints and DNS."
    decision = await backend.route(session, prompt)
    assert decision.action == "ppt"
    original = await backend.plan_deck(session, prompt)
    assert "Azure networking" in original.title
    content = " ".join(bullet for slide in original.slides for bullet in slide.bullets)
    assert "private endpoints" in content and "DNS" in content
    assert session.turns == []
    session.latest_deck = reference(session, "deck")
    session.turns = [
        Turn(role="user", content=prompt),
        Turn(role="assistant", content="Saved the offline deck."),
    ]
    add = "Add a slide about cost controls"
    assert (await backend.route(session, add)).action == "ppt"
    added = await backend.plan_deck(session, add, None, original)
    assert added.slides[-1].title == "cost controls"
    assert added.slides[:-1] == original.slides
    session.turns += [
        Turn(role="user", content=add),
        Turn(role="assistant", content="Saved the addition."),
    ]
    remove = "Remove cost controls"
    removed = await backend.plan_deck(session, remove, None, added)
    assert removed == original
    session.turns += [
        Turn(role="user", content=remove),
        Turn(role="assistant", content="Saved the removal."),
    ]
    recreated = await backend.plan_deck(session, "Re-create the PPT", None, removed)
    assert recreated == original
    before = session.model_dump()
    assert (await backend.route(session, "What did we discuss?")).action == "clarify"
    answer = await backend.clarify(session, "What did we discuss?", None, recreated)
    assert prompt in answer and add in answer and remove in answer
    assert "What did we discuss?" not in answer
    assert "Latest saved presentation" in answer
    assert session.model_dump() == before


async def test_missing_contoso_topic_requests_clarification_and_does_not_plan():
    backend = DemoAgents()
    session = SessionState(session_id=str(uuid4()))
    decision = await backend.route(session, "Create a Contoso PPT")
    assert decision.action == "clarify"
    assert "topic" in decision.message
    with pytest.raises(UnsupportedDemoRequest, match="topic"):
        await backend.plan_deck(session, "Create a Contoso PPT")


async def test_exact_documented_kickoff_prompts_are_supported_without_model_calls():
    backend = DemoAgents()
    session = SessionState(session_id=str(uuid4()))
    combined = "Research Azure networking and create a Contoso PPT."
    assert (await backend.route(session, combined)).action == "research_ppt"
    document = await backend.research(session, combined)
    assert document.title == "Azure networking"
    result = await backend.plan_deck(session, combined, document)
    assert result.title == "Azure networking" and result.sources == []
    direct = await backend.plan_deck(session, "Create a Contoso PPT about Azure networking.")
    assert direct.title == "Azure networking." and direct.sources == []
    assert session.turns == []


async def test_offline_research_never_fabricates_evidence(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Demo must never construct a model client or a web client")

    monkeypatch.setattr("contoso_agents.agents._model_client", fail)
    monkeypatch.setattr("httpx.AsyncClient", fail)
    backend = create_agents("demo")
    assert isinstance(backend, AgentBackend)
    doc = await backend.research(state(), "Research Python")
    assert doc.sources == []
    assert all(not item.source_ids for item in doc.findings)
    assert "OFFLINE" in doc.summary
    assert (await backend.plan_deck(state(), "Create PPT about Python", doc)).sources == []


async def test_demo_unsupported_revisions_fail_not_pretend():
    backend = DemoAgents()
    with pytest.raises(UnsupportedDemoRequest):
        await backend.plan_deck(state(), "Make it more persuasive", None, deck())
    with pytest.raises(UnsupportedDemoRequest):
        await backend.plan_deck(state(), "Remove does not exist", None, deck())
    with pytest.raises(UnsupportedDemoRequest):
        await backend.plan_deck(state(), "Re-create PPT")
    one = deck().model_copy(update={"slides": [deck().slides[0]]})
    with pytest.raises(UnsupportedDemoRequest):
        await backend.plan_deck(state(), "Remove 1", None, one)


async def test_large_demo_input_is_preserved_or_explicitly_rejected():
    topic = "User content " * 450
    result = await DemoAgents().plan_deck(state(), "Create PPT about " + topic)
    assert (
        "".join(bullet for slide in result.slides[:-1] for bullet in slide.bullets) == topic.strip()
    )
    full = deck().model_copy(
        update={"slides": [SlideSpec(title=str(i), bullets=["Original."]) for i in range(12)]}
    )
    with pytest.raises(UnsupportedDemoRequest, match="12"):
        await DemoAgents().plan_deck(state(), "Add more content", None, full)


async def test_create_from_saved_research_without_repeating_topic():
    session = state()
    session.latest_research = reference(session, "research")
    decision = await DemoAgents().route(session, "Create PPT")
    assert decision.action == "ppt" and decision.use_research
    result = await DemoAgents().plan_deck(session, decision.instruction, research())
    assert result.title == "Python"
    assert result.slides[0].source_ids == ["S1"]


@pytest.mark.parametrize("mode", ["invalid", "test", "offline"])
def test_factory_rejects_unknown_mode(mode):
    with pytest.raises(AgentConfigurationError):
        create_agents(mode)


def test_live_missing_config_never_falls_back(monkeypatch):
    for name in [
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME",
        "CONTOSO_AGENT_MODE",
    ]:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AgentConfigurationError, match="Live mode requires"):
        create_agents("live")
    monkeypatch.setenv("CONTOSO_AGENT_MODE", "demo")
    assert isinstance(create_agents(), DemoAgents)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    with pytest.raises(AgentConfigurationError, match="deployment"):
        create_agents("live")


def test_real_maf_agent_construction_has_three_agents_and_web_tools():
    from agent_framework import Agent
    from agent_framework.openai import OpenAIChatCompletionClient

    backend = LiveAgents(
        client=OpenAIChatCompletionClient(api_key="offline-construction-test", model="gpt-4o-mini")
    )
    assert isinstance(backend.orchestrator, Agent)
    assert isinstance(backend.researcher, Agent)
    assert isinstance(backend.planner, Agent)
    assert backend.orchestrator.name == "contoso-orchestrator"
    assert backend.researcher.name == "contoso-researcher"
    assert backend.planner.name == "contoso-ppt-planner"


async def test_live_factories_accept_config_without_authentication_or_network(monkeypatch):
    from openai import AsyncAzureOpenAI, AsyncOpenAI

    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "offline-test-model")
    openai_backend = create_agents("live")
    assert isinstance(openai_backend.client.client, AsyncOpenAI)
    assert openai_backend.client.model == "offline-test-model"
    await openai_backend.client.client.close()
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://contoso.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME", "test-deployment")
    azure_backend = create_agents("live")
    assert isinstance(azure_backend.client.client, AsyncAzureOpenAI)
    assert azure_backend.client.model == "test-deployment"
    assert azure_backend.client.api_version == "2024-10-21"
    await azure_backend.client.client.close()


class StubAgent:
    def __init__(self, result, *, before=None):
        self.result = result
        self.before = before
        self.calls = []

    async def run(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.before:
            await self.before()
        return SimpleNamespace(
            value=self.result,
            text=self.result.model_dump_json()
            if hasattr(self.result, "model_dump_json")
            else str(self.result),
        )


def stub_backend():
    backend = object.__new__(LiveAgents)
    backend._evidence = ContextVar("test_evidence")
    return backend


async def test_live_calls_include_complete_committed_history_and_previous_content():
    session = state()
    backend = stub_backend()
    backend.orchestrator = StubAgent(RouteDecision(action="ppt", instruction="Create PPT"))
    backend.planner = StubAgent(deck())
    assert (await backend.route(session, "Create PPT about Python")).action == "ppt"
    result = await backend.plan_deck(session, "Create PPT about Python", research(), deck())
    assert result == deck()
    for agent in [backend.orchestrator, backend.planner]:
        messages, kwargs = agent.calls[0]
        assert [message.text for message in messages[:2]] == [
            turn.content for turn in session.turns
        ]
        assert [str(message.role) for message in messages[:2]] == ["user", "assistant"]
        assert "response_format" in kwargs["options"]
    context = json.loads(backend.planner.calls[0][0][-1].text)
    assert context["previous_deck"] == deck().model_dump()
    assert context["research"] == research().model_dump()
    assert context["committed_turn_metadata"] == [
        turn.model_dump(exclude={"content", "role"}) for turn in session.turns
    ]
    backend.orchestrator = StubAgent("The prior deck has Overview and Conclusion.")
    answer = await backend.clarify(session, "What did you create?", research(), deck())
    assert "Overview" in answer
    assert len(backend.orchestrator.calls[0][0]) == len(session.turns) + 1


async def test_live_recreation_is_exact_and_past_questions_cannot_regenerate():
    backend = stub_backend()
    backend.orchestrator = StubAgent(RouteDecision(action="research_ppt", instruction="Research"))
    decision = await backend.route(state(), "What did you create previously?")
    assert decision.action == "clarify"
    original = deck()
    recreated = await backend.plan_deck(state(), "Re-create PPT", None, original)
    assert recreated == original and recreated is not original
    with pytest.raises(ValueError, match="no previous"):
        await backend.plan_deck(state(), "Re-create PPT")


async def test_live_missing_topic_routes_to_clarification_even_if_model_misroutes():
    backend = stub_backend()
    backend.orchestrator = StubAgent(RouteDecision(action="ppt", instruction="Create PPT"))
    decision = await backend.route(SessionState(session_id=str(uuid4())), "Create PPT")
    assert decision.action == "clarify"
    assert "topic" in decision.message


async def test_planner_rejects_fake_sources_and_unrequested_removal():
    backend = stub_backend()
    bad = deck().model_copy(update={"sources": [source(title="Fabricated title")]})
    backend.planner = StubAgent(bad)
    with pytest.raises(ValueError, match="fabricated"):
        await backend.plan_deck(state(), "Create PPT about Python", research())
    backend.planner = StubAgent(deck().model_copy(update={"slides": [deck().slides[0]]}))
    with pytest.raises(ValueError, match="preserve"):
        await backend.plan_deck(state(), "Add Pricing", research(), deck())
    backend.planner = StubAgent(deck())
    with pytest.raises(ValueError, match="not applied"):
        await backend.plan_deck(state(), "Add Pricing", research(), deck())
    with pytest.raises(ValueError, match="Removal"):
        await backend.plan_deck(state(), "Remove Overview", research(), deck())


def test_new_research_source_ids_do_not_alias_previous_deck_evidence():
    new = ResearchDocument(
        title="New research",
        summary="New evidence.",
        findings=[Finding(text="New cited finding.", source_ids=["S1"])],
        sources=[source(title="Different source", url="https://learn.microsoft.com/new")],
    )
    normalized, ledger = _evidence_context(new, deck())
    assert normalized.sources[0].id == "S2"
    assert normalized.findings[0].source_ids == ["S2"]
    assert ledger["S1"] == deck().sources[0]
    assert ledger["S2"].url == new.sources[0].url
    assert new.sources[0].id == "S1"


async def test_research_uses_real_retrieved_records_not_model_supplied_sources():
    from contoso_agents.agents import _ResearchDraft

    backend = stub_backend()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<title>Actual page</title><p>Actual retrieved evidence.</p>",
            )
        )
    )
    web = WebResources(
        client=client,
        resolver=lambda host: ["93.184.215.14"],
        urls=["https://docs.python.org/3/"],
    )
    backend._web_factory = lambda: web

    async def retrieve():
        result = await backend.search_web("Python")
        assert "Actual retrieved evidence." in result

    backend.researcher = StubAgent(
        _ResearchDraft(
            title="Python",
            summary="Summary.",
            findings=[Finding(text="Actual retrieved evidence.", source_ids=["S1"])],
        ),
        before=retrieve,
    )
    async with client:
        result = await backend.research(state(), "Research Python")
    assert result.sources[0].title == "Actual page"
    assert result.sources[0].excerpt == "Actual retrieved evidence."
    assert result.findings[0].source_ids == ["S1"]
    assert result.limitations


async def test_research_hallucinated_citations_fail():
    from contoso_agents.agents import _ResearchDraft

    backend = stub_backend()
    backend._web_factory = WebResources
    backend.researcher = StubAgent(
        _ResearchDraft(
            title="Unverified",
            summary="Unverified.",
            findings=[Finding(text="Invented.", source_ids=["S99"])],
        )
    )
    with pytest.raises(ValueError, match="unknown source"):
        await backend.research(state(), "Research Python")


async def test_failed_evidence_never_becomes_model_memory_claims():
    from contoso_agents.agents import _ResearchDraft

    backend = stub_backend()
    backend._web_factory = WebResources
    backend.researcher = StubAgent(
        _ResearchDraft(
            title="Unverified topic",
            summary="A model-generated unsourced claim.",
            findings=[Finding(text="A model-generated unsourced finding.")],
        )
    )
    result = await backend.research(state(), "Research topic")
    assert result.sources == []
    assert "model-generated" not in result.summary
    assert "model-generated" not in result.findings[0].text
    assert "No verifiable" in result.summary


async def test_genuine_maf_structured_run_invokes_secure_web_tool_offline():
    from agent_framework.openai import OpenAIChatCompletionClient
    from openai import AsyncOpenAI

    requests = []

    def model_handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert any("executive audience" in str(message) for message in payload["messages"])
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_search",
                        "type": "function",
                        "function": {"name": "search_web", "arguments": '{"query":"Python"}'},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            assert any(message["role"] == "tool" for message in payload["messages"])
            assert "Actual tool evidence." in json.dumps(payload["messages"])
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "title": "Python",
                        "summary": "A tool-grounded summary.",
                        "findings": [{"text": "Actual tool evidence.", "source_ids": ["S1"]}],
                        "limitations": ["A bounded review."],
                    }
                ),
            }
            finish = "stop"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-offline",
                "object": "chat.completion",
                "created": 1,
                "model": "offline-test-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def page_handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Actual tool page</title><p>Actual tool evidence.</p>",
        )

    model_http = httpx.AsyncClient(transport=httpx.MockTransport(model_handler))
    web_http = httpx.AsyncClient(transport=httpx.MockTransport(page_handler))
    async with model_http, web_http:
        model = AsyncOpenAI(api_key="offline-test", http_client=model_http, max_retries=0)
        backend = LiveAgents(
            client=OpenAIChatCompletionClient(model="offline-test-model", async_client=model),
            web_factory=lambda: WebResources(
                client=web_http,
                resolver=lambda host: ["93.184.215.14"],
                urls=["https://docs.python.org/3/"],
            ),
        )
        result = await backend.research(state(), "Research Python")
    assert len(requests) == 2
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert result.sources[0].title == "Actual tool page"
    assert result.findings[0].source_ids == ["S1"]
