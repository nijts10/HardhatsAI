from ingest.chunking import build_chunks, count_tokens
from ingest.parsing import Line, ParsedPage, Table


def make_page(page_number, lines, tables=None, median_font_size=10.0):
    return ParsedPage(
        page_number=page_number,
        text="",
        layout=None,
        used_vision_fallback=False,
        tables=tables or [],
        lines=lines,
        median_font_size=median_font_size,
    )


def test_heading_detected_by_font_size_and_prepended_to_content():
    page = make_page(1, lines=[
        Line(text="Technische gegevens", top=10, avg_font_size=16.0),  # 1.6x median -> L1
        Line(text="Brandwerendheid", top=30, avg_font_size=13.0),      # 1.3x median -> L2
        Line(text="Dit product heeft EI 60 volgens NEN-EN 13501-2.", top=50, avg_font_size=10.0),
    ])
    chunks = build_chunks([page])
    assert len(chunks) == 1
    assert chunks[0].heading_path == ["Technische gegevens", "Brandwerendheid"]
    assert chunks[0].content.startswith("Technische gegevens > Brandwerendheid\n\n")
    assert "EI 60" in chunks[0].content
    assert chunks[0].chunk_type == "prose"


def test_table_becomes_a_single_chunk_and_is_excluded_from_prose():
    table = Table(rows=[["Type", "EI"], ["A", "60"], ["B", "90"]], top=40, bottom=80)
    page = make_page(1, lines=[
        Line(text="Intro text before the table.", top=10, avg_font_size=10.0),
        # These lines sit inside the table's bbox and must not leak into prose.
        Line(text="Type EI", top=45, avg_font_size=10.0),
        Line(text="A 60", top=55, avg_font_size=10.0),
        Line(text="Outro text after the table.", top=100, avg_font_size=10.0),
    ], tables=[table])

    chunks = build_chunks([page])
    types = [c.chunk_type for c in chunks]
    assert "spec_table" in types or "table" in types

    table_chunks = [c for c in chunks if c.chunk_type in ("table", "spec_table")]
    assert len(table_chunks) == 1
    assert "| Type | EI |" in table_chunks[0].content
    assert "| A | 60 |" in table_chunks[0].content

    prose_content = " ".join(c.content for c in chunks if c.chunk_type == "prose")
    assert "Type EI" not in prose_content
    assert "Intro text" in prose_content
    assert "Outro text" in prose_content


def test_spec_table_classified_when_units_present():
    table = Table(rows=[["Dikte", "Lambda"], ["100 mm", "0.035 W/mK"]], top=0, bottom=20)
    page = make_page(1, lines=[], tables=[table])
    chunks = build_chunks([page])
    assert chunks[0].chunk_type == "spec_table"


def test_prose_flushes_and_carries_overlap_across_the_token_window():
    # Force many flushes by feeding well over PROSE_MAX_TOKENS of unique words.
    words = [f"word{i}" for i in range(2000)]
    page = make_page(1, lines=[Line(text=w, top=float(i), avg_font_size=10.0) for i, w in enumerate(words)])
    chunks = build_chunks([page])
    assert len(chunks) > 1
    # Overlap: the tail of one chunk should reappear at the head of the next
    # (the overlap window is ~15% of PROSE_MAX_TOKENS, i.e. ~120 words here).
    first_tail_words = chunks[0].content.split()[-5:]
    second_head_words = chunks[1].content.split()[:120]
    assert any(w in second_head_words for w in first_tail_words)


def test_page_span_recorded_across_multi_page_prose():
    page1 = make_page(1, lines=[Line(text="short line one", top=10, avg_font_size=10.0)])
    page2 = make_page(2, lines=[Line(text="short line two", top=10, avg_font_size=10.0)])
    chunks = build_chunks([page1, page2])
    assert len(chunks) == 1
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 2


def test_count_tokens_never_zero_for_nonempty_text():
    assert count_tokens("hello world") > 0
