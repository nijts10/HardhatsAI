"""Thin data-access layer over the schema in supabase/migrations.

Every function takes an open psycopg connection/cursor rather than owning
one, so callers control transaction boundaries (worker.py commits once per
job, tests can roll back).
"""
from __future__ import annotations

import json
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


def connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, row_factory=dict_row, autocommit=False)


# ---------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------

def claim_job(conn: psycopg.Connection, worker: str, kinds: Optional[list[str]] = None) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from claim_job(%s, %s)", (worker, kinds))
        row = cur.fetchone()
    if row is None or row.get("id") is None:
        return None
    return row


def enqueue_job(
    conn: psycopg.Connection,
    kind: str,
    payload: dict,
    priority: int = 100,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (kind, payload, priority)
            values (%s, %s, %s)
            returning id
            """,
            (kind, Json(payload), priority),
        )
        return cur.fetchone()["id"]


def mark_job_succeeded(conn: psycopg.Connection, job_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("update jobs set status = 'succeeded' where id = %s", (job_id,))


def mark_job_failed(conn: psycopg.Connection, job_id: str, error: str, max_attempts: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update jobs
               set status = (case when attempts >= %s then 'dead' else 'pending' end)::job_status,
                   last_error = %s,
                   run_after = now() + (least(attempts, 5) * interval '30 seconds')
             where id = %s
            """,
            (max_attempts, error, job_id),
        )


# ---------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------

def get_document(conn: psycopg.Connection, document_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("select * from documents where id = %s", (document_id,))
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"document {document_id} not found")
    return row


def find_document_by_sha256(conn: psycopg.Connection, sha256: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from documents where sha256 = %s", (sha256,))
        return cur.fetchone()


def insert_document(
    conn: psycopg.Connection,
    *,
    supplier_company_id: Optional[str],
    uploaded_by: Optional[str],
    source: str,
    source_url: Optional[str],
    storage_bucket: str,
    storage_path: str,
    original_filename: Optional[str],
    mime_type: Optional[str],
    byte_size: int,
    sha256: str,
    kind: str,
    title: Optional[str] = None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into documents
              (supplier_company_id, uploaded_by, source, source_url, storage_bucket,
               storage_path, original_filename, mime_type, byte_size, sha256, kind, title,
               status)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued')
            returning id
            """,
            (
                supplier_company_id, uploaded_by, source, source_url, storage_bucket,
                storage_path, original_filename, mime_type, byte_size, sha256, kind, title,
            ),
        )
        return cur.fetchone()["id"]


def set_document_status(
    conn: psycopg.Connection,
    document_id: str,
    status: str,
    *,
    parse_error: Optional[str] = None,
    page_count: Optional[int] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update documents
               set status = %s,
                   parse_error = coalesce(%s, parse_error),
                   page_count = coalesce(%s, page_count),
                   parsed_at = case when %s in ('parsed') then now() else parsed_at end,
                   indexed_at = case when %s = 'indexed' then now() else indexed_at end
             where id = %s
            """,
            (status, parse_error, page_count, status, status, document_id),
        )


# ---------------------------------------------------------------------
# Pages & chunks
# ---------------------------------------------------------------------

def insert_page(conn: psycopg.Connection, document_id: str, page_number: int, text: str, layout: Optional[dict]) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into document_pages (document_id, page_number, text, layout)
            values (%s, %s, %s, %s)
            on conflict (document_id, page_number) do update set text = excluded.text, layout = excluded.layout
            returning id
            """,
            (document_id, page_number, text, Json(layout) if layout is not None else None),
        )
        return cur.fetchone()["id"]


def get_page_text(conn: psycopg.Connection, document_id: str, page_number: int) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            "select text from document_pages where document_id = %s and page_number = %s",
            (document_id, page_number),
        )
        row = cur.fetchone()
    return row["text"] if row else None


def insert_chunk(
    conn: psycopg.Connection,
    *,
    document_id: str,
    chunk_index: int,
    page_start: int,
    page_end: int,
    chunk_type: str,
    heading_path: list[str],
    content: str,
    token_count: int,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into document_chunks
              (document_id, chunk_index, page_start, page_end, chunk_type,
               heading_path, content, token_count)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (document_id, chunk_index) do update set
              page_start = excluded.page_start, page_end = excluded.page_end,
              chunk_type = excluded.chunk_type, heading_path = excluded.heading_path,
              content = excluded.content, token_count = excluded.token_count
            returning id
            """,
            (document_id, chunk_index, page_start, page_end, chunk_type,
             heading_path, content, token_count),
        )
        return cur.fetchone()["id"]


def get_chunks_missing_embeddings(conn: psycopg.Connection, document_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, content from document_chunks
             where document_id = %s and embedding is null
             order by chunk_index
            """,
            (document_id,),
        )
        return cur.fetchall()


def set_chunk_embedding(conn: psycopg.Connection, chunk_id: str, embedding: list[float]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update document_chunks set embedding = %s where id = %s",
            (_vector_literal(embedding), chunk_id),
        )


def get_all_chunks(conn: psycopg.Connection, document_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, chunk_index, page_start, page_end, heading_path, content
              from document_chunks
             where document_id = %s
             order by chunk_index
            """,
            (document_id,),
        )
        return cur.fetchall()


def _vector_literal(embedding: list[float]) -> str:
    # pgvector accepts the bracketed literal form over text protocol.
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


# ---------------------------------------------------------------------
# Spec vocabulary
# ---------------------------------------------------------------------

def get_spec_definitions(conn: psycopg.Connection, category_codes: Optional[list[str]] = None) -> list[dict]:
    with conn.cursor() as cur:
        if category_codes:
            cur.execute(
                """
                select * from spec_definitions
                 where category_codes = '{}' or category_codes && %s
                 order by key
                """,
                (category_codes,),
            )
        else:
            cur.execute("select * from spec_definitions order by key")
        return cur.fetchall()


def get_category_by_code(conn: psycopg.Connection, code: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from product_categories where code = %s", (code,))
        return cur.fetchone()


# ---------------------------------------------------------------------
# Extraction runs
# ---------------------------------------------------------------------

def start_extraction_run(
    conn: psycopg.Connection,
    *,
    document_id: str,
    parser_version: str,
    prompt_version: str,
    model: str,
    embedding_model: Optional[str] = None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into extraction_runs (document_id, parser_version, prompt_version, model, embedding_model)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (document_id, parser_version, prompt_version, model, embedding_model),
        )
        return cur.fetchone()["id"]


def finish_extraction_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    stats: dict,
    cost_usd: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update extraction_runs
               set status = %s, stats = %s, cost_usd = %s, error = %s, finished_at = now()
             where id = %s
            """,
            (status, Json(stats), cost_usd, error, run_id),
        )


# ---------------------------------------------------------------------
# Products & specs
# ---------------------------------------------------------------------

def upsert_product(
    conn: psycopg.Connection,
    *,
    supplier_company_id: str,
    category_id: Optional[str],
    canonical_name: str,
    slug: str,
    manufacturer_ref: Optional[str],
    description: Optional[str],
    primary_document_id: Optional[str],
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id from products
             where supplier_company_id = %s and manufacturer_ref = %s
            """,
            (supplier_company_id, manufacturer_ref),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """
                update products
                   set canonical_name = %s, category_id = %s, description = %s,
                       primary_document_id = coalesce(primary_document_id, %s)
                 where id = %s
                """,
                (canonical_name, category_id, description, primary_document_id, existing["id"]),
            )
            return existing["id"]

        cur.execute(
            """
            insert into products
              (supplier_company_id, category_id, canonical_name, slug,
               manufacturer_ref, description, primary_document_id, status)
            values (%s, %s, %s, %s, %s, %s, %s, 'draft')
            returning id
            """,
            (supplier_company_id, category_id, canonical_name, slug,
             manufacturer_ref, description, primary_document_id),
        )
        return cur.fetchone()["id"]


def link_product_document(conn: psycopg.Connection, product_id: str, document_id: str, relation: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into product_documents (product_id, document_id, relation)
            values (%s, %s, %s)
            on conflict (product_id, document_id) do nothing
            """,
            (product_id, document_id, relation),
        )


def get_current_spec(conn: psycopg.Connection, product_id: str, spec_definition_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select * from product_specs
             where product_id = %s and spec_definition_id = %s and is_current
            """,
            (product_id, spec_definition_id),
        )
        return cur.fetchone()


def insert_spec(conn: psycopg.Connection, spec: dict, *, is_current: bool = True) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into product_specs
              (product_id, spec_definition_id, value_numeric, value_numeric_max,
               value_text, value_bool, value_enum, unit, presence, confidence,
               document_id, chunk_id, page_number, source_snippet, source_bbox,
               extraction_run_id, is_current)
            values (%(product_id)s, %(spec_definition_id)s, %(value_numeric)s, %(value_numeric_max)s,
                    %(value_text)s, %(value_bool)s, %(value_enum)s, %(unit)s, %(presence)s, %(confidence)s,
                    %(document_id)s, %(chunk_id)s, %(page_number)s, %(source_snippet)s, %(source_bbox)s,
                    %(extraction_run_id)s, %(is_current)s)
            returning id
            """,
            {**spec, "source_bbox": Json(spec.get("source_bbox")) if spec.get("source_bbox") else None,
             "is_current": is_current},
        )
        return cur.fetchone()["id"]


def set_spec_current(conn: psycopg.Connection, spec_id: str, is_current: bool) -> None:
    with conn.cursor() as cur:
        cur.execute("update product_specs set is_current = %s where id = %s", (is_current, spec_id))


def supersede_spec(conn: psycopg.Connection, old_spec_id: str, new_spec_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("select supersede_spec(%s, %s)", (old_spec_id, new_spec_id))


def bump_product_version(conn: psycopg.Connection, product_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("update products set version = version + 1 where id = %s", (product_id,))


def recompute_data_completeness(conn: psycopg.Connection, supplier_company_id: str) -> None:
    """Share of category-relevant filterable specs that are 'stated', across
    all of a supplier's products. Recomputed after every persisted document."""
    with conn.cursor() as cur:
        cur.execute(
            """
            with relevant as (
              select p.id as product_id, sd.id as spec_definition_id
                from products p
                join spec_definitions sd
                  on sd.is_filterable
                 and (sd.category_codes = '{}' or exists (
                       select 1 from product_categories pc
                        where pc.id = p.category_id and pc.code = any(sd.category_codes)
                     ))
               where p.supplier_company_id = %s
            ),
            stated as (
              select r.product_id, r.spec_definition_id
                from relevant r
                join product_specs ps
                  on ps.product_id = r.product_id
                 and ps.spec_definition_id = r.spec_definition_id
                 and ps.is_current
                 and ps.presence in ('stated', 'derived', 'manual')
            )
            select
              coalesce(
                (select count(*) from stated)::numeric
                / nullif((select count(*) from relevant), 0),
                0
              ) as completeness
            """,
            (supplier_company_id,),
        )
        completeness = cur.fetchone()["completeness"]
        cur.execute(
            "update supplier_companies set data_completeness = %s where id = %s",
            (completeness, supplier_company_id),
        )
