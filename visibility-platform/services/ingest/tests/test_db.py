import pytest

from ingest import db
from ingest.config import EMBEDDING_DIMENSIONS


def _make_org(conn, slug="acme"):
    with conn.cursor() as cur:
        cur.execute("insert into organizations (name, slug) values (%s, %s) returning id", (slug.title(), slug))
        return cur.fetchone()["id"]


def _make_brand(conn, org_id, slug="acme-brand"):
    with conn.cursor() as cur:
        cur.execute("insert into brands (organization_id, name, slug) values (%s, %s, %s) returning id",
                    (org_id, slug.title(), slug))
        return cur.fetchone()["id"]


def test_organization_brand_product_lookups(conn):
    org_id = _make_org(conn)
    brand_id = _make_brand(conn, org_id)
    product_id = db.create_product(conn, brand_id=brand_id, category_id=None, name="Acme 211", slug="acme-211")

    assert db.get_organization_by_slug(conn, "acme")["id"] == org_id
    assert db.get_organization_by_slug(conn, "nonexistent") is None
    assert db.get_brand_by_slug(conn, "acme-brand")["id"] == brand_id
    assert db.get_product_by_slug(conn, "acme-211")["id"] == product_id
    assert db.get_category_by_code(conn, "brandwerende_systeemwanden") is not None


def test_category_ancestor_codes_walks_the_tree(conn):
    # Codes prefixed __test_ so this can never collide with seed.sql's real
    # category tree (which now genuinely includes isolatiematerialen ->
    # steenwol -> ... as of 0006/seed.sql) -- conftest.py applies seed.sql
    # before tests run, so an unprefixed "steenwol" here would hit
    # product_categories.code's unique constraint against the real row.
    with conn.cursor() as cur:
        cur.execute(
            "insert into product_categories (code, name_nl, name_en) values (%s, %s, %s) returning id",
            ("__test_isolatie_root", "Isolatie", "Insulation"),
        )
        root_id = cur.fetchone()["id"]
        cur.execute(
            "insert into product_categories (code, name_nl, name_en, parent_id) values (%s, %s, %s, %s) returning id",
            ("__test_steenwol_leaf", "Steenwol", "Rock wool", root_id),
        )
        leaf_id = cur.fetchone()["id"]

    assert db.get_category_ancestor_codes(conn, leaf_id) == ["__test_steenwol_leaf", "__test_isolatie_root"]
    assert db.get_category_ancestor_codes(conn, root_id) == ["__test_isolatie_root"]
    assert db.get_category_ancestor_codes(conn, None) == []


def test_document_and_version_lifecycle(conn):
    org_id = _make_org(conn)
    brand_id = _make_brand(conn, org_id)
    product_id = db.create_product(conn, brand_id=brand_id, category_id=None, name="Acme 211", slug="acme-211")

    document_id = db.create_document(conn, organization_id=org_id, product_id=product_id,
                                      title="Datasheet", kind="datasheet")
    assert db.get_document(conn, document_id)["title"] == "Datasheet"

    assert db.find_version_by_sha256(conn, "a" * 64) is None
    version_id = db.create_document_version(
        conn, document_id=document_id, storage_path="acme/aa/doc.pdf",
        original_filename="doc.pdf", mime_type="application/pdf", byte_size=100,
        sha256="a" * 64, uploaded_by=None,
    )
    assert db.find_version_by_sha256(conn, "a" * 64)["id"] == version_id

    # The trigger from 0001_init.sql should have repointed current_version_id.
    assert db.get_document(conn, document_id)["current_version_id"] == version_id

    joined = db.get_document_version(conn, version_id)
    assert joined["organization_id"] == org_id
    assert joined["product_id"] == product_id


def test_page_and_chunk_lifecycle(conn):
    org_id = _make_org(conn)
    brand_id = _make_brand(conn, org_id)
    product_id = db.create_product(conn, brand_id=brand_id, category_id=None, name="Acme 211", slug="acme-211")
    document_id = db.create_document(conn, organization_id=org_id, product_id=product_id, title=None, kind="datasheet")
    version_id = db.create_document_version(
        conn, document_id=document_id, storage_path="acme/aa/doc.pdf", original_filename="doc.pdf",
        mime_type="application/pdf", byte_size=1, sha256="b" * 64, uploaded_by=None,
    )

    db.insert_page(conn, version_id, 1, "hello world", {"words": []})
    assert db.get_page_text(conn, version_id, 1) == "hello world"
    db.insert_page(conn, version_id, 1, "hello world v2", None)
    assert db.get_page_text(conn, version_id, 1) == "hello world v2"

    chunk_id = db.insert_chunk(
        conn, document_version_id=version_id, chunk_index=0, page_start=1, page_end=1,
        chunk_type="prose", heading_path=["Intro"], content="some prose", token_count=2,
    )
    assert [c["id"] for c in db.get_chunks_missing_embeddings(conn, version_id)] == [chunk_id]
    db.set_chunk_embedding(conn, chunk_id, [0.1] * EMBEDDING_DIMENSIONS)
    assert db.get_chunks_missing_embeddings(conn, version_id) == []
    assert len(db.get_all_chunks(conn, version_id)) == 1


def test_document_chunks_embedding_column_enforces_config_dimensions(conn):
    # The migration's vector(1536) is a literal in an already-applied SQL
    # file -- it can't reference config.EMBEDDING_DIMENSIONS directly (see
    # the comment above that constant). This is the live check that stands
    # in for that reference: if a future change ever lets the two drift
    # apart, this test catches it immediately instead of failing silently
    # at embed time.
    org_id = _make_org(conn, slug="dimcheck")
    brand_id = _make_brand(conn, org_id, slug="dimcheck-brand")
    product_id = db.create_product(conn, brand_id=brand_id, category_id=None, name="Dim Check", slug="dim-check")
    document_id = db.create_document(conn, organization_id=org_id, product_id=product_id, title=None, kind="datasheet")
    version_id = db.create_document_version(
        conn, document_id=document_id, storage_path="dimcheck/aa/doc.pdf", original_filename="doc.pdf",
        mime_type="application/pdf", byte_size=1, sha256="e" * 64, uploaded_by=None,
    )
    chunk_id = db.insert_chunk(
        conn, document_version_id=version_id, chunk_index=0, page_start=1, page_end=1,
        chunk_type="prose", heading_path=[], content="n/a", token_count=1,
    )

    # Exactly EMBEDDING_DIMENSIONS floats: the column must accept it.
    db.set_chunk_embedding(conn, chunk_id, [0.1] * EMBEDDING_DIMENSIONS)

    # One float short: the column must reject it. pgvector raises this as
    # a plain exception, not a specific psycopg error class, so assert on
    # the message rather than the exception type.
    with pytest.raises(Exception, match="expected .* dimensions"):
        db.set_chunk_embedding(conn, chunk_id, [0.1] * (EMBEDDING_DIMENSIONS - 1))
    conn.rollback()


def test_extraction_run_lifecycle(conn):
    org_id = _make_org(conn)
    brand_id = _make_brand(conn, org_id)
    product_id = db.create_product(conn, brand_id=brand_id, category_id=None, name="Acme 211", slug="acme-211")
    document_id = db.create_document(conn, organization_id=org_id, product_id=product_id, title=None, kind="datasheet")
    version_id = db.create_document_version(
        conn, document_id=document_id, storage_path="acme/aa/doc.pdf", original_filename="doc.pdf",
        mime_type="application/pdf", byte_size=1, sha256="c" * 64, uploaded_by=None,
    )

    run_id = db.start_extraction_run(conn, document_version_id=version_id, parser_version="1",
                                      prompt_version="1", model="test-model")
    db.finish_extraction_run(conn, run_id, status="succeeded", stats={"claims": 3}, cost_usd=0.01)

    with conn.cursor() as cur:
        cur.execute("select status, stats, cost_usd, finished_at from extraction_runs where id = %s", (run_id,))
        row = cur.fetchone()
    assert row["status"] == "succeeded"
    assert row["stats"]["claims"] == 3
    assert row["finished_at"] is not None


def test_review_queue_lifecycle(conn):
    org_id = _make_org(conn, slug="iota")
    document_id = db.create_document(conn, organization_id=org_id, product_id=None, title=None, kind="datasheet")
    version_id = db.create_document_version(
        conn, document_id=document_id, storage_path="iota/aa/doc.pdf", original_filename="doc.pdf",
        mime_type="application/pdf", byte_size=1, sha256="d" * 64, uploaded_by=None,
    )

    entry_id = db.insert_review_queue_entry(
        conn, found_term="water_resistant", source_snippet="Water resistant: yes",
        document_version_id=version_id, page_number=3,
    )

    entry = db.get_review_queue_entry(conn, entry_id)
    assert entry["status"] == "new"
    assert entry["found_term"] == "water_resistant"
    assert entry["promoted_to_key"] is None

    assert [e["id"] for e in db.list_review_queue(conn, status="new")] == [entry_id]
    assert db.list_review_queue(conn, status="rejected") == []

    assert db.get_spec_attribute_by_key(conn, "thickness_mm") is not None
    assert db.get_spec_attribute_by_key(conn, "not_a_real_key") is None

    db.promote_review_queue_entry(conn, entry_id, "thickness_mm")
    promoted = db.get_review_queue_entry(conn, entry_id)
    assert promoted["status"] == "promoted"
    assert promoted["promoted_to_key"] == "thickness_mm"


def test_review_queue_reject(conn):
    org_id = _make_org(conn, slug="kappa")
    document_id = db.create_document(conn, organization_id=org_id, product_id=None, title=None, kind="datasheet")
    version_id = db.create_document_version(
        conn, document_id=document_id, storage_path="kappa/aa/doc.pdf", original_filename="doc.pdf",
        mime_type="application/pdf", byte_size=1, sha256="e" * 64, uploaded_by=None,
    )
    entry_id = db.insert_review_queue_entry(
        conn, found_term="irrelevant_note", source_snippet="Some irrelevant note",
        document_version_id=version_id, page_number=1,
    )

    db.reject_review_queue_entry(conn, entry_id)

    assert db.get_review_queue_entry(conn, entry_id)["status"] == "rejected"
