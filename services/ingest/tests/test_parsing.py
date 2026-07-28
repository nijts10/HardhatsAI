"""Integration tests against real PDF bytes (built with reportlab) rather
than mocked pdfplumber objects, so the font-size heading heuristic and table
detection are exercised against an actual text layer and ruled table."""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib import colors

from ingest.parsing import parse_pdf


def _build_pdf(*, include_table: bool) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    heading_style = ParagraphStyle("Heading", fontSize=20, leading=24)
    body_style = ParagraphStyle("Body", fontSize=10, leading=12)

    story = [
        Paragraph("Technische gegevens", heading_style),
        Spacer(1, 12),
        Paragraph("Dit product heeft EI 60 volgens NEN-EN 13501-2.", body_style),
        Spacer(1, 12),
    ]
    if include_table:
        table = Table([["Type", "EI"], ["A", "60"], ["B", "90"]])
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
        story.append(table)

    doc.build(story)
    return buf.getvalue()


def test_parse_pdf_extracts_text_and_font_sizes():
    pdf_bytes = _build_pdf(include_table=False)
    pages = parse_pdf(pdf_bytes)

    assert len(pages) == 1
    page = pages[0]
    assert "EI 60" in page.text
    assert not page.used_vision_fallback

    heading_lines = [l for l in page.lines if "Technische gegevens" in l.text]
    body_lines = [l for l in page.lines if "EI 60" in l.text]
    assert heading_lines and body_lines
    assert heading_lines[0].avg_font_size > body_lines[0].avg_font_size


def test_parse_pdf_extracts_ruled_table():
    pdf_bytes = _build_pdf(include_table=True)
    pages = parse_pdf(pdf_bytes)

    page = pages[0]
    assert len(page.tables) == 1
    rows = page.tables[0].rows
    assert rows[0] == ["Type", "EI"]
    assert rows[1] == ["A", "60"]
    assert rows[2] == ["B", "90"]


def test_parse_pdf_falls_back_to_vision_for_sparse_text_layer():
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    # A near-empty page: below MIN_TEXT_LAYER_CHARS, so vision kicks in.
    doc.build([Paragraph("Hi", ParagraphStyle("Tiny", fontSize=6))])
    pdf_bytes = buf.getvalue()

    calls = []

    def fake_vision_extractor(png_bytes: bytes) -> str:
        calls.append(png_bytes)
        return "vision-transcribed text"

    pages = parse_pdf(pdf_bytes, vision_extractor=fake_vision_extractor)

    assert len(calls) == 1
    assert calls[0].startswith(b"\x89PNG")
    assert pages[0].used_vision_fallback
    assert pages[0].text == "vision-transcribed text"
