"""Runs persist_extracted_products against the real schema (supabase/migrations
applied verbatim, see conftest.py) so the tricky append-only supersede/
is_current bookkeeping is checked against the actual constraints, not a mock."""
from ingest import db
from ingest.persistence import persist_extracted_products, slugify
from ingest.verification import PLAUSIBILITY_RANGES as _PLAUSIBLE_RANGES


def _spec_defs_by_key(conn):
    return {d["key"]: d for d in db.get_spec_definitions(conn)}


def _category_by_code(conn):
    with conn.cursor() as cur:
        cur.execute("select id, code from product_categories")
        return {c["code"]: c for c in cur.fetchall()}


def _make_supplier(conn, slug="rockwool"):
    with conn.cursor() as cur:
        cur.execute(
            "insert into supplier_companies (name, slug) values (%s, %s) returning id",
            (slug.title(), slug),
        )
        return cur.fetchone()["id"]


def _make_document(conn, supplier_company_id, sha256=None):
    return db.insert_document(
        conn,
        supplier_company_id=supplier_company_id,
        uploaded_by=None,
        source="crawled",
        source_url=None,
        storage_bucket="supplier-documents",
        storage_path=f"rockwool/{sha256 or 'ab'}/doc.pdf",
        original_filename="datasheet.pdf",
        mime_type="application/pdf",
        byte_size=123,
        sha256=sha256 or ("abcdef" + "0" * 58),
        kind="datasheet",
    )


def test_slugify_is_ascii_lowercase_hyphenated():
    assert slugify("Rockwool 211", "RW-211") == "rockwool-211-rw-211"
    assert slugify(None, None) == "product"


def test_persist_inserts_product_and_stated_spec_with_citation(conn):
    supplier_id = _make_supplier(conn)
    document_id = _make_document(conn, supplier_id)
    run_id = db.start_extraction_run(
        conn, document_id=document_id, parser_version="1", prompt_version="1", model="test-model",
    )

    page_text = "Technische gegevens\nBrandwerendheid EI 60 volgens NEN-EN 13501-2\n"
    extracted = [{
        "canonical_name": "Rockwool 211 systeemwand",
        "manufacturer_ref": "RW-211",
        "category_code": "systeemwand",
        "specs": [
            {
                "key": "fire_resistance_ei",
                "value_numeric": 60,
                "unit": "min",
                "presence": "stated",
                "page_number": 1,
                "source_snippet": "Brandwerendheid EI 60 volgens NEN-EN 13501-2",
                "confidence": 0.95,
            },
            {"key": "airborne_sound_reduction_rw", "presence": "not_stated"},
            {"key": "not_a_real_key", "value_text": "should be dropped", "presence": "stated",
             "page_number": 1, "source_snippet": "irrelevant"},
        ],
    }]

    stats = persist_extracted_products(
        conn,
        document_id=document_id,
        supplier_company_id=supplier_id,
        extraction_run_id=run_id,
        extracted_products=extracted,
        spec_defs_by_key=_spec_defs_by_key(conn),
        category_by_code=_category_by_code(conn),
        document_chunks=[],
        get_page_text=lambda page_number: page_text,
    )

    assert stats.products_created_or_updated == 1
    assert stats.specs_inserted == 2  # fire_resistance_ei (stated) + rw (not_stated)
    assert stats.specs_rejected == 0  # unknown key silently dropped, not "rejected"

    with conn.cursor() as cur:
        cur.execute("select * from products where manufacturer_ref = 'RW-211'")
        product = cur.fetchone()
    assert product["canonical_name"] == "Rockwool 211 systeemwand"
    assert product["status"] == "draft"

    with conn.cursor() as cur:
        cur.execute(
            """
            select ps.*, sd.key from product_specs ps
              join spec_definitions sd on sd.id = ps.spec_definition_id
             where ps.product_id = %s and ps.is_current
            """,
            (product["id"],),
        )
        specs = {row["key"]: row for row in cur.fetchall()}

    assert specs["fire_resistance_ei"]["value_numeric"] == 60
    assert specs["fire_resistance_ei"]["presence"] == "stated"
    assert specs["fire_resistance_ei"]["document_id"] == document_id
    assert specs["fire_resistance_ei"]["page_number"] == 1
    assert specs["airborne_sound_reduction_rw"]["presence"] == "not_stated"
    assert specs["airborne_sound_reduction_rw"]["value_numeric"] is None
    assert "not_a_real_key" not in specs


def test_persist_rejects_uncited_stated_value_but_keeps_product(conn):
    supplier_id = _make_supplier(conn)
    document_id = _make_document(conn, supplier_id)
    run_id = db.start_extraction_run(
        conn, document_id=document_id, parser_version="1", prompt_version="1", model="test-model",
    )

    extracted = [{
        "canonical_name": "Rockwool 211 systeemwand",
        "manufacturer_ref": "RW-211",
        "category_code": "systeemwand",
        "specs": [{
            "key": "fire_resistance_ei",
            "value_numeric": 60,
            "unit": "min",
            "presence": "stated",
            "page_number": 1,
            "source_snippet": "This text does not appear on the page.",
        }],
    }]

    stats = persist_extracted_products(
        conn,
        document_id=document_id,
        supplier_company_id=supplier_id,
        extraction_run_id=run_id,
        extracted_products=extracted,
        spec_defs_by_key=_spec_defs_by_key(conn),
        category_by_code=_category_by_code(conn),
        document_chunks=[],
        get_page_text=lambda page_number: "Something entirely different is on this page.",
    )

    assert stats.products_created_or_updated == 1
    assert stats.specs_inserted == 0
    assert stats.specs_rejected == 1
    assert "not found verbatim" in stats.rejections[0]

    with conn.cursor() as cur:
        cur.execute(
            "select count(*) as n from product_specs ps join products p on p.id = ps.product_id "
            "where p.manufacturer_ref = 'RW-211'"
        )
        assert cur.fetchone()["n"] == 0


def test_persist_supersedes_changed_value_and_bumps_version(conn):
    supplier_id = _make_supplier(conn)
    document_id_1 = _make_document(conn, supplier_id, sha256="a" * 64)

    def make_extracted(value, snippet):
        return [{
            "canonical_name": "Rockwool 211 systeemwand",
            "manufacturer_ref": "RW-211",
            "category_code": "systeemwand",
            "specs": [{
                "key": "fire_resistance_ei", "value_numeric": value, "unit": "min",
                "presence": "stated", "page_number": 1, "source_snippet": snippet,
            }],
        }]

    run_id_1 = db.start_extraction_run(conn, document_id=document_id_1, parser_version="1",
                                        prompt_version="1", model="test-model")
    persist_extracted_products(
        conn, document_id=document_id_1, supplier_company_id=supplier_id, extraction_run_id=run_id_1,
        extracted_products=make_extracted(60, "EI 60 rating"),
        spec_defs_by_key=_spec_defs_by_key(conn), category_by_code=_category_by_code(conn),
        document_chunks=[], get_page_text=lambda p: "EI 60 rating",
    )

    with conn.cursor() as cur:
        cur.execute("select id, version from products where manufacturer_ref = 'RW-211'")
        product = cur.fetchone()
    assert product["version"] == 1

    # A revised datasheet (new document) states a different value for the same spec.
    document_id_2 = _make_document(conn, supplier_id, sha256="b" * 64)

    run_id_2 = db.start_extraction_run(conn, document_id=document_id_2, parser_version="1",
                                        prompt_version="1", model="test-model")
    persist_extracted_products(
        conn, document_id=document_id_2, supplier_company_id=supplier_id, extraction_run_id=run_id_2,
        extracted_products=make_extracted(90, "EI 90 rating"),
        spec_defs_by_key=_spec_defs_by_key(conn), category_by_code=_category_by_code(conn),
        document_chunks=[], get_page_text=lambda p: "EI 90 rating",
    )

    with conn.cursor() as cur:
        cur.execute(
            "select * from product_specs where product_id = %s order by created_at",
            (product["id"],),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    old, new = rows
    assert old["is_current"] is False
    assert old["superseded_by"] == new["id"]
    assert new["is_current"] is True
    assert new["value_numeric"] == 90

    with conn.cursor() as cur:
        cur.execute("select version from products where id = %s", (product["id"],))
        assert cur.fetchone()["version"] == 2


def test_recompute_data_completeness_reflects_stated_share(conn):
    supplier_id = _make_supplier(conn)
    document_id = _make_document(conn, supplier_id)
    run_id = db.start_extraction_run(conn, document_id=document_id, parser_version="1",
                                      prompt_version="1", model="test-model")

    spec_defs = _spec_defs_by_key(conn)
    relevant_keys = [
        k for k, d in spec_defs.items()
        if d["is_filterable"] and (not d["category_codes"] or "systeemwand" in d["category_codes"])
    ]
    assert relevant_keys, "seed data should define at least one systeemwand-relevant filterable spec"

    numeric_key = next(k for k in relevant_keys if spec_defs[k]["data_type"] in ("numeric", "numeric_range"))
    lo, hi = _PLAUSIBLE_RANGES.get(numeric_key, (1, 100))
    plausible_value = (lo + hi) / 2

    specs = [{"key": k, "presence": "not_stated"} for k in relevant_keys]
    specs[relevant_keys.index(numeric_key)] = {
        "key": numeric_key, "value_numeric": plausible_value, "unit": spec_defs[numeric_key].get("unit"),
        "presence": "stated", "page_number": 1, "source_snippet": f"value {plausible_value}",
    }

    persist_extracted_products(
        conn, document_id=document_id, supplier_company_id=supplier_id, extraction_run_id=run_id,
        extracted_products=[{
            "canonical_name": "Rockwool 211 systeemwand", "manufacturer_ref": "RW-211",
            "category_code": "systeemwand", "specs": specs,
        }],
        spec_defs_by_key=spec_defs, category_by_code=_category_by_code(conn),
        document_chunks=[], get_page_text=lambda p: f"value {plausible_value}",
    )

    with conn.cursor() as cur:
        cur.execute("select data_completeness from supplier_companies where id = %s", (supplier_id,))
        completeness = cur.fetchone()["data_completeness"]

    assert 0 < float(completeness) < 1
