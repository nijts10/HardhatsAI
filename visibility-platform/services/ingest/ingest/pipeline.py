"""Synchronous pipeline: one document version, start to finish.

No job queue (see db.py's module docstring for why) — cli.py calls this
directly after a successful upload, and it runs parse -> chunk -> embed ->
extract -> persist in one go.
"""
from __future__ import annotations

from anthropic import Anthropic

from . import db, storage
from .chunking import build_chunks
from .config import Config
from .embedding import Embedder
from .extraction import PARSER_VERSION, PROMPT_VERSION, extract_claims, spec_attributes_for_category
from .parsing import claude_vision_extractor, parse_pdf
from .persistence import persist_extracted_products, persist_unmapped_findings
from .webpage import parse_webpage


def run(conn, config: Config, *, document_version_id: str) -> dict:
    version = db.get_document_version(conn, document_version_id)
    document = db.get_document(conn, version["document_id"])
    if document["product_id"] is None:
        raise ValueError(
            "document has no product_id set — Phase 2 extraction needs to know "
            "which brand's product family this document describes"
        )

    with conn.cursor() as cur:
        cur.execute(
            "select b.id as brand_id, b.name as brand_name, p.category_id "
            "from products p join brands b on b.id = p.brand_id where p.id = %s",
            (document["product_id"],),
        )
        product_row = cur.fetchone()

    client = storage.make_client(config.supabase_url, config.supabase_service_role_key)
    pdf_bytes = storage.download(client, version["storage_path"])

    anthropic_client = Anthropic(api_key=config.anthropic_api_key)
    vision_extractor = claude_vision_extractor(anthropic_client, config.anthropic_model)
    pages = parse_pdf(pdf_bytes, vision_extractor=vision_extractor)

    return _chunk_embed_extract_persist(
        conn, config, document_version_id=document_version_id, brand_id=product_row["brand_id"],
        brand_name=product_row["brand_name"], category_id=product_row["category_id"],
        pages=pages, anthropic_client=anthropic_client, scope_by_category=True,
    )


def run_webpage(conn, config: Config, *, document_version_id: str, brand_id: str, brand_name: str) -> dict:
    """Same pipeline as run(), for a page of the brand's own live website
    instead of an uploaded PDF -- see webpage.py's module docstring for
    why this is genuinely the same standard, not a second weaker one.

    Two real differences from run(): (1) the source bytes are HTML fetched
    earlier and archived to Storage (by the ingest-website CLI command),
    parsed into the same ParsedPage shape via webpage.parse_webpage()
    instead of parse_pdf(); (2) no category scoping -- a crawled page's
    category isn't known ahead of time the way an uploaded datasheet's
    product is, so the full spec_attributes vocabulary is always asked
    about rather than narrowed to one product's category tree. Category_id
    is therefore not required here; a newly-created product from a
    webpage claim gets category_id=None (matches the existing "not yet
    known" convention -- see spec_attributes_for_category's own docstring)
    unless it already exists under a category from prior PDF ingestion, in
    which case persistence.py's exact-name-slug match reuses that
    existing row (and its category) automatically."""
    version = db.get_document_version(conn, document_version_id)
    client = storage.make_client(config.supabase_url, config.supabase_service_role_key)
    html_bytes = storage.download(client, version["storage_path"])

    anthropic_client = Anthropic(api_key=config.anthropic_api_key)
    page = parse_webpage(html_bytes.decode("utf-8", errors="replace"), version["source_url"])

    return _chunk_embed_extract_persist(
        conn, config, document_version_id=document_version_id, brand_id=brand_id,
        brand_name=brand_name, category_id=None, pages=[page],
        anthropic_client=anthropic_client, scope_by_category=False,
    )


def _chunk_embed_extract_persist(
    conn, config: Config, *, document_version_id: str, brand_id: str, brand_name: str,
    category_id: str | None, pages: list, anthropic_client: Anthropic, scope_by_category: bool,
) -> dict:
    for page in pages:
        db.insert_page(conn, document_version_id, page.page_number, page.text, page.layout)

    chunks = build_chunks(pages)
    for chunk in chunks:
        db.insert_chunk(
            conn, document_version_id=document_version_id, chunk_index=chunk.chunk_index,
            page_start=chunk.page_start, page_end=chunk.page_end, chunk_type=chunk.chunk_type,
            heading_path=chunk.heading_path, content=chunk.content, token_count=chunk.token_count,
        )

    pending = db.get_chunks_missing_embeddings(conn, document_version_id)
    if pending:
        embedder = Embedder(config.openai_api_key, config.openai_embedding_model, config.embedding_dimensions)
        vectors = embedder.embed_batch([c["content"] for c in pending])
        for chunk, vector in zip(pending, vectors):
            db.set_chunk_embedding(conn, chunk["id"], vector)

    spec_attributes = db.get_spec_attributes(conn)
    # spec_attrs_by_key stays the FULL vocabulary, so persistence still accepts
    # any approved key the model states even if it fell outside this product's
    # category scope below -- the scoping only narrows what we ASK about.
    spec_attrs_by_key = {d["key"]: d for d in spec_attributes}
    if scope_by_category:
        category_codes = db.get_category_ancestor_codes(conn, category_id)
        prompt_spec_attributes = spec_attributes_for_category(spec_attributes, category_codes)
    else:
        prompt_spec_attributes = spec_attributes
    all_chunks = db.get_all_chunks(conn, document_version_id)

    run_id = db.start_extraction_run(
        conn, document_version_id=document_version_id, parser_version=PARSER_VERSION,
        prompt_version=PROMPT_VERSION, model=config.anthropic_model,
        embedding_model=config.openai_embedding_model,
    )

    try:
        extracted = extract_claims(
            anthropic_client, model=config.anthropic_model, brand_name=brand_name,
            spec_attributes=prompt_spec_attributes, chunks=all_chunks,
        )
    except Exception as exc:
        db.finish_extraction_run(conn, run_id, status="failed", stats={}, error=str(exc))
        raise

    def get_page_text(page_number):
        return db.get_page_text(conn, document_version_id, page_number)

    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=category_id,
        document_version_id=document_version_id, extraction_run_id=run_id,
        extracted_products=extracted.products, spec_attrs_by_key=spec_attrs_by_key,
        document_chunks=all_chunks, get_page_text=get_page_text,
    )
    persist_unmapped_findings(
        conn, document_version_id=document_version_id, findings=extracted.unmapped_findings,
        get_page_text=get_page_text, stats=stats,
    )

    db.finish_extraction_run(
        conn, run_id, status="succeeded",
        stats={
            "products": stats.products_created_or_updated,
            "claims_inserted": stats.claims_inserted,
            "claims_superseded": stats.claims_superseded,
            "claims_rejected": stats.claims_rejected,
            "certifications_inserted": stats.certifications_inserted,
            "unmapped_findings_queued": stats.unmapped_findings_queued,
            "unmapped_findings_rejected": stats.unmapped_findings_rejected,
            "rejections": stats.rejections,
            "possible_duplicate_products": stats.possible_duplicate_products,
        },
    )

    return {
        "pages": len(pages),
        "chunks": len(chunks),
        "products": stats.products_created_or_updated,
        "claims_inserted": stats.claims_inserted,
        "claims_rejected": stats.claims_rejected,
        "unmapped_findings_queued": stats.unmapped_findings_queued,
        "possible_duplicate_products": stats.possible_duplicate_products,
    }
