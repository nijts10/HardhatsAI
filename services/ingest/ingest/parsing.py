"""Stage 3: PDF -> per-page text + layout.

Datasheets are table-dominant (see docs/INGESTION.md). pdfplumber gives us a
text layer plus word bounding boxes for later highlighting; pages with too
little extractable text (scans, flattened graphics) fall back to Claude
vision on a rendered image of the page.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import pdfplumber

# Below this character count we don't trust the text layer to represent the
# page and switch to vision extraction instead.
MIN_TEXT_LAYER_CHARS = 40


@dataclass
class Line:
    text: str
    top: float
    avg_font_size: float


@dataclass
class Table:
    rows: list[list[str | None]]
    top: float
    bottom: float


@dataclass
class ParsedPage:
    page_number: int  # 1-indexed
    text: str
    layout: dict | None
    used_vision_fallback: bool
    tables: list[Table]
    lines: list[Line]
    median_font_size: float


def parse_pdf(pdf_bytes: bytes, vision_extractor=None) -> list[ParsedPage]:
    """vision_extractor: callable(png_bytes: bytes) -> str, injected so callers
    can supply the Claude vision call (or a fake, in tests)."""
    pages: list[ParsedPage] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            words = page.extract_words(extra_attrs=["size"]) or []
            layout = {
                "width": page.width,
                "height": page.height,
                "words": [
                    {
                        "text": w["text"],
                        "x0": w["x0"], "x1": w["x1"],
                        "top": w["top"], "bottom": w["bottom"],
                    }
                    for w in words
                ],
            }
            lines, median_size = _group_lines(words)
            tables = [
                Table(rows=t.extract(), top=t.bbox[1], bottom=t.bbox[3])
                for t in page.find_tables()
                if t.extract()
            ]

            used_vision = False
            if len(text.strip()) < MIN_TEXT_LAYER_CHARS and vision_extractor is not None:
                png_bytes = _render_page_png(page)
                text = vision_extractor(png_bytes)
                used_vision = True

            pages.append(ParsedPage(
                page_number=i, text=text, layout=layout, used_vision_fallback=used_vision,
                tables=tables, lines=lines, median_font_size=median_size,
            ))
    return pages


def _group_lines(words: list[dict], y_tolerance: float = 2.0) -> tuple[list[Line], float]:
    """Group words into text lines using vertical position, since pdfplumber
    gives word-level boxes, not lines. Also returns the page's median word
    font size, used by chunking to detect heading-sized text."""
    if not words:
        return [], 0.0

    sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    current: list[dict] = [sorted_words[0]]
    for w in sorted_words[1:]:
        if abs(w["top"] - current[-1]["top"]) <= y_tolerance:
            current.append(w)
        else:
            lines.append(current)
            current = [w]
    lines.append(current)

    result = [
        Line(
            text=" ".join(w["text"] for w in sorted(line, key=lambda w: w["x0"])),
            top=min(w["top"] for w in line),
            avg_font_size=sum(w.get("size", 0) for w in line) / len(line),
        )
        for line in lines
    ]
    sizes = sorted(w.get("size", 0) for w in words)
    median_size = sizes[len(sizes) // 2] if sizes else 0.0
    return result, median_size


def _render_page_png(page) -> bytes:
    image = page.to_image(resolution=150)
    buf = io.BytesIO()
    image.original.save(buf, format="PNG")
    return buf.getvalue()


def claude_vision_extractor(client, model: str):
    """Returns a vision_extractor callable bound to a live Anthropic client."""

    def _extract(png_bytes: bytes) -> str:
        b64 = base64.standard_b64encode(png_bytes).decode("ascii")
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Transcribe all text on this page verbatim, including table "
                            "contents in a readable row/column order. Output only the "
                            "transcription, no commentary."
                        ),
                    },
                ],
            }],
        )
        return "".join(block.text for block in message.content if block.type == "text")

    return _extract
