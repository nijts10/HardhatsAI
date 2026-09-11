"""Runs persist_extracted_products against the real schema so the
append-only claims/supersede bookkeeping is checked against the actual
constraints, not a mock."""
from ingest import db
from ingest.persistence import PersistStats, persist_extracted_products, persist_unmapped_findings, slugify


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
            "insert into document_versions (document_id, storage_path, sha256, uploaded_by, source_url, retrieved_at) "
            "values (%s, %s, %s, null, %s, %s) returning id",
            (document_id, f"acme/{sha256[:2]}/doc.pdf", sha256,
             "https://example.com/test-datasheet.pdf", "2026-01-01T00:00:00+00:00"),
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


def test_persist_derived_claim_requires_citation_and_keeps_derivation_note(conn):
    org_id, brand_id = _make_org_brand_product(conn, org_slug="eta")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-eta"))
        placeholder_product_id = cur.fetchone()["id"]
    document_id, version_id = _make_document(conn, org_id, placeholder_product_id, "4" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id",
            (version_id,),
        )
        run_id = cur.fetchone()["id"]

    page_text = "Combustibility of Materials at 750 C - Noncombustible"
    extracted = [{
        "name": "Eta Product",
        "claims": [
            # valid: derived, but cited with a real verbatim snippet + a
            # separate derivation_note for the reasoning.
            {"key": "fire_resistance_ei", "value_numeric": 60, "unit": "min", "presence": "derived",
             "page_number": 1, "source_snippet": page_text,
             "derivation_note": "US noncombustibility implies this EI rating."},
            # invalid: derived with no citation at all -- must be rejected,
            # not silently accepted the way it was before fix 7.
            {"key": "max_height_mm", "value_numeric": 4000, "presence": "derived"},
        ],
    }]

    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id,
        extraction_run_id=run_id, extracted_products=extracted,
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: page_text,
    )

    assert stats.claims_inserted == 1
    assert stats.claims_rejected == 1
    assert "derived claim missing" in stats.rejections[0]

    with conn.cursor() as cur:
        cur.execute(
            "select c.derivation_note from claims c join spec_attributes sa on sa.id = c.spec_attribute_id "
            "where sa.key = 'fire_resistance_ei' and c.is_current"
        )
        row = cur.fetchone()
    assert row["derivation_note"] == "US noncombustibility implies this EI rating."


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


def test_persist_upsert_product_dedupes_by_slug_despite_manufacturer_ref_casing_drift(conn):
    # Observed against a real document: re-extracting the same PDF returned
    # manufacturer_ref with different capitalization between calls
    # ("Rockwool" vs "ROCKWOOL"). manufacturer_ref is no longer part of the
    # identity slug at all (see the next test for why -- presence/absence
    # drift, not just casing, fragments products), so this must still
    # dedupe to one product on name alone, and the later, real
    # manufacturer_ref value must still end up on the row.
    org_id, brand_id = _make_org_brand_product(conn, org_slug="delta")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-delta"))
        placeholder_product_id = cur.fetchone()["id"]

    def make_extracted(manufacturer_ref):
        return [{
            "name": "ROXUL Safe", "manufacturer_ref": manufacturer_ref,
            "claims": [{"key": "demountable", "presence": "not_stated"}],
        }]

    _, version_id_1 = _make_document(conn, org_id, placeholder_product_id, "e" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_1,))
        run_1 = cur.fetchone()["id"]

    persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_1,
        extraction_run_id=run_1, extracted_products=make_extracted("Rockwool"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    _, version_id_2 = _make_document(conn, org_id, placeholder_product_id, "f" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_2,))
        run_2 = cur.fetchone()["id"]

    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_2,
        extraction_run_id=run_2, extracted_products=make_extracted("ROCKWOOL"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    assert stats.products_created_or_updated == 1
    with conn.cursor() as cur:
        cur.execute("select id, manufacturer_ref from products where slug = 'roxul-safe'")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["manufacturer_ref"] == "Rockwool"


def test_persist_upsert_product_dedupes_by_name_despite_manufacturer_ref_presence_drift(conn):
    # Found for real 2026-08-14 auditing the HeartFelt line: two different
    # source documents both say "HeartFelt® Origami" (same name, verified
    # against the PDFs), but only one extraction call happened to emit a
    # manufacturer_ref -- the other left it None. Under the old (name,
    # manufacturer_ref) slug key this silently created TWO product rows,
    # each holding only part of the real ground truth (claims split
    # 38/5), with no error anywhere. Must dedupe to one product regardless
    # of which call states a manufacturer_ref, and a manufacturer_ref
    # supplied later must still get backfilled onto the existing row.
    org_id, brand_id = _make_org_brand_product(conn, org_slug="epsilon")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-epsilon"))
        placeholder_product_id = cur.fetchone()["id"]

    def make_extracted(manufacturer_ref):
        return [{
            "name": "HeartFelt Origami", "manufacturer_ref": manufacturer_ref,
            "claims": [{"key": "demountable", "presence": "not_stated"}],
        }]

    _, version_id_1 = _make_document(conn, org_id, placeholder_product_id, "1" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_1,))
        run_1 = cur.fetchone()["id"]

    persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_1,
        extraction_run_id=run_1, extracted_products=make_extracted(None),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    _, version_id_2 = _make_document(conn, org_id, placeholder_product_id, "2" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_2,))
        run_2 = cur.fetchone()["id"]

    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_2,
        extraction_run_id=run_2, extracted_products=make_extracted("Hunter Douglas Europe B.V."),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    assert stats.products_created_or_updated == 1
    with conn.cursor() as cur:
        cur.execute("select id, manufacturer_ref from products where slug = 'heartfelt-origami'")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["manufacturer_ref"] == "Hunter Douglas Europe B.V."


def test_persist_creates_product_line_and_links_product(conn):
    org_id, brand_id = _make_org_brand_product(conn, org_slug="zeta")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-zeta"))
        placeholder_product_id = cur.fetchone()["id"]
    _, version_id = _make_document(conn, org_id, placeholder_product_id, "3" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id,))
        run_id = cur.fetchone()["id"]

    persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id,
        extraction_run_id=run_id,
        extracted_products=[{
            "name": "HeartFelt Ceiling System", "product_line": "HeartFelt",
            "claims": [{"key": "demountable", "presence": "not_stated"}],
        }],
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    with conn.cursor() as cur:
        cur.execute("select id, product_line_id from products where slug = 'heartfelt-ceiling-system'")
        product = cur.fetchone()
        assert product["product_line_id"] is not None
        cur.execute("select brand_id, name, slug from product_lines where id = %s", (product["product_line_id"],))
        line = cur.fetchone()
    assert line["brand_id"] == brand_id
    assert line["name"] == "HeartFelt"
    assert line["slug"] == "heartfelt"


def test_persist_backfills_product_line_id_on_existing_product(conn):
    # Same backfill precedent as manufacturer_ref: a product created before
    # its line was ever identified (or by an extraction call that didn't
    # supply one) must pick up product_line_id the first time a later call
    # does supply one, without needing a new row.
    org_id, brand_id = _make_org_brand_product(conn, org_slug="eta")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-eta"))
        placeholder_product_id = cur.fetchone()["id"]

    def make_extracted(product_line):
        return [{
            "name": "Origami", "product_line": product_line,
            "claims": [{"key": "demountable", "presence": "not_stated"}],
        }]

    _, version_id_1 = _make_document(conn, org_id, placeholder_product_id, "4" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_1,))
        run_1 = cur.fetchone()["id"]
    persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_1,
        extraction_run_id=run_1, extracted_products=make_extracted(None),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )
    with conn.cursor() as cur:
        cur.execute("select product_line_id from products where slug = 'origami'")
        assert cur.fetchone()["product_line_id"] is None

    _, version_id_2 = _make_document(conn, org_id, placeholder_product_id, "5" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_2,))
        run_2 = cur.fetchone()["id"]
    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_2,
        extraction_run_id=run_2, extracted_products=make_extracted("HeartFelt"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    assert stats.products_created_or_updated == 1
    with conn.cursor() as cur:
        cur.execute("select id, name, slug from products where slug = 'origami'")
        rows = cur.fetchall()
        cur.execute("""select p.product_line_id, pl.name as line_name from products p
                       join product_lines pl on pl.id = p.product_line_id where p.slug = 'origami'""")
        linked = cur.fetchone()
    assert len(rows) == 1  # still one product row, not a second one
    assert linked["line_name"] == "HeartFelt"


def test_persist_warns_on_similar_name_within_same_product_line(conn):
    # This is the exact failure class that has fragmented one real product
    # into two rows three times in production (2026-08-14, 08-19, 09-07):
    # a genuinely different name string for the same real product, inside
    # the same line. Must NOT block creation or auto-merge -- only surface
    # the candidate so a human/reviewer can catch it, same principle as
    # spec_attributes staying human-curated.
    org_id, brand_id = _make_org_brand_product(conn, org_slug="theta")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-theta"))
        placeholder_product_id = cur.fetchone()["id"]

    def make_extracted(name):
        return [{
            "name": name, "product_line": "HeartFelt",
            "claims": [{"key": "demountable", "presence": "not_stated"}],
        }]

    _, version_id_1 = _make_document(conn, org_id, placeholder_product_id, "6" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_1,))
        run_1 = cur.fetchone()["id"]
    persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_1,
        extraction_run_id=run_1, extracted_products=make_extracted("HeartFelt Wall System"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    _, version_id_2 = _make_document(conn, org_id, placeholder_product_id, "7" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_2,))
        run_2 = cur.fetchone()["id"]
    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_2,
        extraction_run_id=run_2, extracted_products=make_extracted("HeartFelt Wall"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    # Both rows exist -- this is a warning, not an auto-merge.
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from products where slug in ('heartfelt-wall-system', 'heartfelt-wall')")
        assert cur.fetchone()["n"] == 2

    assert len(stats.possible_duplicate_products) == 1
    warning = stats.possible_duplicate_products[0]
    assert warning["new_name"] == "HeartFelt Wall"
    assert warning["existing_name"] == "HeartFelt Wall System"


def test_persist_no_warning_across_different_product_lines(conn):
    # A similar name is only suspicious WITHIN the same line -- two
    # unrelated products in different lines that happen to share a word
    # ("Linear") must not trigger a false-positive warning.
    org_id, brand_id = _make_org_brand_product(conn, org_slug="iota")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-iota"))
        placeholder_product_id = cur.fetchone()["id"]

    def make_extracted(name, product_line):
        return [{
            "name": name, "product_line": product_line,
            "claims": [{"key": "demountable", "presence": "not_stated"}],
        }]

    _, version_id_1 = _make_document(conn, org_id, placeholder_product_id, "8" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_1,))
        run_1 = cur.fetchone()["id"]
    persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_1,
        extraction_run_id=run_1, extracted_products=make_extracted("HeartFelt Linear", "HeartFelt"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    _, version_id_2 = _make_document(conn, org_id, placeholder_product_id, "9" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_2,))
        run_2 = cur.fetchone()["id"]
    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_2,
        extraction_run_id=run_2, extracted_products=make_extracted("Luxalon Linear Ceiling 84B", "Luxalon"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    assert stats.possible_duplicate_products == []


def test_persist_warns_on_similar_name_with_no_product_line(conn):
    # Regression test: find_similar_products_in_line used to be skipped
    # entirely whenever product_line_id was None (persistence.py's old
    # `if product_line_id is not None:` guard) -- a real blind spot found
    # 2026-09-11 reviewing the Hunter Douglas catalog, where the one
    # confirmed genuine duplicate pair ("Metalen Baffle Plafond" / "Metalen
    # Baffle Plafonds") had NO product_line on either side and so was never
    # checked against anything. Neither extracted_product below names a
    # product_line -- the warning must still fire.
    org_id, brand_id = _make_org_brand_product(conn, org_slug="kappa")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-kappa"))
        placeholder_product_id = cur.fetchone()["id"]

    def make_extracted(name):
        return [{
            "name": name,
            "claims": [{"key": "demountable", "presence": "not_stated"}],
        }]

    _, version_id_1 = _make_document(conn, org_id, placeholder_product_id, "a" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_1,))
        run_1 = cur.fetchone()["id"]
    persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_1,
        extraction_run_id=run_1, extracted_products=make_extracted("Metalen Baffle Plafond"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    _, version_id_2 = _make_document(conn, org_id, placeholder_product_id, "b" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_2,))
        run_2 = cur.fetchone()["id"]
    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_2,
        extraction_run_id=run_2, extracted_products=make_extracted("Metalen Baffle Plafonds"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "n/a",
    )

    assert len(stats.possible_duplicate_products) == 1
    warning = stats.possible_duplicate_products[0]
    assert warning["new_name"] == "Metalen Baffle Plafonds"
    assert warning["existing_name"] == "Metalen Baffle Plafond"


def test_persist_variants_get_distinct_current_claims_per_variant(conn):
    # The whole point of fix 6: two variants of one product can carry
    # DIFFERENT values for the same spec (e.g. sound reduction by
    # thickness) without the unique-current-claim constraint colliding.
    org_id, brand_id = _make_org_brand_product(conn, org_slug="epsilon")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-epsilon"))
        placeholder_product_id = cur.fetchone()["id"]
    document_id, version_id = _make_document(conn, org_id, placeholder_product_id, "1" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id",
            (version_id,),
        )
        run_id = cur.fetchone()["id"]

    page_text = "40mm: Rw 45 dB. 80mm: Rw 52 dB."
    extracted = [{
        "name": "Insulatie X",
        "claims": [{"key": "thickness_mm", "presence": "not_stated"}],
        "variants": [
            {"label": "40mm", "claims": [
                {"key": "airborne_sound_reduction_rw", "value_numeric": 45, "unit": "dB",
                 "presence": "stated", "page_number": 1, "source_snippet": "40mm: Rw 45 dB."},
            ]},
            {"label": "80mm", "claims": [
                {"key": "airborne_sound_reduction_rw", "value_numeric": 52, "unit": "dB",
                 "presence": "stated", "page_number": 1, "source_snippet": "80mm: Rw 52 dB."},
            ]},
        ],
    }]

    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id,
        extraction_run_id=run_id, extracted_products=extracted,
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: page_text,
    )

    assert stats.products_created_or_updated == 1
    assert stats.variants_created_or_updated == 2
    assert stats.claims_inserted == 3  # product-level thickness_mm + 2 variant-level Rw claims
    assert stats.claims_rejected == 0

    with conn.cursor() as cur:
        cur.execute("select id from products where slug = 'insulatie-x'")
        product_id = cur.fetchone()["id"]
        cur.execute(
            "select pv.label, c.value_numeric from product_variants pv "
            "join claims c on c.variant_id = pv.id and c.is_current "
            "where pv.product_id = %s order by pv.label",
            (product_id,),
        )
        rows = cur.fetchall()

    assert {(r["label"], float(r["value_numeric"])) for r in rows} == {("40mm", 45.0), ("80mm", 52.0)}


def test_persist_variant_dedupes_by_slug_and_supersedes_changed_value(conn):
    org_id, brand_id = _make_org_brand_product(conn, org_slug="zeta")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-zeta"))
        placeholder_product_id = cur.fetchone()["id"]

    def make_extracted(value, snippet):
        return [{
            "name": "Insulatie Z", "claims": [],
            "variants": [{"label": "40mm", "claims": [
                {"key": "airborne_sound_reduction_rw", "value_numeric": value, "unit": "dB",
                 "presence": "stated", "page_number": 1, "source_snippet": snippet},
            ]}],
        }]

    _, version_id_1 = _make_document(conn, org_id, placeholder_product_id, "2" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_1,))
        run_1 = cur.fetchone()["id"]

    persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_1,
        extraction_run_id=run_1, extracted_products=make_extracted(45, "Rw 45"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "Rw 45",
    )

    _, version_id_2 = _make_document(conn, org_id, placeholder_product_id, "3" * 64)
    with conn.cursor() as cur:
        cur.execute(
            "insert into extraction_runs (document_version_id, parser_version, prompt_version, model) "
            "values (%s, '1', '1', 'test-model') returning id", (version_id_2,))
        run_2 = cur.fetchone()["id"]

    stats = persist_extracted_products(
        conn, brand_id=brand_id, category_id=None, document_version_id=version_id_2,
        extraction_run_id=run_2, extracted_products=make_extracted(46, "Rw 46"),
        spec_attrs_by_key=_spec_attrs_by_key(conn), document_chunks=[],
        get_page_text=lambda p: "Rw 46",
    )

    assert stats.variants_created_or_updated == 1  # same variant, not a duplicate
    assert stats.claims_superseded == 1

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from product_variants where slug = '40mm'")
        assert cur.fetchone()["n"] == 1


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


def test_persist_unmapped_findings_queues_verified_ones_and_drops_uncited(conn):
    org_id, brand_id = _make_org_brand_product(conn, org_slug="theta")
    with conn.cursor() as cur:
        cur.execute("insert into products (brand_id, name, slug) values (%s, %s, %s) returning id",
                    (brand_id, "placeholder", "placeholder-theta"))
        placeholder_product_id = cur.fetchone()["id"]
    _, version_id = _make_document(conn, org_id, placeholder_product_id, "5" * 64)

    page_text = "Water resistant: yes. Corrosiveness to Steel - Passed."
    findings = [
        {"found_term": "water_resistant", "page_number": 1, "source_snippet": "Water resistant: yes."},
        # not actually on the page -- must be dropped, not queued as-is
        {"found_term": "fabricated", "page_number": 1, "source_snippet": "This sentence is not on the page."},
    ]

    stats = PersistStats()
    persist_unmapped_findings(
        conn, document_version_id=version_id, findings=findings,
        get_page_text=lambda p: page_text, stats=stats,
    )

    assert stats.unmapped_findings_queued == 1
    assert stats.unmapped_findings_rejected == 1
    assert "fabricated" in stats.rejections[0]

    with conn.cursor() as cur:
        cur.execute("select found_term, source_snippet, status, page_number from review_queue "
                     "where document_version_id = %s", (version_id,))
        rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["found_term"] == "water_resistant"
    assert rows[0]["status"] == "new"
    assert rows[0]["page_number"] == 1


def test_persist_unmapped_findings_never_creates_a_claim():
    # The structural guarantee STAP 4 asks for: nothing in this path ever
    # touches the claims table, so review_queue rows can never be counted
    # by report.py's coverage query. Verified by inspecting the function's
    # only DB call rather than needing a live connection for this one.
    import inspect

    source = inspect.getsource(persist_unmapped_findings)
    assert "insert_claim" not in source
    assert "insert_review_queue_entry" in source
