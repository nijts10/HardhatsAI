from ingest import db


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
    with conn.cursor() as cur:
        cur.execute(
            "insert into product_categories (code, name_nl, name_en) values (%s, %s, %s) returning id",
            ("isolatie", "Isolatie", "Insulation"),
        )
        root_id = cur.fetchone()["id"]
        cur.execute(
            "insert into product_categories (code, name_nl, name_en, parent_id) values (%s, %s, %s, %s) returning id",
            ("steenwol", "Steenwol", "Rock wool", root_id),
        )
        leaf_id = cur.fetchone()["id"]

    assert db.get_category_ancestor_codes(conn, leaf_id) == ["steenwol", "isolatie"]
    assert db.get_category_ancestor_codes(conn, root_id) == ["isolatie"]
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
    db.set_chunk_embedding(conn, chunk_id, [0.1] * 1536)
    assert db.get_chunks_missing_embeddings(conn, version_id) == []
    assert len(db.get_all_chunks(conn, version_id)) == 1


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
