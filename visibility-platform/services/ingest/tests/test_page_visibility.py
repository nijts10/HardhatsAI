import hashlib
import uuid

from ingest.http_fetch import FetchResult
from ingest.page_visibility import run_page_visibility_check


def _make_org_brand(conn, slug="acme"):
    with conn.cursor() as cur:
        cur.execute("insert into organizations (name, slug) values (%s, %s) returning id", (slug.title(), slug))
        org_id = cur.fetchone()["id"]
        cur.execute("insert into brands (organization_id, name, slug) values (%s, %s, %s) returning id",
                    (org_id, "Acme Systeemplafonds", slug))
        brand_id = cur.fetchone()["id"]
    return org_id, brand_id


def _systeemplafond_category(conn):
    with conn.cursor() as cur:
        cur.execute("select id from product_categories where code = 'systeemplafond'")
        return cur.fetchone()["id"]


def _make_product(conn, brand_id, category_id, name, product_url=None):
    slug = name.lower().replace(" ", "-") + "-" + uuid.uuid4().hex[:8]
    with conn.cursor() as cur:
        cur.execute(
            "insert into products (brand_id, category_id, name, slug, product_url) values (%s, %s, %s, %s, %s) "
            "returning id",
            (brand_id, category_id, name, slug, product_url),
        )
        return cur.fetchone()["id"]


def _make_document_version(conn, org_id, product_id):
    sha256 = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "insert into documents (organization_id, product_id, title, kind) "
            "values (%s, %s, 'Datasheet', 'datasheet') returning id",
            (org_id, product_id),
        )
        document_id = cur.fetchone()["id"]
        cur.execute(
            "insert into document_versions (document_id, storage_path, sha256, source_url, retrieved_at) "
            "values (%s, %s, %s, 'https://example.com/x.pdf', '2026-01-01') returning id",
            (document_id, f"acme/{sha256[:2]}/doc.pdf", sha256),
        )
        return cur.fetchone()["id"]


def _make_spec_attribute(conn, *, key, data_type="numeric", unit=None):
    with conn.cursor() as cur:
        cur.execute(
            "insert into spec_attributes (key, name_nl, name_en, data_type, unit) values (%s, %s, %s, %s, %s) "
            "returning id",
            (key, key, key, data_type, unit),
        )
        return cur.fetchone()["id"]


def _make_claim(conn, *, product_id, spec_attribute_id, document_version_id, **values):
    row = {"value_numeric": None, "value_numeric_max": None, "value_text": None, "value_bool": None,
           "value_enum": None, "unit": None, "presence": "stated"}
    row.update(values)
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into claims
              (product_id, spec_attribute_id, value_numeric, value_numeric_max, value_text, value_bool,
               value_enum, unit, presence, document_version_id, page_number, source_snippet)
            values (%(product_id)s, %(spec_attribute_id)s, %(value_numeric)s, %(value_numeric_max)s, %(value_text)s,
                    %(value_bool)s, %(value_enum)s, %(unit)s, %(presence)s, %(document_version_id)s, 1, 'ground truth')
            returning id
            """,
            {"product_id": product_id, "spec_attribute_id": spec_attribute_id,
             "document_version_id": document_version_id, **row},
        )
        return cur.fetchone()["id"]


def _canned_fetcher(url_to_result: dict):
    return lambda url: url_to_result[url]


def test_numeric_value_found_via_dutch_comma_decimal_variant(conn):
    org_id, brand_id = _make_org_brand(conn, slug="pv-comma")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme Comma Panel", product_url="https://acme.example/comma")
    doc_version_id = _make_document_version(conn, org_id, product_id)
    spec_id = _make_spec_attribute(conn, key="alpha_pv_comma")
    _make_claim(conn, product_id=product_id, spec_attribute_id=spec_id, document_version_id=doc_version_id,
                value_numeric=0.85)

    html = "<html><body><p>Geluidsabsorptie: 0,85</p></body></html>"
    stats = run_page_visibility_check(
        conn, brand_id=brand_id,
        fetcher=_canned_fetcher({"https://acme.example/comma": FetchResult(200, html, None)}),
    )

    assert stats.claims_found == 1
    assert stats.claims_not_found == 0


def test_numeric_value_not_found_on_page(conn):
    org_id, brand_id = _make_org_brand(conn, slug="pv-missing")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme Missing Panel",
                                product_url="https://acme.example/missing")
    doc_version_id = _make_document_version(conn, org_id, product_id)
    spec_id = _make_spec_attribute(conn, key="alpha_pv_missing")
    _make_claim(conn, product_id=product_id, spec_attribute_id=spec_id, document_version_id=doc_version_id,
                value_numeric=0.85)

    html = "<html><body><p>This page never mentions the value at all.</p></body></html>"
    stats = run_page_visibility_check(
        conn, brand_id=brand_id,
        fetcher=_canned_fetcher({"https://acme.example/missing": FetchResult(200, html, None)}),
    )

    assert stats.claims_found == 0
    assert stats.claims_not_found == 1


def test_text_value_found_and_boolean_claim_is_skipped_as_undetermined(conn):
    org_id, brand_id = _make_org_brand(conn, slug="pv-mixed")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme Mixed Panel", product_url="https://acme.example/mixed")
    doc_version_id = _make_document_version(conn, org_id, product_id)
    text_spec = _make_spec_attribute(conn, key="edge_pv_mixed", data_type="text")
    bool_spec = _make_spec_attribute(conn, key="demountable_pv_mixed", data_type="boolean")
    _make_claim(conn, product_id=product_id, spec_attribute_id=text_spec, document_version_id=doc_version_id,
                value_text="Kant type A")
    _make_claim(conn, product_id=product_id, spec_attribute_id=bool_spec, document_version_id=doc_version_id,
                value_bool=True)

    html = "<html><body><p>Randafwerking: Kant type A</p></body></html>"
    stats = run_page_visibility_check(
        conn, brand_id=brand_id,
        fetcher=_canned_fetcher({"https://acme.example/mixed": FetchResult(200, html, None)}),
    )

    assert stats.claims_checked == 2
    assert stats.claims_found == 1        # the text claim
    assert stats.claims_undetermined == 1  # the boolean claim, never checked


def test_schema_org_product_jsonld_is_detected(conn):
    org_id, brand_id = _make_org_brand(conn, slug="pv-jsonld")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme LD Panel", product_url="https://acme.example/ld")
    doc_version_id = _make_document_version(conn, org_id, product_id)
    spec_id = _make_spec_attribute(conn, key="alpha_pv_ld")
    _make_claim(conn, product_id=product_id, spec_attribute_id=spec_id, document_version_id=doc_version_id,
                value_numeric=0.85)

    html = """
    <html><body>
    <script type="application/ld+json">{"@context": "https://schema.org", "@type": "Product", "name": "Acme LD Panel"}</script>
    <p>0.85</p>
    </body></html>
    """
    stats = run_page_visibility_check(
        conn, brand_id=brand_id,
        fetcher=_canned_fetcher({"https://acme.example/ld": FetchResult(200, html, None)}),
    )
    assert stats.claims_found == 1

    with conn.cursor() as cur:
        cur.execute("select schema_org_product_present from page_spec_visibility where product_id = %s", (product_id,))
        assert cur.fetchone()["schema_org_product_present"] is True


def test_schema_org_absent_when_no_markup(conn):
    org_id, brand_id = _make_org_brand(conn, slug="pv-noschema")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme NoSchema Panel",
                                product_url="https://acme.example/noschema")
    doc_version_id = _make_document_version(conn, org_id, product_id)
    spec_id = _make_spec_attribute(conn, key="alpha_pv_noschema")
    _make_claim(conn, product_id=product_id, spec_attribute_id=spec_id, document_version_id=doc_version_id,
                value_numeric=0.85)

    html = "<html><body><p>0.85 but no structured markup at all.</p></body></html>"
    run_page_visibility_check(
        conn, brand_id=brand_id,
        fetcher=_canned_fetcher({"https://acme.example/noschema": FetchResult(200, html, None)}),
    )

    with conn.cursor() as cur:
        cur.execute("select schema_org_product_present from page_spec_visibility where product_id = %s", (product_id,))
        assert cur.fetchone()["schema_org_product_present"] is False


def test_product_without_url_is_skipped(conn):
    org_id, brand_id = _make_org_brand(conn, slug="pv-nourl")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme NoUrl Panel", product_url=None)
    doc_version_id = _make_document_version(conn, org_id, product_id)
    spec_id = _make_spec_attribute(conn, key="alpha_pv_nourl")
    _make_claim(conn, product_id=product_id, spec_attribute_id=spec_id, document_version_id=doc_version_id,
                value_numeric=0.85)

    stats = run_page_visibility_check(conn, brand_id=brand_id, fetcher=_canned_fetcher({}))
    assert stats.products_skipped_no_url == 1
    assert stats.products_checked == 0


def test_product_without_ground_truth_is_skipped(conn):
    org_id, brand_id = _make_org_brand(conn, slug="pv-noground")
    category_id = _systeemplafond_category(conn)
    _make_product(conn, brand_id, category_id, "Acme NoGround Panel", product_url="https://acme.example/noground")

    stats = run_page_visibility_check(conn, brand_id=brand_id, fetcher=_canned_fetcher({}))
    assert stats.products_skipped_no_ground_truth == 1
    assert stats.products_checked == 0


def test_fetch_error_records_undetermined_for_every_claim(conn):
    org_id, brand_id = _make_org_brand(conn, slug="pv-fetcherr")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme Err Panel", product_url="https://acme.example/err")
    doc_version_id = _make_document_version(conn, org_id, product_id)
    spec_id = _make_spec_attribute(conn, key="alpha_pv_err")
    _make_claim(conn, product_id=product_id, spec_attribute_id=spec_id, document_version_id=doc_version_id,
                value_numeric=0.85)

    stats = run_page_visibility_check(
        conn, brand_id=brand_id,
        fetcher=_canned_fetcher({"https://acme.example/err": FetchResult(None, None, "connection refused")}),
    )
    assert stats.products_checked == 1
    assert stats.claims_undetermined == 1
    assert len(stats.fetch_errors) == 1

    with conn.cursor() as cur:
        cur.execute("select value_found_as_text, schema_org_product_present, error from page_spec_visibility "
                    "where product_id = %s", (product_id,))
        row = cur.fetchone()
    assert row["value_found_as_text"] is None
    assert row["schema_org_product_present"] is None
    assert row["error"] == "connection refused"


def test_not_stated_claim_has_nothing_to_check_and_is_excluded(conn):
    org_id, brand_id = _make_org_brand(conn, slug="pv-notstated")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme NotStated Panel",
                                product_url="https://acme.example/notstated")
    doc_version_id = _make_document_version(conn, org_id, product_id)
    spec_id = _make_spec_attribute(conn, key="alpha_pv_notstated")
    _make_claim(conn, product_id=product_id, spec_attribute_id=spec_id, document_version_id=doc_version_id,
                presence="not_stated")

    stats = run_page_visibility_check(conn, brand_id=brand_id, fetcher=_canned_fetcher({}))
    # The ONLY claim for this product is not_stated -- nothing to check against,
    # so the product itself is skipped as having no (real) ground truth.
    assert stats.products_skipped_no_ground_truth == 1
