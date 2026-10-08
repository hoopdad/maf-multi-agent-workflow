"""A typed MAF graph; durable application sessions surround each workflow run."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Never
from uuid import UUID
from weakref import WeakKeyDictionary

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from .agents import AgentBackend
from .models import ArtifactRef, DeckSpec, RouteDecision, RunResult, SessionState, Turn, WorkerJob
from .storage import ArtifactStore
from .workers import WorkerDispatcher, put_json, read_json


@dataclass
class SessionGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_session_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, SessionGate]] = (
    WeakKeyDictionary()
)


@dataclass
class FlowMessage:
    state: SessionState
    prompt: str
    direct_ppt: bool = False
    decision: RouteDecision | None = None
    artifacts: list[ArtifactRef] = field(default_factory=list)

    def route(self) -> RouteDecision:
        if self.decision is None:
            raise ValueError("Workflow message has not been routed")
        return self.decision


class OrchestratorExecutor(Executor):
    def __init__(self, agents: AgentBackend):
        super().__init__(id="orchestrator")
        self.agents = agents

    @handler
    async def run(self, message: FlowMessage, ctx: WorkflowContext[FlowMessage]) -> None:
        message.decision = (
            RouteDecision(action="ppt", instruction=message.prompt, use_research=False)
            if message.direct_ppt
            else await self.agents.route(message.state, message.prompt)
        )
        await ctx.send_message(message)


class ResearchExecutor(Executor):
    def __init__(self, store: ArtifactStore, dispatcher: WorkerDispatcher):
        super().__init__(id="researcher")
        self.store = store
        self.dispatcher = dispatcher

    @handler
    async def run(self, message: FlowMessage, ctx: WorkflowContext[FlowMessage]) -> None:
        job = WorkerJob(
            session_id=message.state.session_id,
            kind="research",
            prompt=message.route().instruction,
            state_ref=put_json(self.store, message.state.session_id, "state.json", message.state),
        )
        result = await self.dispatcher.invoke(job)
        message.artifacts.extend(result.artifacts)
        message.state.latest_research = next(
            ref for ref in result.artifacts if ref.filename == "research.json"
        )
        await ctx.send_message(message)


class PptExecutor(Executor):
    def __init__(self, store: ArtifactStore, dispatcher: WorkerDispatcher):
        super().__init__(id="ppt")
        self.store = store
        self.dispatcher = dispatcher

    @handler
    async def run(self, message: FlowMessage, ctx: WorkflowContext[Never, RunResult]) -> None:
        decision = message.route()
        job = WorkerJob(
            session_id=message.state.session_id,
            kind="ppt",
            prompt=decision.instruction,
            state_ref=put_json(self.store, message.state.session_id, "state.json", message.state),
            research_ref=(
                message.state.latest_research
                if decision.use_research or decision.action == "research_ppt"
                else None
            ),
            deck_ref=message.state.latest_deck,
        )
        result = await self.dispatcher.invoke(job)
        message.artifacts.extend(result.artifacts)
        message.state.latest_deck = next(
            ref for ref in result.artifacts if ref.filename == "deck.json"
        )
        message.state.latest_pptx = next(
            ref for ref in result.artifacts if ref.filename == "presentation.pptx"
        )
        deck = read_json(self.store, message.state.latest_deck, DeckSpec)
        await ctx.yield_output(
            RunResult(
                session_id=message.state.session_id,
                action=decision.action,
                message=f"Created Contoso presentation: {deck.title}.",
                artifacts=message.artifacts,
                revision=message.state.revision + 1,
            )
        )


class AnswerExecutor(Executor):
    def __init__(self, store: ArtifactStore, agents: AgentBackend):
        super().__init__(id="answer")
        self.store = store
        self.agents = agents

    @handler
    async def run(self, message: FlowMessage, ctx: WorkflowContext[Never, RunResult]) -> None:
        from .models import ResearchDocument

        decision = message.route()
        if decision.action == "research":
            if message.state.latest_research is None:
                raise ValueError("Research executor did not return a research artifact")
            research = read_json(self.store, message.state.latest_research, ResearchDocument)
            answer = f"Research saved: {research.title}. {research.summary}"
        elif decision.message:
            answer = decision.message
        else:
            research = (
                read_json(self.store, message.state.latest_research, ResearchDocument)
                if message.state.latest_research
                else None
            )
            deck = (
                read_json(self.store, message.state.latest_deck, DeckSpec)
                if message.state.latest_deck
                else None
            )
            answer = await self.agents.clarify(message.state, message.prompt, research, deck)
        await ctx.yield_output(
            RunResult(
                session_id=message.state.session_id,
                action=decision.action,
                message=answer,
                artifacts=message.artifacts,
                revision=message.state.revision + 1,
            )
        )


def build_workflow(store: ArtifactStore, agents: AgentBackend, dispatcher: WorkerDispatcher):
    router = OrchestratorExecutor(agents)
    researcher = ResearchExecutor(store, dispatcher)
    ppt = PptExecutor(store, dispatcher)
    answer = AnswerExecutor(store, agents)
    return (
        WorkflowBuilder(start_executor=router)
        .add_edge(
            router, researcher, condition=lambda m: m.route().action in {"research", "research_ppt"}
        )
        .add_edge(router, ppt, condition=lambda m: m.route().action == "ppt")
        .add_edge(router, answer, condition=lambda m: m.route().action == "clarify")
        .add_edge(researcher, ppt, condition=lambda m: m.route().action == "research_ppt")
        .add_edge(researcher, answer, condition=lambda m: m.route().action == "research")
        .build()
    )


async def invoke(
    store: ArtifactStore,
    agents: AgentBackend,
    dispatcher: WorkerDispatcher,
    session_id: str,
    prompt: str,
    *,
    direct_ppt: bool = False,
) -> RunResult:
    session_id = str(UUID(session_id))
    if not prompt.strip() or len(prompt) > 8000:
        raise ValueError("Prompt must contain 1-8000 characters")
    # File/Blob locks are thread-reentrant; asynchronous tasks also need a task-level gate.
    locks = _session_locks.setdefault(asyncio.get_running_loop(), {})
    gate = locks.setdefault(session_id, SessionGate())
    gate.users += 1
    try:
        async with gate.lock:
            with store.lock_session(session_id):
                state = store.load_session(session_id) or SessionState(session_id=session_id)
                message = FlowMessage(state=state, prompt=prompt.strip(), direct_ppt=direct_ppt)
                workflow = build_workflow(store, agents, dispatcher)
                result = await workflow.run(message)
                outputs = result.get_outputs()
                if len(outputs) != 1 or not isinstance(outputs[0], RunResult):
                    raise RuntimeError("MAF workflow did not produce exactly one RunResult")
                output = outputs[0]
                state.turns.extend(
                    [
                        Turn(role="user", content=prompt),
                        Turn(role="assistant", content=output.message, artifacts=output.artifacts),
                    ]
                )
                state.revision = output.revision
                store.save_session(state)
                return output
    finally:
        gate.users -= 1
        if gate.users == 0:
            del locks[session_id]
