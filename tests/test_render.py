import io
from zipfile import ZipFile

import pytest
from pptx import Presentation
from pptx.enum.text import PP_ALIGN
from pydantic import ValidationError

from contoso_agents.agents.pptx import render_deck, research_markdown
from contoso_agents.models import DeckSpec, Finding, ResearchDocument, SlideSpec, SourceRecord


def source(number=1, *, url=None):
    return SourceRecord(
        id=f"S{number}",
        title=f"Verified source {number}",
        url=url or f"https://docs.python.org/3/source-{number}",
        excerpt=f"Evidence excerpt {number}.",
        retrieved_at="2026-10-08T15:00:00+00:00",
    )


def text(slide):
    return "\n".join(shape.text for shape in slide.shapes if shape.has_text_frame)


def test_editable_wide_theme_titles_notes_and_real_citations():
    spec = DeckSpec(
        title="Responsible AI",
        subtitle="A grounded Contoso discussion",
        slides=[
            SlideSpec(
                title="Evidence",
                bullets=["One grounded point.", "Another point."],
                notes="Keep these exact speaker notes.",
                source_ids=["S1"],
            ),
            SlideSpec(title="Conclusion", bullets=["Review the evidence before deciding."]),
        ],
        sources=[source()],
    )
    content = render_deck(spec)
    with ZipFile(io.BytesIO(content)) as archive:
        assert archive.testzip() is None
        assert "ppt/slides/slide1.xml" in archive.namelist()
        assert any(path.startswith("ppt/notesSlides/notesSlide") for path in archive.namelist())
    deck = Presentation(io.BytesIO(content))
    assert abs(deck.slide_width / deck.slide_height - 16 / 9) < 0.001
    assert deck.core_properties.title == spec.title
    assert spec.title in text(deck.slides[0])
    assert "One grounded point." in text(deck.slides[1])
    assert "Sources: S1" in text(deck.slides[1])
    assert deck.slides[1].notes_slide.notes_text_frame.text == spec.slides[0].notes
    assert source().url in text(deck.slides[3])
    assert source().excerpt in deck.slides[3].notes_slide.notes_text_frame.text
    assert deck.slides[0].background.fill.fore_color.rgb != (
        deck.slides[1].background.fill.fore_color.rgb
    )
    assert deck.slides[2].background.fill.fore_color.rgb == (
        deck.slides[0].background.fill.fore_color.rgb
    )
    assert not any(shape.shape_type == 13 for slide in deck.slides for shape in slide.shapes)


def test_explicit_run_styles_match_effective_paragraph_styles_for_preview_interoperability():
    spec = DeckSpec(
        title="Contoso style proof",
        slides=[
            SlideSpec(title="Evidence", bullets=["A light-slide content card."]),
            SlideSpec(title="Conclusion", bullets=["A white-on-dark conclusion card."]),
        ],
    )
    deck = Presentation(io.BytesIO(render_deck(spec)))
    for slide in deck.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    assert run.font.name == paragraph.font.name
                    assert run.font.size == paragraph.font.size
                    assert run.font.bold == paragraph.font.bold
                    assert run.font.color.rgb == paragraph.font.color.rgb
    conclusion = next(
        shape
        for shape in deck.slides[2].shapes
        if shape.has_text_frame and shape.text == spec.slides[1].bullets[0]
    )
    run = conclusion.text_frame.paragraphs[0].runs[0]
    assert run.font.size.pt == 20
    assert str(run.font.color.rgb) == "FFFFFF"


def test_many_sources_dense_long_text_paginate_without_loss_or_tiny_text():
    sources = [source(number) for number in range(1, 13)]
    spec = DeckSpec(
        title="W" * 100,
        subtitle="W" * 160,
        slides=[
            SlideSpec(
                title="界" * 80,
                bullets=[f"{index}: " + "界" * 196 for index in range(5)],
                source_ids=[item.id for item in sources],
                notes="Long-content notes preserved.",
            )
        ],
        sources=sources,
    )
    deck = Presentation(io.BytesIO(render_deck(spec)))
    combined = "\n".join(text(slide) for slide in deck.slides)
    for bullet in spec.slides[0].bullets:
        assert bullet in combined
    for item in sources:
        assert item.url in combined and item.title in combined
    assert len(deck.slides) > 14
    for slide in deck.slides:
        for shape in slide.shapes:
            assert shape.left >= 0 and shape.top >= 0
            assert shape.left + shape.width <= deck.slide_width
            assert shape.top + shape.height <= deck.slide_height
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    if paragraph.text:
                        assert paragraph.font.size.pt >= 9
                        if paragraph.text not in combined.splitlines()[-2:]:
                            assert shape.width > 0


def test_long_source_url_is_not_truncated():
    url = "https://docs.python.org/" + "abcdef" * 330
    item = source(url=url)
    spec = DeckSpec(
        title="Long evidence URL",
        slides=[SlideSpec(title="Point", bullets=["Cited evidence."], source_ids=["S1"])],
        sources=[item],
    )
    deck = Presentation(io.BytesIO(render_deck(spec)))
    source_slides = [slide for slide in deck.slides if "Sources / S1" in text(slide)]
    assert len(source_slides) >= 2
    for index, slide in enumerate(source_slides):
        assert f"Sources / S1 · Part {index + 1} of {len(source_slides)}" in text(slide)
    combined = "".join(
        shape.text.replace("\n", "")
        for slide in source_slides
        for shape in slide.shapes
        if shape.has_text_frame and abs(shape.top / 914400 - 1.97) < 0.01
    )
    assert "abcdef" * 330 in combined
    assert item.url in source_slides[0].notes_slide.notes_text_frame.text


def test_maximum_contract_widths_render_more_than_one_hundred_output_slides():
    prefix = "https://docs.python.org/"
    url = prefix + "W" * (2048 - len(prefix))
    sources = [
        source(number, url=url).model_copy(update={"title": "W" * 200}) for number in range(1, 13)
    ]
    spec = DeckSpec(
        title="W" * 100,
        subtitle="W" * 160,
        slides=[
            SlideSpec(
                title="W" * 80,
                bullets=["W" * 200] * 5,
                source_ids=[item.id for item in sources],
            )
            for _ in range(12)
        ],
        sources=sources,
    )
    deck = Presentation(io.BytesIO(render_deck(spec)))
    assert len(deck.slides) >= 100
    for page in (1, 10, 99, 100, len(deck.slides)):
        counter = next(
            shape
            for shape in deck.slides[page - 1].shapes
            if shape.has_text_frame and shape.text == f"{page} / {len(deck.slides)}"
        )
        assert counter.text_frame.paragraphs[0].alignment == PP_ALIGN.RIGHT
        assert counter.left + counter.width <= deck.slide_width
        assert counter.top + counter.height <= deck.slide_height
        assert counter.width / 914400 >= 1.5


def test_ordinary_source_url_uses_the_available_width_instead_of_monospace_wrapping():
    url = "https://docs.python.org/3/library/asyncio.html?highlight=asyncio#module-asyncio"
    spec = DeckSpec(
        title="Source layout",
        slides=[SlideSpec(title="Point", bullets=["A layout-only fixture."])],
        sources=[source(url=url)],
    )
    deck = Presentation(io.BytesIO(render_deck(spec)))
    appendix_text = text(deck.slides[-1])
    assert f"URL: {url}" in appendix_text


def test_invalid_citations_rejected_even_for_unvalidated_model_construct():
    slide = SlideSpec.model_construct(
        title="Invalid", bullets=["Point"], source_ids=["S9"], notes=""
    )
    spec = DeckSpec.model_construct(title="Invalid", subtitle="", slides=[slide], sources=[])
    with pytest.raises(ValidationError):
        render_deck(spec)


def test_markdown_contains_grounded_sources_excerpts_and_limitations():
    doc = ResearchDocument(
        title="Python <script>",
        summary="A grounded summary.",
        findings=[Finding(text="An evidence-backed finding.", source_ids=["S1"])],
        sources=[source()],
        limitations=["A bounded review, not exhaustive."],
    )
    result = research_markdown(doc)
    assert "[S1]" in result and source().url in result and source().excerpt in result
    assert "A bounded review, not exhaustive." in result
    assert "\\<script\\>" in result
    assert "Retrieved:" in result


def test_no_sources_markdown_is_explicit():
    doc = ResearchDocument(
        title="Offline",
        summary="No web calls.",
        findings=[Finding(text="User supplied topic.")],
    )
    assert "No external sources were retrieved." in research_markdown(doc)
