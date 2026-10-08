from uuid import uuid4

import pytest
from pydantic import ValidationError

from contoso_agents.models import (
    ArtifactRef,
    DeckSpec,
    Finding,
    ResearchDocument,
    SessionState,
    SlideSpec,
    SourceRecord,
    Turn,
)


def artifact(session: str, filename="research.json") -> ArtifactRef:
    return ArtifactRef(
        key=f"sessions/{session}/artifacts/{uuid4()}/{filename}",
        filename=filename,
        content_type="application/json",
        size=1,
        sha256="a" * 64,
    )


def test_schema_rejects_traversal_and_mismatched_filenames():
    base = artifact(str(uuid4())).model_dump()
    for key in ("../research.json", "C:\\research.json", "/research.json", "a//research.json"):
        with pytest.raises(ValidationError):
            ArtifactRef.model_validate({**base, "key": key})
    with pytest.raises(ValidationError, match="filename"):
        ArtifactRef.model_validate({**base, "filename": "other.json"})


def test_session_rejects_cross_session_artifact_history():
    with pytest.raises(ValidationError, match="another session"):
        SessionState(
            session_id=str(uuid4()),
            turns=[Turn(role="assistant", content="Saved", artifacts=[artifact(str(uuid4()))])],
        )


def test_unknown_citations_fail_in_research_and_deck():
    with pytest.raises(ValidationError, match="unknown source"):
        ResearchDocument(
            title="Topic",
            summary="Claim",
            findings=[Finding(text="Claim", source_ids=["S1"])],
        )
    with pytest.raises(ValidationError, match="unknown source"):
        DeckSpec(
            title="Topic", slides=[SlideSpec(title="Claim", bullets=["Claim"], source_ids=["S1"])]
        )


def test_sources_do_not_accept_credential_urls():
    with pytest.raises(ValidationError, match="HTTPS"):
        SourceRecord(
            id="S1", title="Source", url="https://token@domain.test/page", excerpt="Evidence"
        )


@pytest.mark.parametrize("model", [Finding, SlideSpec])
def test_citation_lists_reject_duplicates_and_unbounded_lengths(model):
    fields = {"text": "Evidence"} if model is Finding else {"title": "Topic", "bullets": ["Fact"]}
    with pytest.raises(ValidationError, match="unique"):
        model(**fields, source_ids=["S1", "S1"])
    with pytest.raises(ValidationError, match="at most 12"):
        model(**fields, source_ids=[f"S{i}" for i in range(1, 14)])
    assert len(model(**fields, source_ids=[f"S{i}" for i in range(1, 13)]).source_ids) == 12
