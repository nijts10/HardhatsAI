"""Main loop: claim_job() -> dispatch by kind -> commit or fail.

Job kinds chain into each other so that queuing a single parse_document job
for a freshly uploaded document runs the whole pipeline to 'indexed' without
further intervention (see the stage table in docs/INGESTION.md).
"""
from __future__ import annotations

import logging
import time

from anthropic import Anthropic

from . import db, storage
from .chunking import build_chunks
from .config import Config, load_config
from .embedding import Embedder
from .extraction import PARSER_VERSION, PROMPT_VERSION, extract_products
from .parsing import claude_vision_extractor, parse_pdf
from .persistence import persist_extracted_products

logger = logging.getLogger("ingest.worker")

JOB_KINDS = ["parse_document", "embed_chunks", "extract_specs"]
POLL_INTERVAL_SECONDS = 5


def handle_parse_document(conn, config: Config, payload: dict) -> None:
    document_id = payload["document_id"]
    document = db.get_document(conn, document_id)

    client = storage.make_client(config.supabase_url, config.supabase_service_role_key)
    pdf_bytes = storage.download(client, document["storage_path"])

    anthropic_client = Anthropic(api_key=config.anthropic_api_key)
    vision_extractor = claude_vision_extractor(anthropic_client, config.anthropic_model)

    pages = parse_pdf(pdf_bytes, vision_extractor=vision_extractor)
    for page in pages:
        db.insert_page(conn, document_id, page.page_number, page.text, page.layout)

    chunks = build_chunks(pages)
    for chunk in chunks:
        db.insert_chunk(
            conn,
            document_id=document_id,
            chunk_index=chunk.chunk_index,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            chunk_type=chunk.chunk_type,
            heading_path=chunk.heading_path,
            content=chunk.content,
            token_count=chunk.token_count,
        )

    db.set_document_status(conn, document_id, "parsed", page_count=len(pages))
    db.enqueue_job(conn, "embed_chunks", {"document_id": document_id})


def handle_embed_chunks(conn, config: Config, payload: dict) -> None:
    document_id = payload["document_id"]
    pending = db.get_chunks_missing_embeddings(conn, document_id)
    if pending:
        embedder = Embedder(config.openai_api_key, config.openai_embedding_model, config.embedding_dimensions)
        vectors = embedder.embed_batch([c["content"] for c in pending])
        for chunk, vector in zip(pending, vectors):
            db.set_chunk_embedding(conn, chunk["id"], vector)

    db.enqueue_job(conn, "extract_specs", {"document_id": document_id})


def handle_extract_specs(conn, config: Config, payload: dict) -> None:
    document_id = payload["document_id"]
    document = db.get_document(conn, document_id)
    db.set_document_status(conn, document_id, "extracting")

    spec_defs = db.get_spec_definitions(conn)
    spec_defs_by_key = {d["key"]: d for d in spec_defs}
    with conn.cursor() as cur:
        cur.execute("select code, id from product_categories")
        categories = cur.fetchall()
    category_by_code = {c["code"]: c for c in categories}
    category_codes = list(category_by_code.keys())

    chunks = db.get_all_chunks(conn, document_id)

    run_id = db.start_extraction_run(
        conn,
        document_id=document_id,
        parser_version=PARSER_VERSION,
        prompt_version=PROMPT_VERSION,
        model=config.anthropic_model,
        embedding_model=config.openai_embedding_model,
    )

    anthropic_client = Anthropic(api_key=config.anthropic_api_key)
    try:
        extracted = extract_products(
            anthropic_client,
            model=config.anthropic_model,
            spec_definitions=spec_defs,
            category_codes=category_codes,
            chunks=chunks,
        )
    except Exception as exc:
        db.finish_extraction_run(conn, run_id, status="failed", stats={}, error=str(exc))
        raise

    def get_page_text(page_number):
        return db.get_page_text(conn, document_id, page_number)

    supplier_company_id = document["supplier_company_id"]
    stats = persist_extracted_products(
        conn,
        document_id=document_id,
        supplier_company_id=supplier_company_id,
        extraction_run_id=run_id,
        extracted_products=extracted,
        spec_defs_by_key=spec_defs_by_key,
        category_by_code=category_by_code,
        document_chunks=chunks,
        get_page_text=get_page_text,
    )

    db.finish_extraction_run(
        conn, run_id, status="succeeded",
        stats={
            "products": stats.products_created_or_updated,
            "specs_inserted": stats.specs_inserted,
            "specs_superseded": stats.specs_superseded,
            "specs_rejected": stats.specs_rejected,
            "rejections": stats.rejections,
        },
    )
    db.set_document_status(conn, document_id, "indexed")


HANDLERS = {
    "parse_document": handle_parse_document,
    "embed_chunks": handle_embed_chunks,
    "extract_specs": handle_extract_specs,
}


def run_one(conn, config: Config) -> bool:
    """Claims and runs a single job. Returns False if no job was available."""
    job = db.claim_job(conn, config.worker_name, JOB_KINDS)
    conn.commit()  # release the claim_job row lock/status update independently of the work
    if job is None:
        return False

    handler = HANDLERS[job["kind"]]
    try:
        handler(conn, config, job["payload"])
        db.mark_job_succeeded(conn, job["id"])
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.exception("job %s (%s) failed", job["id"], job["kind"])
        db.mark_job_failed(conn, job["id"], str(exc), job["max_attempts"])
        conn.commit()

        document_id = job["payload"].get("document_id")
        if document_id:
            try:
                db.set_document_status(conn, document_id, "failed", parse_error=str(exc))
                conn.commit()
            except Exception:
                conn.rollback()
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    conn = db.connect(config.database_url)
    logger.info("ingest worker %s started, polling for %s", config.worker_name, JOB_KINDS)
    try:
        while True:
            if not run_one(conn, config):
                time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
