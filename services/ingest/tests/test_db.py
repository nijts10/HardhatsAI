from ingest import db


def _make_supplier(conn, slug="rockwool"):
    with conn.cursor() as cur:
        cur.execute(
            "insert into supplier_companies (name, slug) values (%s, %s) returning id",
            (slug.title(), slug),
        )
        return cur.fetchone()["id"]


def _make_document(conn, supplier_company_id, sha256):
    return db.insert_document(
        conn,
        supplier_company_id=supplier_company_id,
        uploaded_by=None,
        source="crawled",
        source_url=None,
        storage_bucket="supplier-documents",
        storage_path=f"rockwool/{sha256[:2]}/doc.pdf",
        original_filename="datasheet.pdf",
        mime_type="application/pdf",
        byte_size=123,
        sha256=sha256,
        kind="datasheet",
    )


def test_find_document_by_sha256_roundtrip(conn):
    supplier_id = _make_supplier(conn)
    document_id = _make_document(conn, supplier_id, "a" * 64)
    assert db.find_document_by_sha256(conn, "a" * 64)["id"] == document_id
    assert db.find_document_by_sha256(conn, "b" * 64) is None


def test_set_document_status_transitions_and_timestamps(conn):
    supplier_id = _make_supplier(conn)
    document_id = _make_document(conn, supplier_id, "a" * 64)

    db.set_document_status(conn, document_id, "parsed", page_count=7)
    doc = db.get_document(conn, document_id)
    assert doc["status"] == "parsed"
    assert doc["page_count"] == 7
    assert doc["parsed_at"] is not None

    db.set_document_status(conn, document_id, "indexed")
    doc = db.get_document(conn, document_id)
    assert doc["status"] == "indexed"
    assert doc["indexed_at"] is not None


def test_page_insert_and_lookup(conn):
    supplier_id = _make_supplier(conn)
    document_id = _make_document(conn, supplier_id, "a" * 64)

    db.insert_page(conn, document_id, 1, "hello world", {"words": []})
    assert db.get_page_text(conn, document_id, 1) == "hello world"
    assert db.get_page_text(conn, document_id, 2) is None

    # Re-parsing the same page (e.g. a retried job) should update, not duplicate.
    db.insert_page(conn, document_id, 1, "hello world v2", None)
    assert db.get_page_text(conn, document_id, 1) == "hello world v2"


def test_chunk_insert_embedding_lifecycle(conn):
    supplier_id = _make_supplier(conn)
    document_id = _make_document(conn, supplier_id, "a" * 64)

    chunk_id = db.insert_chunk(
        conn, document_id=document_id, chunk_index=0, page_start=1, page_end=1,
        chunk_type="prose", heading_path=["Intro"], content="some prose content", token_count=3,
    )

    pending = db.get_chunks_missing_embeddings(conn, document_id)
    assert [c["id"] for c in pending] == [chunk_id]

    db.set_chunk_embedding(conn, chunk_id, [0.1] * 1536)
    assert db.get_chunks_missing_embeddings(conn, document_id) == []

    all_chunks = db.get_all_chunks(conn, document_id)
    assert len(all_chunks) == 1
    assert all_chunks[0]["heading_path"] == ["Intro"]


def test_enqueue_and_claim_job_marks_running_and_increments_attempts(conn):
    job_id = db.enqueue_job(conn, "parse_document", {"document_id": "x"})
    conn.commit()  # claim_job runs its own update; needs the insert visible

    claimed = db.claim_job(conn, "worker-1", ["parse_document"])
    conn.commit()
    assert claimed["id"] == job_id
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["locked_by"] == "worker-1"

    # Already running: a second worker asking for the same kind gets nothing.
    assert db.claim_job(conn, "worker-2", ["parse_document"]) is None


def test_claim_job_filters_by_kind(conn):
    db.enqueue_job(conn, "embed_chunks", {"document_id": "x"})
    conn.commit()
    assert db.claim_job(conn, "worker-1", ["parse_document"]) is None
    claimed = db.claim_job(conn, "worker-1", ["embed_chunks"])
    assert claimed["kind"] == "embed_chunks"


def test_mark_job_succeeded_and_failed(conn):
    job_id = db.enqueue_job(conn, "parse_document", {"document_id": "x"})
    conn.commit()
    db.claim_job(conn, "worker-1", ["parse_document"])
    conn.commit()

    db.mark_job_succeeded(conn, job_id)
    with conn.cursor() as cur:
        cur.execute("select status from jobs where id = %s", (job_id,))
        assert cur.fetchone()["status"] == "succeeded"


def test_mark_job_failed_goes_dead_after_max_attempts(conn):
    job_id = db.enqueue_job(conn, "parse_document", {"document_id": "x"})
    conn.commit()
    db.claim_job(conn, "worker-1", ["parse_document"])  # attempts -> 1
    conn.commit()

    db.mark_job_failed(conn, job_id, "boom", max_attempts=3)
    with conn.cursor() as cur:
        cur.execute("select status, last_error from jobs where id = %s", (job_id,))
        row = cur.fetchone()
    assert row["status"] == "pending"  # attempts (1) < max_attempts (3)
    assert row["last_error"] == "boom"

    db.mark_job_failed(conn, job_id, "boom again", max_attempts=1)
    with conn.cursor() as cur:
        cur.execute("select status from jobs where id = %s", (job_id,))
        assert cur.fetchone()["status"] == "dead"
