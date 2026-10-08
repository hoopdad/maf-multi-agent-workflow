"""Editable, paginated 16:9 slides using an illustrative Contoso theme."""

from __future__ import annotations

import io
import math
import unicodedata
from collections.abc import Sequence

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

from ...models import DeckSpec, ResearchDocument, SlideSpec

WIDTH = 13.333333
HEIGHT = 7.5
NAVY = "172B3A"
TEAL = "007F82"
MINT = "80DFCB"
PAPER = "F3F6F5"
INK = "203543"
WHITE = "FFFFFF"
MUTED = "526773"


def _escape(text: str) -> str:
    for character in "\\`*_{}[]<>()#|":
        text = text.replace(character, "\\" + character)
    return text


def research_markdown(doc: ResearchDocument) -> str:
    """Formats stored research as readable Markdown with citations and evidence limitations."""
    doc = ResearchDocument.model_validate(doc.model_dump())
    lines = [f"# {_escape(doc.title)}", "", _escape(doc.summary), "", "## Findings", ""]
    for finding in doc.findings:
        citations = " ".join(f"[{source_id}]" for source_id in finding.source_ids)
        lines.append(f"- {_escape(finding.text)}" + (f" {citations}" if citations else ""))
    lines.extend(["", "## Sources", ""])
    if not doc.sources:
        lines.append("No external sources were retrieved.")
    for source in doc.sources:
        lines.extend(
            [
                f"### [{source.id}] {_escape(source.title)}",
                f"- URL: {source.url}",
                f"- Retrieved: {_escape(source.retrieved_at)}",
                f"- Evidence excerpt: {_escape(source.excerpt)}",
                "",
            ]
        )
    lines.extend(["", "## Evidence limitations", ""])
    lines.extend(f"- {_escape(item)}" for item in doc.limitations)
    if not doc.limitations:
        lines.append("- Evidence is limited to the retrieved excerpts, not an exhaustive review.")
    return "\n".join(lines).rstrip() + "\n"


def _units(text: str) -> float:
    # Conservative profiles for the declared Calibri/Trebuchet faces, not monospace widths.
    total = 0.0
    for character in text:
        if character == "\t":
            total += 3.0
        elif unicodedata.east_asian_width(character) in {"W", "F"}:
            total += 1.3
        elif character.isspace():
            total += 0.35
        elif character in "iIl!.,:;'|`":
            total += 0.4
        elif character in "MWmw@%&":
            total += 1.05
        elif ord(character) > 127:
            total += 1.1
        elif character.isupper():
            total += 0.85
        else:
            total += 0.65
    return total


def _wrapped_lines(text: str, width: float, size: int) -> list[str]:
    capacity = width * 72 / size
    output: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            output.append("")
            continue
        remaining = paragraph
        while remaining:
            count = 0
            units = 0.0
            for char in remaining:
                increment = _units(char)
                if units + increment > capacity and count:
                    break
                units += increment
                count += 1
            if count < len(remaining):
                space = remaining.rfind(" ", 0, count + 1)
                if space > count // 2:
                    count = space + 1
            output.append(remaining[:count])
            remaining = remaining[count:]
    return output


def _height(text: str, width: float, size: int) -> float:
    return len(_wrapped_lines(text, width, size)) * size * 1.25 / 72 + 0.12


def _text(
    slide,
    text: str,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    size: int,
    color: str = INK,
    bold: bool = False,
    heading: bool = False,
    align=PP_ALIGN.LEFT,
):
    if _height(text, width, size) > height + 0.01:
        raise ValueError("Text does not fit its reserved layout; pagination is required")
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(width), Inches(height))
    frame = box.text_frame
    frame.clear()
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0
    frame.vertical_anchor = MSO_ANCHOR.TOP
    for index, line in enumerate(text.split("\n")):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.text = line
        paragraph.alignment = align
        for font in (paragraph.font, *(run.font for run in paragraph.runs)):
            font.name = "Trebuchet MS" if heading else "Calibri"
            font.size = Pt(size)
            font.bold = bold
            font.color.rgb = RGBColor.from_string(color)
        paragraph.line_spacing = 1.15
        paragraph.space_before = paragraph.space_after = Pt(0)
    return box


def _shape(slide, kind, x, y, width, height, fill: str):
    shape = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(width), Inches(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(fill)
    shape.line.fill.background()
    return shape


def _base(presentation, dark: bool):
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor.from_string(NAVY if dark else PAPER)
    _shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, 11.95, 0.55, 0.65, 0.65, TEAL)
    _shape(slide, MSO_SHAPE.OVAL, 12.14, 0.74, 0.27, 0.27, MINT)
    return slide


def _footer(slide, page: int, total: int, dark: bool, source_ids: Sequence[str] = ()):
    color = "C2D9DC" if dark else MUTED
    if source_ids:
        _text(
            slide,
            "Sources: " + ", ".join(source_ids),
            0.65,
            6.52,
            11.7,
            0.3,
            size=10,
            color=color,
        )
    _text(
        slide,
        "CONTOSO  /  Illustrative theme — not official brand assets",
        0.65,
        6.96,
        10.1,
        0.28,
        size=9,
        color=color,
    )
    _text(
        slide,
        f"{page} / {total}",
        11.15,
        6.96,
        1.55,
        0.28,
        size=9,
        color=color,
        align=PP_ALIGN.RIGHT,
    )


def _content_pages(spec: SlideSpec, available: float) -> list[list[str]]:
    pages: list[list[str]] = []
    current: list[str] = []
    used = 0.0
    for bullet in spec.bullets:
        height = max(0.67, _height(bullet, 10.55, 20) + 0.25)
        if height > available:
            raise ValueError("A slide bullet exceeds the readable rendering capacity")
        if current and used + 0.16 + height > available:
            pages.append(current)
            current, used = [], 0.0
        used += (0.16 if current else 0.0) + height
        current.append(bullet)
    if current:
        pages.append(current)
    return pages


def render_deck(spec: DeckSpec) -> bytes:
    """Renders a validated deck as editable Contoso PPTX bytes with pagination and citations.

    The output contains a cover, every supplied content slide (possibly continued),
    and readable source appendix pages. Supplied notes are retained verbatim.
    Source excerpts are preserved in appendix speaker notes rather than dense text.
    """
    spec = DeckSpec.model_validate(spec.model_dump())
    presentation = Presentation()
    presentation.slide_width = Inches(WIDTH)
    presentation.slide_height = Inches(HEIGHT)
    presentation.core_properties.title = spec.title
    presentation.core_properties.subject = "Illustrative Contoso presentation"
    presentation.core_properties.author = "Contoso multi-agent sample"
    cover = _base(presentation, True)
    _text(
        cover, "CONTOSO  /  RESEARCH & IDEAS", 0.65, 0.65, 10.6, 0.4, size=14, color=MINT, bold=True
    )
    title_size = next(
        (size for size in (36, 34, 32, 30) if _height(spec.title, 11.7, size) <= 3.1),
        None,
    )
    if title_size is None:
        raise ValueError("Cover title exceeds the readable layout capacity")
    _text(
        cover,
        spec.title,
        0.65,
        1.3,
        11.7,
        3.1,
        size=title_size,
        color=WHITE,
        bold=True,
        heading=True,
    )
    if spec.subtitle:
        _text(cover, spec.subtitle, 0.65, 4.65, 11.7, 1.9, size=20, color="D7EBEC")
    cover.notes_slide.notes_text_frame.text = (
        "Illustrative Contoso theme. This sample does not use official brand assets."
    )
    slide_citations: list[list[str]] = [[]]
    dark_slides = [True]
    for slide_index, item in enumerate(spec.slides):
        title_height = max(1.35, _height(item.title, 10.9, 30))
        body_y = max(2.2, 0.58 + title_height + 0.55)
        pages = _content_pages(item, 6.2 - body_y)
        for page_index, bullets in enumerate(pages):
            dark = slide_index == len(spec.slides) - 1
            slide = _base(presentation, dark)
            _text(
                slide,
                item.title,
                0.65,
                0.58,
                10.9,
                title_height,
                size=30,
                color=WHITE if dark else INK,
                bold=True,
                heading=True,
            )
            if len(pages) > 1:
                _text(
                    slide,
                    f"Part {page_index + 1} of {len(pages)}",
                    0.65,
                    body_y - 0.35,
                    10.9,
                    0.3,
                    size=10,
                    color=MINT if dark else MUTED,
                )
            y = body_y
            for bullet in bullets:
                height = max(0.67, _height(bullet, 10.55, 20) + 0.25)
                _shape(
                    slide,
                    MSO_SHAPE.ROUNDED_RECTANGLE,
                    0.65,
                    y,
                    12.05,
                    height,
                    "24434F" if dark else WHITE,
                )
                _shape(slide, MSO_SHAPE.OVAL, 0.92, y + 0.19, 0.28, 0.28, MINT if dark else TEAL)
                _text(
                    slide,
                    bullet,
                    1.55,
                    y + 0.12,
                    10.55,
                    height - 0.22,
                    size=20,
                    color=WHITE if dark else INK,
                )
                y += height + 0.16
            slide.notes_slide.notes_text_frame.text = item.notes
            slide_citations.append(item.source_ids)
            dark_slides.append(dark)
    for source in spec.sources:
        details = (
            f"[{source.id}] {source.title}\nURL: {source.url}\nRetrieved: {source.retrieved_at}"
        )
        lines = _wrapped_lines(details, 11.65, 16)
        per_page = math.floor((4.5 - 0.12) * 72 / (16 * 1.25))
        source_pages = math.ceil(len(lines) / per_page)
        for offset in range(0, len(lines), per_page):
            slide = _base(presentation, False)
            heading = f"Sources / {source.id}"
            if source_pages > 1:
                heading += f" · Part {offset // per_page + 1} of {source_pages}"
            _text(
                slide,
                heading,
                0.65,
                0.65,
                10.9,
                1.35,
                size=30,
                bold=True,
                heading=True,
            )
            _shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, 0.65, 1.8, 12.05, 4.65, WHITE)
            _text(
                slide,
                "\n".join(lines[offset : offset + per_page]),
                0.85,
                1.97,
                11.65,
                4.45,
                size=16,
            )
            slide.notes_slide.notes_text_frame.text = (
                f"[{source.id}] {source.title}\n{source.url}\n"
                f"Retrieved: {source.retrieved_at}\nEvidence excerpt:\n{source.excerpt}"
            )
            slide_citations.append([])
            dark_slides.append(False)
    for index, slide in enumerate(presentation.slides):
        _footer(
            slide, index + 1, len(presentation.slides), dark_slides[index], slide_citations[index]
        )
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()
