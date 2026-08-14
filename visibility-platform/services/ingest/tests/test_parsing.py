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


def test_parse_pdf_appends_table_text_in_reading_order_for_verbatim_matching():
    # Found for real auditing a dense Hunter Douglas comparison table
    # (2026-08-14): a cell whose content wraps to more than one line lands
    # on a different y-coordinate than its row neighbours, so pdfplumber's
    # spatial extract_text() splits it across lines and interleaves it with
    # the NEXT column's value instead of keeping it with its own row --
    # "Unperforated linear grill ceiling panel" (one cell) came out as
    # "...ceiling 0.90\npanel..." (0.90 is the cell to its right). Every
    # claim quoting that cell verbatim then failed verify_claim's citation
    # check even though the model read the table correctly. page.text must
    # still contain the phrase as one contiguous (whitespace-normalised)
    # run, from the table's own row-major order, alongside whatever
    # extract_text() produced.
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    cell_style = ParagraphStyle("cell", fontSize=8, leading=10)
    rows = [
        [Paragraph("Material", cell_style), Paragraph("Description", cell_style),
         Paragraph("Absorption alpha w", cell_style)],
        [Paragraph("Aluminum", cell_style), Paragraph("Unperforated linear grill ceiling panel", cell_style),
         Paragraph("0.90", cell_style)],
    ]
    table = Table(rows, colWidths=[60, 120, 60])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    doc.build([table])
    pdf_bytes = buf.getvalue()

    pages = parse_pdf(pdf_bytes)

    from ingest.verification import snippet_in_page
    assert snippet_in_page("Unperforated linear grill ceiling panel", pages[0].text)


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
