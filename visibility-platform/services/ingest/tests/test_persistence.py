"""Runs persist_extracted_products against the real schema so the
append-only claims/supersede bookkeeping is checked against the actual
constraints, not a mock."""
from ingest import db
from ingest.persistence import persist_extracted_products, slugify


def _spec_attrs_by_key(conn):
    return {d["key"]: d for d in db.get_spec_attributes(conn)}


def _make_org_brand_product(conn, org_slug="acme"):
    with conn.cursor() as cur:
        cur.execute("insert into organizations (name, slug) values (%s, %s) returning id",
                    (org_slug.title(), org_slug))
        org_id = cur.fetchone()["id"]
        cur.execute("insert into brands (organization_id, name, slug) values (%s, %s, %s) returning id",
                    (org_id, org_slug.title(), org_slug))
        brand_id = cur.fetchone()["id"]
    return org_id, brand_id


def _make_document(conn, org_id, product_id, sha256):
    with conn.cursor() as cur:
        cur.execute(
            "insert into documents (organization_id, product_id, title, kind) values (%s, %s, %s, 'datasheet') returning id",
            (org_id, product_id, "Datasheet"),
        )
        document_id = cur.fetchone()["id"]
        cur.execute(
            "insert into document_versions (document_id, storage_path, sha256, uploaded_by) "
            "values (%s, %s, %s, null) returning id",
            (document_id, f"acme/{sha256[:2]}/doc.pdf", sha256),
        )
        version_id = cur.fetchone()["id"]
    return document_id, version_id


def test_slugify_is_ascii_lowercase_hyphenated():
    assert slugify("Acme 211", "RW-211") == "acme-211-rw-211"
    assert slugify(None, None) == "product"


def test_persist_inserts_product_and_stated_claim_with_citation(conn):
    org_id, brand_id = _make_org_brand_product(conn)
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder"))
        placeholder_product_id = cur.fetchone()["id"]
    document_id, version_id = _make_document(conn, org_id, placeholder_product_id, "a" * 64)

    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id",
            (version_id,),
        )
        run_id = cur.fetchone()["id"]

    page_text = "Technische gegevens\nBrandwerendheid EI 60 volgens NEN-EN 13501-2\n"
    extracted = [{
        "name": "Acme 211",
        "manufacturer_ref": "RW-211",
        "claims": [
            {"key": "fire_resistance_ei", "value_numeric": 60, "unit": "min", "presence": "stated",
             "page_number": 1, "source_snippet": "Brandwerendheid EI 60 volgens NEN-EN 13501-2",
             "confidence": 0.95},
            {"key": "demountable", "presence": "not_stated"},
            {"key": "not_a_real_key", "value_text": "dropped", "presence": "stated",
             "page_number": 1, "source_snippet": "irrelevant"},
        ],
        "certifications": [
            {"scheme": "CE", "number": "CE-123", "page_number": 1, "source_snippet": "CE-markering CE-123"},
        ],
    }]

    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id,
        extraction_run_id=run_id, extracted_products=extracted,
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: page_text,
    )

    assert stats.products_created_or_updated == 1
    assert stats.claims_inserted == 2
    assert stats.claims_rejected == 0
    assert stats.certifications_inserted == 1

    with conn.cursor() as cur:
        cur.execute("select * from products where manufacturer_ref = 'RW-211'")
        product = cur.fetchone()
    assert product["name"] == "Acme 211"

    with conn.cursor() as cur:
        cur.execute(
            "select c.*, sa.key from claims c join spec_attributes sa on sa.id = c.spec_attribute_id "
            "where c.product_id = %s and c.is_current",
            (product["id"],),
        )
        claims = {row["key"]: row for row in cur.fetchall()}

    assert claims["fire_resistance_ei"]["value_numeric"] == 60
    assert claims["fire_resistance_ei"]["document_version_id"] == version_id
    assert claims["demountable"]["presence"] == "not_stated"
    assert "not_a_real_key" not in claims


def test_persist_rejects_uncited_stated_claim_but_keeps_product(conn):
    org_id, brand_id = _make_org_brand_product(conn, org_slug="beta")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-beta"))
        placeholder_product_id = cur.fetchone()["id"]
    document_id, version_id = _make_document(conn, org_id, placeholder_product_id, "b" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id",
            (version_id,),
        )
        run_id = cur.fetchone()["id"]

    extracted = [{
        "name": "Beta X",
        "claims": [{"key": "fire_resistance_ei", "value_numeric": 60, "unit": "min", "presence": "stated",
                    "page_number": 1, "source_snippet": "This text is not on the page."}],
    }]

    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id,
        extraction_run_id=run_id, extracted_products=extracted,
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "Something else entirely.",
    )

    assert stats.products_created_or_updated == 1
    assert stats.claims_inserted == 0
    assert stats.claims_rejected == 1
    assert "not found verbatim" in stats.rejections[0]


def test_persist_supersedes_changed_claim_value(conn):
    org_id, brand_id = _make_org_brand_product(conn, org_slug="gamma")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-gamma"))
        placeholder_product_id = cur.fetchone()["id"]

    def make_extracted(value, snippet):
        return [{
            "name": "Gamma Wall", "manufacturer_ref": "GW-1",
            "claims": [{"key": "fire_resistance_ei", "value_numeric": value, "unit": "min",
                        "presence": "stated", "page_number": 1, "source_snippet": snippet}],
        }]

    _, version_id_1 = _make_document(conn, org_id, placeholder_product_id, "c" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_1,))
        run_1 = cur.fetchone()["id"]

    persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_1,
        extraction_run_id=run_1, extracted_products=make_extracted(60, "EI 60 rating"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "EI 60 rating",
    )

    with conn.cursor() as cur:
        cur.execute("select id from products where manufacturer_ref = 'GW-1'")
        product_id = cur.fetchone()["id"]

    _, version_id_2 = _make_document(conn, org_id, placeholder_product_id, "d" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_2,))
        run_2 = cur.fetchone()["id"]

    persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_2,
        extraction_run_id=run_2, extracted_products=make_extracted(90, "EI 90 rating"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "EI 90 rating",
    )

    with conn.cursor() as cur:
        cur.execute("select * from claims where product_id = %s order by created_at", (product_id,))
        rows = cur.fetchall()

    assert len(rows) == 2
    old, new = rows
    assert old["is_current"] is False
    assert old["superseded_by"] == new["id"]
    assert new["is_current"] is True
    assert new["value_numeric"] == 90
