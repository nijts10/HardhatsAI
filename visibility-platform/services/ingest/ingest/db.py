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


def get_category_ancestor_codes(conn: psycopg.Connection, category_id: Optional[str]) -> list[str]:
    """category_id's own code plus every ancestor's, closest first. Empty
    list for a null category_id -- see category_and_ancestor_codes() in
    0006_category_hierarchy.sql for why that's correct rather than an error."""
    with conn.cursor() as cur:
        cur.execute("select category_and_ancestor_codes(%s) as codes", (category_id,))
        row = cur.fetchone()
    return row["codes"] if row else []


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


def get_variant_by_slug(conn: psycopg.Connection, product_id: str, slug: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "select * from product_variants where product_id = %s and slug = %s",
            (product_id, slug),
        )
        return cur.fetchone()


def create_variant(
    conn: psycopg.Connection, *, product_id: str, label: str, slug: str,
    manufacturer_ref: Optional[str] = None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into product_variants (product_id, label, slug, manufacturer_ref)
            values (%s, %s, %s, %s)
            returning id
            """,
            (product_id, label, slug, manufacturer_ref),
        )
        return cur.fetchone()["id"]


def update_variant_label(conn: psycopg.Connection, variant_id: str, label: str) -> None:
    with conn.cursor() as cur:
        cur.execute("update product_variants set label = %s where id = %s", (label, variant_id))


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
    sha256: str, uploaded_by: Optional[str], source_url: str, retrieved_at,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into document_versions
              (document_id, storage_path, original_filename, mime_type, byte_size, sha256, uploaded_by,
               source_url, retrieved_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (document_id, storage_path, original_filename, mime_type, byte_size, sha256, uploaded_by,
             source_url, retrieved_at),
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


def get_spec_attribute_by_key(conn: psycopg.Connection, key: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from spec_attributes where key = %s", (key,))
        return cur.fetchone()


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

def get_current_claim(
    conn: psycopg.Connection, product_id: str, spec_attribute_id: str, *,
    variant_id: Optional[str] = None,
) -> Optional[dict]:
    # "is not distinct from" (not plain "=") so variant_id=None correctly
    # matches product-level rows (variant_id is null) instead of matching
    # nothing -- SQL's "x = null" is never true, even when x is itself null.
    with conn.cursor() as cur:
        cur.execute(
            """
            select * from claims
             where product_id = %s and spec_attribute_id = %s and is_current
               and variant_id is not distinct from %s
            """,
            (product_id, spec_attribute_id, variant_id),
        )
        return cur.fetchone()


def insert_claim(conn: psycopg.Connection, claim: dict, *, is_current: bool = True) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into claims
              (product_id, variant_id, spec_attribute_id, value_numeric, value_numeric_max,
               value_text, value_bool, value_enum, unit, presence, confidence,
               document_version_id, chunk_id, page_number, source_snippet, derivation_note,
               extraction_run_id, is_current)
            values (%(product_id)s, %(variant_id)s, %(spec_attribute_id)s, %(value_numeric)s, %(value_numeric_max)s,
                    %(value_text)s, %(value_bool)s, %(value_enum)s, %(unit)s, %(presence)s, %(confidence)s,
                    %(document_version_id)s, %(chunk_id)s, %(page_number)s, %(source_snippet)s, %(derivation_note)s,
                    %(extraction_run_id)s, %(is_current)s)
            returning id
            """,
            {"variant_id": claim.get("variant_id"), "derivation_note": claim.get("derivation_note"),
             **claim, "is_current": is_current},
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


# ---------------------------------------------------------------------
# Review queue -- found-but-undefined specs, never claims (see
# 0011_review_queue.sql for why that's structural, not conventional)
# ---------------------------------------------------------------------

def insert_review_queue_entry(
    conn: psycopg.Connection, *, found_term: str, source_snippet: str,
    document_version_id: str, page_number: int,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into review_queue (found_term, source_snippet, document_version_id, page_number)
            values (%s, %s, %s, %s)
            returning id
            """,
            (found_term, source_snippet, document_version_id, page_number),
        )
        return cur.fetchone()["id"]


def list_review_queue(conn: psycopg.Connection, status: Optional[str] = None) -> list[dict]:
    with conn.cursor() as cur:
        if status:
            cur.execute(
                "select * from review_queue where status = %s order by created_at", (status,)
            )
        else:
            cur.execute("select * from review_queue order by created_at")
        return cur.fetchall()


def get_review_queue_entry(conn: psycopg.Connection, entry_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from review_queue where id = %s", (entry_id,))
        return cur.fetchone()


def promote_review_queue_entry(conn: psycopg.Connection, entry_id: str, spec_key: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update review_queue set status = 'promoted', promoted_to_key = %s where id = %s",
            (spec_key, entry_id),
        )


def reject_review_queue_entry(conn: psycopg.Connection, entry_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("update review_queue set status = 'rejected' where id = %s", (entry_id,))


# ---------------------------------------------------------------------
# Spec-diff (Part 4) -- resolving visibility_answers into answer_claims.
# ---------------------------------------------------------------------

def get_visibility_run(conn: psycopg.Connection, run_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select vr.*, b.name as brand_name
              from visibility_runs vr
              join brands b on b.id = vr.brand_id
             where vr.id = %s
            """,
            (run_id,),
        )
        return cur.fetchone()


def get_visibility_answers_for_run(conn: psycopg.Connection, run_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "select id, prompt_id, response_text from visibility_answers where run_id = %s order by created_at",
            (run_id,),
        )
        return cur.fetchall()


def has_answer_claims(conn: psycopg.Connection, answer_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("select 1 from answer_claims where answer_id = %s limit 1", (answer_id,))
        return cur.fetchone() is not None


def resolve_product_by_name(conn: psycopg.Connection, name: str) -> Optional[dict]:
    """Best pg_trgm fuzzy match across ALL products, not scoped to any one
    brand -- a competitor's product belongs to a different brand_id, and
    resolving it is exactly the point of extracting competitor claims.
    Returns None only when `products` itself is empty; otherwise always
    returns the best match found however weak -- the caller decides
    whether resolution_score clears the acceptance threshold, this never
    guesses on its own."""
    with conn.cursor() as cur:
        cur.execute(
            "select id, name, similarity(name, %s) as score from products order by score desc limit 1",
            (name,),
        )
        return cur.fetchone()


def set_product_url(conn: psycopg.Connection, product_id: str, product_url: str) -> None:
    with conn.cursor() as cur:
        cur.execute("update products set product_url = %s where id = %s", (product_url, product_id))


def set_brand_website(conn: psycopg.Connection, brand_id: str, website: str) -> None:
    with conn.cursor() as cur:
        cur.execute("update brands set website = %s where id = %s", (website, brand_id))


def get_products_for_brand(conn: psycopg.Connection, brand_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from products where brand_id = %s order by name", (brand_id,))
        return cur.fetchall()


def get_current_claims_for_product(conn: psycopg.Connection, product_id: str) -> list[dict]:
    """Current claims joined with their spec_attributes row (key/data_type/
    name) -- page_visibility.py needs data_type to decide HOW to check for
    a claim's value as literal text (numeric vs. text/enum vs. skip for
    boolean)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.*, sa.key as spec_key, sa.data_type as spec_data_type, sa.name_en as spec_name_en
              from claims c
              join spec_attributes sa on sa.id = c.spec_attribute_id
             where c.product_id = %s and c.is_current
            """,
            (product_id,),
        )
        return cur.fetchall()


def insert_answer_claim(conn: psycopg.Connection, row: dict) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into answer_claims
              (answer_id, brand_name, product_name, is_competitor, resolved_product_id, resolution_score,
               spec_attribute_id, value_numeric, value_numeric_max, value_text, value_bool, value_enum, unit,
               source_quote, verdict, compared_claim_id, confusable_key)
            values (%(answer_id)s, %(brand_name)s, %(product_name)s, %(is_competitor)s, %(resolved_product_id)s,
                    %(resolution_score)s, %(spec_attribute_id)s, %(value_numeric)s, %(value_numeric_max)s,
                    %(value_text)s, %(value_bool)s, %(value_enum)s, %(unit)s, %(source_quote)s, %(verdict)s,
                    %(compared_claim_id)s, %(confusable_key)s)
            returning id
            """,
            row,
        )
        return cur.fetchone()["id"]


# ---------------------------------------------------------------------
# Crawlability (Part 5a)
# ---------------------------------------------------------------------

def insert_crawlability_check(
    conn: psycopg.Connection, *, brand_id: str, website_url: str, robots_txt_url: str,
    http_status: Optional[int], robots_txt_body: Optional[str], error: Optional[str],
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into crawlability_checks (brand_id, website_url, robots_txt_url, http_status, robots_txt_body, error)
            values (%s, %s, %s, %s, %s, %s)
            returning id
            """,
            (brand_id, website_url, robots_txt_url, http_status, robots_txt_body, error),
        )
        return cur.fetchone()["id"]


def insert_crawlability_agent(
    conn: psycopg.Connection, *, check_id: str, agent_name: str, allowed: Optional[bool],
    matched_rule: Optional[str], matched_group: Optional[str],
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into crawlability_agents (check_id, agent_name, allowed, matched_rule, matched_group)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (check_id, agent_name, allowed, matched_rule, matched_group),
        )
        return cur.fetchone()["id"]


# ---------------------------------------------------------------------
# Page-spec-visibility (Part 5b)
# ---------------------------------------------------------------------

def insert_page_spec_visibility(conn: psycopg.Connection, row: dict) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into page_spec_visibility
              (product_id, claim_id, product_url, http_status, value_found_as_text,
               schema_org_product_present, error)
            values (%(product_id)s, %(claim_id)s, %(product_url)s, %(http_status)s, %(value_found_as_text)s,
                    %(schema_org_product_present)s, %(error)s)
            returning id
            """,
            row,
        )
        return cur.fetchone()["id"]


# ---------------------------------------------------------------------
# Scoring (Part 6) -- all scoped to one visibility_runs id.
# ---------------------------------------------------------------------

def get_answers_with_intent_for_run(conn: psycopg.Connection, run_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select va.response_text, p.intent::text as intent
              from visibility_answers va
              join prompts p on p.id = va.prompt_id
             where va.run_id = %s
            """,
            (run_id,),
        )
        return cur.fetchall()


def get_distinct_brand_mentions_for_run(conn: psycopg.Connection, run_id: str) -> list[dict]:
    """One row per DISTINCT (answer, brand) pair -- share_of_voice counts
    conversations a brand appeared in, not raw claim volume. Brand names
    are deduped case/whitespace-insensitively within an answer so e.g.
    "Acme" and "ACME" claimed twice in one answer don't double-count."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct on (ac.answer_id, lower(trim(ac.brand_name)))
                   p.intent::text as intent, ac.is_competitor
              from answer_claims ac
              join visibility_answers va on va.id = ac.answer_id
              join prompts p on p.id = va.prompt_id
             where va.run_id = %s
             order by ac.answer_id, lower(trim(ac.brand_name))
            """,
            (run_id,),
        )
        return cur.fetchall()


def get_own_brand_claim_verdicts_for_run(conn: psycopg.Connection, run_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select ac.verdict::text as verdict, p.intent::text as intent
              from answer_claims ac
              join visibility_answers va on va.id = ac.answer_id
              join prompts p on p.id = va.prompt_id
             where va.run_id = %s and not ac.is_competitor
            """,
            (run_id,),
        )
        return cur.fetchall()


def has_any_answer_claims_for_run(conn: psycopg.Connection, run_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from answer_claims ac join visibility_answers va on va.id = ac.answer_id "
            "where va.run_id = %s limit 1",
            (run_id,),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------
# PDF report (Part 7)
# ---------------------------------------------------------------------

def get_worst_findings_for_run(conn: psycopg.Connection, run_id: str, limit: int = 10) -> list[dict]:
    """Own-brand incorrect/likely_confusion claims, worst (flatly wrong)
    first -- everything a citation needs to trace back to document+page+
    quote is joined in here: the ground-truth claim via compared_claim_id,
    and from there its document_version -> document."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select
              ac.id as answer_claim_id, ac.verdict::text as verdict, ac.product_name, ac.source_quote,
              ac.value_numeric, ac.value_numeric_max, ac.value_text, ac.value_bool, ac.value_enum, ac.unit,
              ac.confusable_key,
              sa.key as spec_key, sa.name_nl as spec_name_nl, sa.name_en as spec_name_en,
              p.text as prompt_text, p.intent::text as intent,
              va.citations, va.resolved_model_version,
              vr.engine::text as engine,
              gt.value_numeric as gt_value_numeric, gt.value_numeric_max as gt_value_numeric_max,
              gt.value_text as gt_value_text, gt.value_bool as gt_value_bool, gt.value_enum as gt_value_enum,
              gt.unit as gt_unit, gt.page_number as gt_page_number, gt.source_snippet as gt_source_snippet,
              d.title as gt_document_title
            from answer_claims ac
            join visibility_answers va on va.id = ac.answer_id
            join visibility_runs vr on vr.id = va.run_id
            join prompts p on p.id = va.prompt_id
            join spec_attributes sa on sa.id = ac.spec_attribute_id
            left join claims gt on gt.id = ac.compared_claim_id
            left join document_versions dv on dv.id = gt.document_version_id
            left join documents d on d.id = dv.document_id
            where va.run_id = %s and not ac.is_competitor and ac.verdict in ('incorrect', 'likely_confusion')
            order by (ac.verdict = 'incorrect') desc, ac.created_at
            limit %s
            """,
            (run_id, limit),
        )
        return cur.fetchall()


def get_brand_mentions_by_intent_for_run(conn: psycopg.Connection, run_id: str) -> list[dict]:
    """One row per DISTINCT (answer, brand) pair, for mention-count
    aggregation in the competitive picture -- same dedup rule as Part 6's
    share_of_voice, but keeping brand_name/is_competitor for display."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct on (ac.answer_id, lower(trim(ac.brand_name)))
                   p.intent::text as intent, ac.brand_name, ac.is_competitor
              from answer_claims ac
              join visibility_answers va on va.id = ac.answer_id
              join prompts p on p.id = va.prompt_id
             where va.run_id = %s
             order by ac.answer_id, lower(trim(ac.brand_name))
            """,
            (run_id,),
        )
        return cur.fetchall()


def get_claim_verdicts_by_brand_for_run(conn: psycopg.Connection, run_id: str) -> list[dict]:
    """Every answer_claims row (not deduped) with brand/verdict, for the
    per-brand accuracy breakdown in the competitive picture."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.intent::text as intent, ac.brand_name, ac.is_competitor, ac.verdict::text as verdict
              from answer_claims ac
              join visibility_answers va on va.id = ac.answer_id
              join prompts p on p.id = va.prompt_id
             where va.run_id = %s
            """,
            (run_id,),
        )
        return cur.fetchall()


def get_documentation_gaps_for_run(conn: psycopg.Connection, run_id: str) -> list[dict]:
    """Own-brand unverifiable_spec claims -- the AI made a claim, but there
    is no current ground truth (no row, or explicitly not_stated) to check
    it against. Never an error; framed as a documentation gap."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select ac.product_name, sa.key as spec_key, sa.name_nl as spec_name_nl, sa.name_en as spec_name_en,
                   p.intent::text as intent, p.text as prompt_text, ac.source_quote
              from answer_claims ac
              join visibility_answers va on va.id = ac.answer_id
              join prompts p on p.id = va.prompt_id
              join spec_attributes sa on sa.id = ac.spec_attribute_id
             where va.run_id = %s and not ac.is_competitor and ac.verdict = 'unverifiable_spec'
             order by sa.key, ac.product_name
            """,
            (run_id,),
        )
        return cur.fetchall()


def get_latest_crawlability_check(conn: psycopg.Connection, brand_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "select * from crawlability_checks where brand_id = %s order by fetched_at desc limit 1",
            (brand_id,),
        )
        return cur.fetchone()


def get_crawlability_agents(conn: psycopg.Connection, check_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from crawlability_agents where check_id = %s order by agent_name", (check_id,))
        return cur.fetchall()


def get_latest_page_visibility_checks(conn: psycopg.Connection, brand_id: str) -> list[dict]:
    """Most recent check per (product, claim) -- page_spec_visibility is
    append-only history, so a naive `where value_found_as_text = false`
    could surface a STALE finding that's since been fixed. Returns every
    latest check regardless of outcome; callers filter to gaps themselves
    so "latest" and "is a gap" never get conflated into one WHERE clause
    fighting the DISTINCT ON ordering."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct on (pv.product_id, pv.claim_id)
                   pv.*, pr.name as product_name, sa.key as spec_key,
                   sa.name_nl as spec_name_nl, sa.name_en as spec_name_en
              from page_spec_visibility pv
              join products pr on pr.id = pv.product_id
              join claims c on c.id = pv.claim_id
              join spec_attributes sa on sa.id = c.spec_attribute_id
             where pr.brand_id = %s
             order by pv.product_id, pv.claim_id, pv.fetched_at desc
            """,
            (brand_id,),
        )
        return cur.fetchall()
