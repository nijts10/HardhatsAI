"""Stage 4: chunking, per docs/INGESTION.md.

Rules, in priority order:
  1. Never split a table — it becomes one chunk (chunk_type 'table' or
     'spec_table'), serialised as markdown.
  2. Prepend heading_path to the content before embedding.
  3. Prose targets 500-800 tokens with ~15% overlap.
  4. Record page_start/page_end on every chunk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parsing import ParsedPage, Table

PROSE_MIN_TOKENS = 500
PROSE_MAX_TOKENS = 800
OVERLAP_RATIO = 0.15

# A line larger than this multiple of the page's median word size is treated
# as a heading candidate. Two tiers give a two-level heading_path, matching
# the "Technische gegevens > Brandwerendheid" example in docs/INGESTION.md.
HEADING_L1_RATIO = 1.4
HEADING_L2_RATIO = 1.15
HEADING_MAX_CHARS = 100

_SPEC_UNIT_PATTERN = re.compile(
    r"\b(mm|dB|kg|kg/m2|kg/m3|W/mK|m2K/W|W/m2K|°C|%|EI\d+|S\d+|kPa|EUR)\b",
    re.IGNORECASE,
)


@dataclass
class Chunk:
    chunk_index: int
    page_start: int
    page_end: int
    chunk_type: str
    heading_path: list[str]
    content: str
    token_count: int


@dataclass
class _Event:
    top: float
    kind: str  # 'heading_l1' | 'heading_l2' | 'text' | 'table'
    text: str = ""
    table: Table | None = None


try:
    import tiktoken
    _ENCODING = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - offline fallback
    _ENCODING = None


def count_tokens(text: str) -> int:
    if _ENCODING is not None:
        return len(_ENCODING.encode(text))
    return max(1, len(text.split()))


def _tail_by_tokens(text: str, n_tokens: int) -> str:
    if n_tokens <= 0:
        return ""
    if _ENCODING is not None:
        tokens = _ENCODING.encode(text)
        return _ENCODING.decode(tokens[-n_tokens:])
    words = text.split()
    return " ".join(words[-n_tokens:])


def _classify_table(rows: list[list[str | None]]) -> str:
    flat = " ".join(cell or "" for row in rows for cell in row)
    return "spec_table" if _SPEC_UNIT_PATTERN.search(flat) else "table"


def _table_to_markdown(rows: list[list[str | None]]) -> str:
    if not rows:
        return ""
    cleaned = [[(cell or "").strip().replace("|", "/") for cell in row] for row in rows]
    header, *body = cleaned
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in body:
        # Ragged tables happen; pad/truncate to the header width.
        row = (row + [""] * len(header))[: len(header)]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _page_events(page: ParsedPage) -> list[_Event]:
    events: list[_Event] = []
    for table in page.tables:
        events.append(_Event(top=table.top, kind="table", table=table))

    for line in page.lines:
        inside_table = any(t.top - 1 <= line.top <= t.bottom + 1 for t in page.tables)
        if inside_table:
            continue
        text = line.text.strip()
        if not text:
            continue
        is_heading_sized = page.median_font_size > 0 and len(text) <= HEADING_MAX_CHARS
        if is_heading_sized and line.avg_font_size >= page.median_font_size * HEADING_L1_RATIO:
            events.append(_Event(top=line.top, kind="heading_l1", text=text))
        elif is_heading_sized and line.avg_font_size >= page.median_font_size * HEADING_L2_RATIO:
            events.append(_Event(top=line.top, kind="heading_l2", text=text))
        else:
            events.append(_Event(top=line.top, kind="text", text=text))

    events.sort(key=lambda e: e.top)
    return events


def build_chunks(pages: list[ParsedPage]) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading_stack: list[str] = []

    buffer_text = ""
    buffer_page_start: int | None = None
    buffer_page_end: int | None = None

    def flush_prose(heading_path: list[str]) -> None:
        nonlocal buffer_text, buffer_page_start, buffer_page_end
        stripped = buffer_text.strip()
        if not stripped:
            buffer_text, buffer_page_start, buffer_page_end = "", None, None
            return
        content = _with_heading(heading_path, stripped)
        chunks.append(Chunk(
            chunk_index=len(chunks),
            page_start=buffer_page_start or buffer_page_end or 1,
            page_end=buffer_page_end or buffer_page_start or 1,
            chunk_type="prose",
            heading_path=list(heading_path),
            content=content,
            token_count=count_tokens(content),
        ))
        overlap_tokens = int(PROSE_MAX_TOKENS * OVERLAP_RATIO)
        carry = _tail_by_tokens(stripped, overlap_tokens)
        buffer_text = carry
        buffer_page_start = buffer_page_end
        buffer_page_end = buffer_page_start

    for page in pages:
        for event in _page_events(page):
            if event.kind == "heading_l1":
                flush_prose(heading_stack)
                heading_stack = [event.text]
            elif event.kind == "heading_l2":
                flush_prose(heading_stack)
                heading_stack = (heading_stack[:1] or []) + [event.text]
            elif event.kind == "table":
                flush_prose(heading_stack)
                rows = event.table.rows
                markdown = _table_to_markdown(rows)
                content = _with_heading(heading_stack, markdown)
                chunks.append(Chunk(
                    chunk_index=len(chunks),
                    page_start=page.page_number,
                    page_end=page.page_number,
                    chunk_type=_classify_table(rows),
                    heading_path=list(heading_stack),
                    content=content,
                    token_count=count_tokens(content),
                ))
            else:  # text
                if buffer_page_start is None:
                    buffer_page_start = page.page_number
                buffer_page_end = page.page_number
                buffer_text = (buffer_text + " " + event.text).strip()
                if count_tokens(buffer_text) >= PROSE_MAX_TOKENS:
                    flush_prose(heading_stack)

    flush_prose(heading_stack)
    return chunks


def _with_heading(heading_path: list[str], body: str) -> str:
    if not heading_path:
        return body
    return " > ".join(heading_path) + "\n\n" + body
