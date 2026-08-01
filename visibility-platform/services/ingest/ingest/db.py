"""Thin data-access layer over the visibility-platform schema.

Note on scale: unlike the HardhatsAI ingest worker, there is no jobs/queue
table here. Phase 2 volume is analyst-driven (one client's documents at a
time, uploaded manually), not a bulk crawl — a synchronous pipeline
(cli.py calls pipeline.py directly) is simpler and matches actual scale.
Add a queue back if/when upload volume actually justifies one.
"""
from __future__ import annotations

from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


def connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, row_factory=dict_row, autocommit=False)


# ---------------------------------------------------------------------
# Tenancy lookups (orgs/brands/products are created by analysts, not
# auto-created by the worker — onboarding is sales-driven, not ingest-first)
# ---------------------------------------------------------------------

def get_organization_by_slug(conn: psycopg.Connection, slug: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from organizations where slug = %s", (slug,))
        return cur.fetchone()


def get_brand_by_slug(conn: psycopg.Connection, slug: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from brands where slug = %s", (slug,))
        return cur.fetchone()


def get_product_by_slug(conn: psycopg.Connection, slug: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from products where slug = %s", (slug,))
        return cur.fetchone()


def get_category_by_code(conn: psycopg.Connection, code: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from product_categories where code = %s", (code,))
        return cur.fetchone()


def create_product(
    conn: psycopg.Connection, *, brand_id: str, category_id: Optional[str],
    name: str, slug: str, manufacturer_ref: Optional[str] = None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into products (brand_id, category_id, name, slug, manufacturer_ref)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (brand_id, category_id, name, slug, manufacturer_ref),
        )
        return cur.fetchone()["id"]


# ---------------------------------------------------------------------
# Documents & versions
# ---------------------------------------------------------------------

def get_document(conn: psycopg.Connection, document_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("select * from documents where id = %s", (document_id,))
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"document {document_id} not found")
    return row


def find_version_by_sha256(conn: psycopg.Connection, sha256: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from document_versions where sha256 = %s", (sha256,))
        return cur.fetchone()


def create_document(
    conn: psycopg.Connection, *, organization_id: str, product_id: Optional[str],
    title: Optional[str], kind: str,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into documents (organization_id, product_id, title, kind)
            values (%s, %s, %s, %s)
            returning id
            """,
            (organization_id, product_id, title, kind),
        )
        return cur.fetchone()["id"]


def create_document_version(
    conn: psycopg.Connection, *, document_id: str, storage_path: str,
    original_filename: Optional[str], mime_type: Optional[str], byte_size: int,
    sha256: str, uploaded_by: Optional[str],
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into document_versions
              (document_id, storage_path, original_filename, mime_type, byte_size, sha256, uploaded_by)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (document_id, storage_path, original_filename, mime_type, byte_size, sha256, uploaded_by),
        )
        return cur.fetchone()["id"]


def get_document_version(conn: psycopg.Connection, document_version_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            select dv.*, d.organization_id, d.product_id
              from document_versions dv
              join documents d on d.id = dv.document_id
             where dv.id = %s
            """,
            (document_version_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"document_version {document_version_id} not found")
    return row


# ---------------------------------------------------------------------
# Pages & chunks (scoped to a document VERSION, not the logical document —
# re-parsed content genuinely differs between versions)
# ---------------------------------------------------------------------

def insert_page(conn: psycopg.Connection, document_version_id: str, page_number: int, text: str, layout: Optional[dict]) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into document_pages (document_version_id, page_number, text, layout)
            values (%s, %s, %s, %s)
            on conflict (document_version_id, page_number) do update set text = excluded.text, layout = excluded.layout
            returning id
            """,
            (document_version_id, page_number, text, Json(layout) if layout is not None else None),
        )
        return cur.fetchone()["id"]


def get_page_text(conn: psycopg.Connection, document_version_id: str, page_number: int) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            "select text from document_pages where document_version_id = %s and page_number = %s",
            (document_version_id, page_number),
        )
        row = cur.fetchone()
    return row["text"] if row else None


def insert_chunk(
    conn: psycopg.Connection, *, document_version_id: str, chunk_index: int,
    page_start: int, page_end: int, chunk_type: str, heading_path: list[str],
    content: str, token_count: int,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into document_chunks
              (document_version_id, chunk_index, page_start, page_end, chunk_type,
               heading_path, content, token_count)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (document_version_id, chunk_index) do update set
              page_start = excluded.page_start, page_end = excluded.page_end,
              chunk_type = excluded.chunk_type, heading_path = excluded.heading_path,
              content = excluded.content, token_count = excluded.token_count
            returning id
            """,
            (document_version_id, chunk_index, page_start, page_end, chunk_type,
             heading_path, content, token_count),
        )
        return cur.fetchone()["id"]


def get_chunks_missing_embeddings(conn: psycopg.Connection, document_version_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, content from document_chunks
             where document_version_id = %s and embedding is null
             order by chunk_index
            """,
            (document_version_id,),
        )
        return cur.fetchall()


def set_chunk_embedding(conn: psycopg.Connection, chunk_id: str, embedding: list[float]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update document_chunks set embedding = %s where id = %s",
            (_vector_literal(embedding), chunk_id),
        )


def get_all_chunks(conn: psycopg.Connection, document_version_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, chunk_index, page_start, page_end, heading_path, content
              from document_chunks
             where document_version_id = %s
             order by chunk_index
            """,
            (document_version_id,),
        )
        return cur.fetchall()


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


# ---------------------------------------------------------------------
# Spec vocabulary
# ---------------------------------------------------------------------

def get_spec_attributes(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from spec_attributes order by key")
        return cur.fetchall()


# ---------------------------------------------------------------------
# Extraction runs
# ---------------------------------------------------------------------

def start_extraction_run(
    conn: psycopg.Connection, *, document_version_id: str, parser_version: str,
    prompt_version: str, model: str, embedding_model: Optional[str] = None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into extraction_runs (document_version_id, parser_version, prompt_version, model, embedding_model)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (document_version_id, parser_version, prompt_version, model, embedding_model),
        )
        return cur.fetchone()["id"]


def finish_extraction_run(
    conn: psycopg.Connection, run_id: str, *, status: str, stats: dict,
    cost_usd: Optional[float] = None, error: Optional[str] = None,
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
# Claims & certifications
# ---------------------------------------------------------------------

def get_current_claim(conn: psycopg.Connection, product_id: str, spec_attribute_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select * from claims
             where product_id = %s and spec_attribute_id = %s and is_current
            """,
            (product_id, spec_attribute_id),
        )
        return cur.fetchone()


def insert_claim(conn: psycopg.Connection, claim: dict, *, is_current: bool = True) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into claims
              (product_id, spec_attribute_id, value_numeric, value_numeric_max,
               value_text, value_bool, value_enum, unit, presence, confidence,
               document_version_id, chunk_id, page_number, source_snippet,
               extraction_run_id, is_current)
            values (%(product_id)s, %(spec_attribute_id)s, %(value_numeric)s, %(value_numeric_max)s,
                    %(value_text)s, %(value_bool)s, %(value_enum)s, %(unit)s, %(presence)s, %(confidence)s,
                    %(document_version_id)s, %(chunk_id)s, %(page_number)s, %(source_snippet)s,
                    %(extraction_run_id)s, %(is_current)s)
            returning id
            """,
            {**claim, "is_current": is_current},
        )
        return cur.fetchone()["id"]


def set_claim_current(conn: psycopg.Connection, claim_id: str, is_current: bool) -> None:
    with conn.cursor() as cur:
        cur.execute("update claims set is_current = %s where id = %s", (is_current, claim_id))


def supersede_claim(conn: psycopg.Connection, old_claim_id: str, new_claim_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("select supersede_claim(%s, %s)", (old_claim_id, new_claim_id))


def insert_certification(conn: psycopg.Connection, cert: dict) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into certifications
              (product_id, document_version_id, scheme, number, issuer,
               issued_on, valid_until, scope_text, page_number, source_snippet)
            values (%(product_id)s, %(document_version_id)s, %(scheme)s, %(number)s, %(issuer)s,
                    %(issued_on)s, %(valid_until)s, %(scope_text)s, %(page_number)s, %(source_snippet)s)
            returning id
            """,
            cert,
        )
        return cur.fetchone()["id"]
