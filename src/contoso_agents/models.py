"""Versioned contracts shared by the orchestrator and independent workers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    """Supplies UTC timestamps for conversation turns, web evidence, and HTTP timing logs."""
    return datetime.now(UTC).isoformat()


def unique_citations(values: list[str]) -> list[str]:
    """Rejects duplicate source IDs when validating research findings and slide citations."""
    if len(values) != len(set(values)):
        raise ValueError("Citation IDs must be unique")
    return values


class Contract(BaseModel):
    """Applies shared validation rules to data exchanged across workflow and worker boundaries."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ArtifactRef(Contract):
    """Identifies a stored artifact so workers can retrieve and verify its exact bytes."""

    schema_version: Literal[1] = 1
    key: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    content_type: str = Field(min_length=1, max_length=128)
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    @field_validator("key")
    @classmethod
    def safe_key(cls, value: str) -> str:
        parts = value.split("/")
        if "\\" in value or any(p in {"", ".", ".."} for p in parts) or ":" in value:
            raise ValueError("Artifact key must be a relative, traversal-free storage key")
        return value

    def belongs_to(self, session_id: str) -> bool:
        return self.key.startswith(f"sessions/{session_id}/artifacts/")

    @model_validator(mode="after")
    def filename_matches_key(self) -> ArtifactRef:
        if self.key.split("/")[-1] != self.filename:
            raise ValueError("Artifact filename must match its storage key")
        return self


class Turn(Contract):
    """Records a committed user or assistant message with its timestamp and artifact references."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)
    created_at: str = Field(default_factory=utc_now)
    artifacts: list[ArtifactRef] = Field(default_factory=list)


class SessionState(Contract):
    """Carries committed history, revision, and artifact references into the next workflow turn."""

    schema_version: Literal[1] = 1
    session_id: str
    revision: int = Field(default=0, ge=0)
    turns: list[Turn] = Field(default_factory=list)
    latest_research: ArtifactRef | None = None
    latest_deck: ArtifactRef | None = None
    latest_pptx: ArtifactRef | None = None

    @field_validator("session_id")
    @classmethod
    def valid_session_id(cls, value: str) -> str:
        return str(UUID(value))

    @model_validator(mode="after")
    def own_artifacts(self) -> SessionState:
        for ref in (self.latest_research, self.latest_deck, self.latest_pptx):
            if ref is not None and not ref.belongs_to(self.session_id):
                raise ValueError("Session cannot reference another session's artifact")
        for turn in self.turns:
            if any(not ref.belongs_to(self.session_id) for ref in turn.artifacts):
                raise ValueError("Turn cannot reference another session's artifact")
        return self


class SourceRecord(Contract):
    """Keeps retrieved web evidence and source IDs together for research and slide citations."""

    id: str = Field(pattern=r"^S[1-9][0-9]*$")
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)
    excerpt: str = Field(min_length=1, max_length=6000)
    retrieved_at: str = Field(default_factory=utc_now)

    @field_validator("url")
    @classmethod
    def https_source(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Source URL must use HTTPS without embedded credentials")
        return value


class Finding(Contract):
    """Links a research finding to the retrieved source IDs that support it."""

    text: str = Field(min_length=1, max_length=1000)
    source_ids: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("source_ids")
    @classmethod
    def citation_ids(cls, values: list[str]) -> list[str]:
        return unique_citations(values)


class ResearchDocument(Contract):
    """Carries findings, evidence, and limitations from the researcher to the PPT worker."""

    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[Finding] = Field(min_length=1, max_length=20)
    sources: list[SourceRecord] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_citations(self) -> ResearchDocument:
        ids = [source.id for source in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("Source IDs must be unique")
        for finding in self.findings:
            if not set(finding.source_ids).issubset(ids):
                raise ValueError("Finding contains an unknown source ID")
        return self


class SlideSpec(Contract):
    """Describes one editable slide's content, citations, and speaker notes for the renderer."""

    title: str = Field(min_length=1, max_length=80)
    bullets: list[str] = Field(min_length=1, max_length=5)
    source_ids: list[str] = Field(default_factory=list, max_length=12)
    notes: str = Field(default="", max_length=2000)

    @field_validator("source_ids")
    @classmethod
    def citation_ids(cls, values: list[str]) -> list[str]:
        return unique_citations(values)

    @field_validator("bullets")
    @classmethod
    def bounded_bullets(cls, values: list[str]) -> list[str]:
        if any(not text.strip() or len(text) > 200 for text in values):
            raise ValueError("Each bullet must contain 1-200 characters")
        return values


class DeckSpec(Contract):
    """Carries validated slides and trusted sources into rendering and later revisions."""

    title: str = Field(min_length=1, max_length=100)
    subtitle: str = Field(default="", max_length=160)
    slides: list[SlideSpec] = Field(min_length=1, max_length=12)
    sources: list[SourceRecord] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def valid_citations(self) -> DeckSpec:
        ids = [source.id for source in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("Deck source IDs must be unique")
        for slide in self.slides:
            if not set(slide.source_ids).issubset(ids):
                raise ValueError("Slide contains an unknown source ID")
        return self


Action = Literal["research", "ppt", "research_ppt", "clarify"]


class RouteDecision(Contract):
    """Tells the workflow which worker or answer path to run and what instruction to pass along."""

    action: Action
    instruction: str = Field(min_length=1, max_length=8000)
    use_research: bool = False
    message: str = Field(default="", max_length=4000)


class WorkerJob(Contract):
    """Hands a worker its task and references to the session, research, and prior deck artifacts."""

    schema_version: Literal[1] = 1
    session_id: str
    kind: Literal["research", "ppt"]
    prompt: str = Field(min_length=1, max_length=8000)
    state_ref: ArtifactRef
    research_ref: ArtifactRef | None = None
    deck_ref: ArtifactRef | None = None

    @field_validator("session_id")
    @classmethod
    def valid_session_id(cls, value: str) -> str:
        return str(UUID(value))

    @model_validator(mode="after")
    def own_inputs(self) -> WorkerJob:
        for ref in (self.state_ref, self.research_ref, self.deck_ref):
            if ref is not None and not ref.belongs_to(self.session_id):
                raise ValueError("Worker inputs must belong to the job's session")
        return self


class WorkerResult(Contract):
    """Returns a worker's artifact references so the orchestrator can continue the workflow."""

    session_id: str
    kind: Literal["research", "ppt"]
    artifacts: list[ArtifactRef] = Field(min_length=1)


class RunResult(Contract):
    """Returns the turn's action, reply, artifacts, and committed revision to the CLI or API."""

    session_id: str
    action: Action
    message: str = Field(min_length=1)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    revision: int = Field(ge=0)
