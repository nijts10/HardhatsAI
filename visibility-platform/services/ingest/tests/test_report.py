from ingest.report import compute_coverage, compute_visibility, create_report, render_sections


def _make_org_brand(conn, slug="acme"):
    with conn.cursor() as cur:
        cur.execute("insert into organizations (name, slug) values (%s, %s) returning id", (slug.title(), slug))
        org_id = cur.fetchone()["id"]
        cur.execute("insert into brands (organization_id, name, slug) values (%s, %s, %s) returning id",
                    (org_id, slug.title(), slug))
        brand_id = cur.fetchone()["id"]
    return org_id, brand_id


def _make_product(conn, brand_id, name, slug, category_code=None):
    category_id = None
    if category_code:
        with conn.cursor() as cur:
            cur.execute("select id from product_categories where code = %s", (category_code,))
            category_id = cur.fetchone()["id"]
    with conn.cursor() as cur:
        cur.execute(
            "insert into products (brand_id, category_id, name, slug) values (%s, %s, %s, %s) returning id",
            (brand_id, category_id, name, slug),
        )
        return cur.fetchone()["id"]


def _stated_claim(conn, product_id, spec_key, value_numeric=60):
    with conn.cursor() as cur:
        cur.execute("select id from spec_attributes where key = %s", (spec_key,))
        spec_attribute_id = cur.fetchone()["id"]
        cur.execute(
            """
            insert into claims (product_id, spec_attribute_id, value_numeric, unit, presence,
                                 document_version_id, page_number, source_snippet)
            select %s, %s, %s, 'min', 'stated', dv.id, 1, 'snippet'
              from document_versions dv limit 1
            """,
            (product_id, spec_attribute_id, value_numeric),
        )


def _make_document_version(conn, org_id, product_id):
    with conn.cursor() as cur:
        cur.execute(
            "insert into documents (organization_id, product_id, title, kind) values (%s, %s, 'd', 'datasheet') returning id",
            (org_id, product_id),
        )
        document_id = cur.fetchone()["id"]
        cur.execute(
            "insert into document_versions (document_id, storage_path, sha256, source_url, retrieved_at) "
            "values (%s, 'x', %s, %s, %s) returning id",
            (document_id, "e" * 64, "https://example.com/test-datasheet.pdf", "2026-01-01T00:00:00+00:00"),
        )
        return cur.fetchone()["id"]


def test_compute_coverage_no_products(conn):
    org_id, brand_id = _make_org_brand(conn)
    coverage = compute_coverage(conn, brand_id)
    assert coverage.brand_name == "Acme"
    assert coverage.products == []
    assert coverage.completeness == 0.0


def test_compute_coverage_reflects_stated_vs_missing(conn):
    org_id, brand_id = _make_org_brand(conn)
    product_id = _make_product(conn, brand_id, "Acme 211", "acme-211", "brandwerende_systeemwanden")
    _make_document_version(conn, org_id, product_id)
    _stated_claim(conn, product_id, "fire_resistance_ei")

    coverage = compute_coverage(conn, brand_id)
    assert len(coverage.products) == 1
    pc = coverage.products[0]
    assert "Brandwerendheid EI" in pc.stated
    assert pc.total > 1  # more than one spec is relevant to this category
    assert 0 < pc.completeness < 1  # some stated, some missing -- not 0% or 100%


def test_render_sections_produces_readable_output(conn):
    org_id, brand_id = _make_org_brand(conn)
    product_id = _make_product(conn, brand_id, "Acme 211", "acme-211", "brandwerende_systeemwanden")
    _make_document_version(conn, org_id, product_id)
    _stated_claim(conn, product_id, "fire_resistance_ei")

    coverage = compute_coverage(conn, brand_id)
    sections = render_sections(coverage)
    headings = [h for h, _ in sections]
    assert headings == ["Overview", "Product coverage", "Missing data", "Certifications found", "AI visibility"]

    product_section = dict(sections)["Product coverage"]
    assert "Acme 211" in product_section
    missing_section = dict(sections)["Missing data"]
    assert "Acme 211" in missing_section  # still has some missing specs
    # No benchmark has run -- must say so explicitly, not silently show 0%.
    assert "No AI-visibility benchmark has been run yet" in dict(sections)["AI visibility"]


def test_create_report_persists_reports_and_sections(conn):
    org_id, brand_id = _make_org_brand(conn)
    product_id = _make_product(conn, brand_id, "Acme 211", "acme-211", "brandwerende_systeemwanden")
    _make_document_version(conn, org_id, product_id)
    _stated_claim(conn, product_id, "fire_resistance_ei")

    report_id = create_report(conn, brand_id=brand_id, organization_id=org_id, kind="audit")

    with conn.cursor() as cur:
        cur.execute("select * from reports where id = %s", (report_id,))
        report_row = cur.fetchone()
        cur.execute("select heading, section_order from report_sections where report_id = %s order by section_order", (report_id,))
        sections = cur.fetchall()

    assert report_row["status"] == "draft"
    assert report_row["kind"] == "audit"
    assert report_row["brand_id"] == brand_id
    assert [s["heading"] for s in sections] == [
        "Overview", "Product coverage", "Missing data", "Certifications found", "AI visibility",
    ]
    assert [s["section_order"] for s in sections] == [0, 1, 2, 3, 4]


def test_compute_visibility_empty_when_no_runs(conn):
    org_id, brand_id = _make_org_brand(conn)
    assert compute_visibility(conn, brand_id) == []


def test_compute_visibility_uses_latest_succeeded_run_per_model(conn):
    org_id, brand_id = _make_org_brand(conn)
    with conn.cursor() as cur:
        cur.execute("select id, prompt_text from prompt_library order by use_case limit 2")
        prompts = cur.fetchall()

        # An older, failed chatgpt run -- must be ignored.
        cur.execute(
            "insert into benchmark_runs (organization_id, brand_id, model, status, started_at) "
            "values (%s, %s, 'chatgpt', 'failed', now() - interval '2 days') returning id",
            (org_id, brand_id),
        )
        # A newer, succeeded chatgpt run -- this is the one that should count.
        cur.execute(
            "insert into benchmark_runs (organization_id, brand_id, model, status, started_at, stats) "
            "values (%s, %s, 'chatgpt', 'succeeded', now(), %s) returning id",
            (org_id, brand_id, '{"prompts_run": 2, "mentions": 1}'),
        )
        run_id = cur.fetchone()["id"]
        cur.execute(
            "insert into benchmark_results (benchmark_run_id, prompt_id, response_text, brand_mentioned) "
            "values (%s, %s, 'mentions Acme', true), (%s, %s, 'does not mention it', false)",
            (run_id, prompts[0]["id"], run_id, prompts[1]["id"]),
        )

    results = compute_visibility(conn, brand_id)
    assert len(results) == 1
    assert results[0].model == "chatgpt"
    assert results[0].mentions == 1
    assert results[0].prompts_run == 2
    assert results[0].missed_prompts == [prompts[1]["prompt_text"]]
