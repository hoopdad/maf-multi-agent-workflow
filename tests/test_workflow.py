import asyncio
from uuid import uuid4

import pytest

from contoso_agents.models import (
    DeckSpec,
    Finding,
    ResearchDocument,
    RouteDecision,
    SlideSpec,
)
from contoso_agents.storage import LocalStore
from contoso_agents.workers import InProcessDispatcher
from contoso_agents.workflow import invoke


class StubAgents:
    def __init__(self, action="research_ppt"):
        self.action = action
        self.research_seen = None
        self.previous_seen = None
        self.history_seen = None
        self.fail = False

    async def route(self, state, prompt):
        self.history_seen = list(state.turns)
        return RouteDecision(action=self.action, instruction=prompt, use_research=True)

    async def research(self, state, prompt):
        if self.fail:
            raise RuntimeError("Research failed explicitly")
        return ResearchDocument(
            title="Verified topic",
            summary="Stubbed research for workflow tests, not web evidence.",
            findings=[Finding(text="A supplied fact.")],
            limitations=["No live web evidence in this stub."],
        )

    async def plan_deck(self, state, prompt, research, previous):
        self.research_seen = research
        self.previous_seen = previous
        return DeckSpec(title=prompt[:100], slides=[SlideSpec(title="Overview", bullets=["Fact"])])

    async def clarify(self, state, prompt, research, previous):
        self.history_seen = list(state.turns)
        return "Clarified the saved item without generating a new presentation."


async def test_real_maf_graph_research_handoff_then_fresh_session_revision(tmp_path):
    session = str(uuid4())
    store = LocalStore(tmp_path)
    agents = StubAgents()
    first = await invoke(
        store, agents, InProcessDispatcher(store, agents), session, "Initial topic"
    )
    assert first.action == "research_ppt"
    assert len(first.artifacts) == 4
    assert agents.research_seen.title == "Verified topic"
    saved = store.load_session(session)
    assert saved.revision == 1
    assert len(saved.turns) == 2
    assert len(saved.turns[-1].artifacts) == 4

    fresh_store = LocalStore(tmp_path)
    fresh_agents = StubAgents(action="ppt")
    second = await invoke(
        fresh_store,
        fresh_agents,
        InProcessDispatcher(fresh_store, fresh_agents),
        session,
        "Add a cost slide",
    )
    assert second.revision == 2
    assert fresh_agents.previous_seen.title == "Initial topic"
    assert len(fresh_agents.history_seen) == 2
    assert first.artifacts[-1].key != second.artifacts[-1].key
    assert fresh_store.read_artifact(first.artifacts[-1]).startswith(b"PK")
    assert len(fresh_store.load_session(session).turns) == 4


async def test_direct_ppt_bypasses_orchestrator_and_research(tmp_path):
    class DirectOnly(StubAgents):
        async def route(self, state, prompt):
            raise AssertionError("Direct PPT must not call model router")

        async def research(self, state, prompt):
            raise AssertionError("Direct PPT must not call researcher")

    store = LocalStore(tmp_path)
    agents = DirectOnly()
    result = await invoke(
        store,
        agents,
        InProcessDispatcher(store, agents),
        str(uuid4()),
        "Topic",
        direct_ppt=True,
    )
    assert result.action == "ppt"
    assert len(result.artifacts) == 2
    assert agents.research_seen is None


async def test_clarification_keeps_all_history_without_ppt(tmp_path):
    store = LocalStore(tmp_path)
    agents = StubAgents(action="clarify")
    session = str(uuid4())
    result = await invoke(
        store, agents, InProcessDispatcher(store, agents), session, "What is missing?"
    )
    assert result.artifacts == []
    assert result.action == "clarify"
    assert len(store.load_session(session).turns) == 2


async def test_failed_turn_does_not_overwrite_committed_state(tmp_path):
    store = LocalStore(tmp_path)
    agents = StubAgents(action="research")
    session = str(uuid4())
    await invoke(store, agents, InProcessDispatcher(store, agents), session, "First")
    before = store.load_session(session).model_dump()
    agents.fail = True
    with pytest.raises(Exception, match="Research failed explicitly"):
        await invoke(store, agents, InProcessDispatcher(store, agents), session, "Fail now")
    assert store.load_session(session).model_dump() == before


async def test_invalid_prompt_and_session_are_rejected(tmp_path):
    store = LocalStore(tmp_path)
    agents = StubAgents()
    dispatcher = InProcessDispatcher(store, agents)
    with pytest.raises(ValueError):
        await invoke(store, agents, dispatcher, "not-a-uuid", "Topic")
    with pytest.raises(ValueError, match="Prompt"):
        await invoke(store, agents, dispatcher, str(uuid4()), " ")


async def test_same_session_async_tasks_are_serialized_before_loading_state(tmp_path):
    class DelayedAgents(StubAgents):
        async def research(self, state, prompt):
            await asyncio.sleep(0.02)
            return await super().research(state, prompt)

    session = str(uuid4())
    first_store, second_store = LocalStore(tmp_path), LocalStore(tmp_path)
    first_agents, second_agents = DelayedAgents("research"), DelayedAgents("research")
    results = await asyncio.gather(
        invoke(
            first_store,
            first_agents,
            InProcessDispatcher(first_store, first_agents),
            session,
            "First",
        ),
        invoke(
            second_store,
            second_agents,
            InProcessDispatcher(second_store, second_agents),
            session,
            "Second",
        ),
    )
    assert [result.revision for result in results] == [1, 2]
    assert len(second_agents.history_seen) == 2
    assert len(first_store.load_session(session).turns) == 4
