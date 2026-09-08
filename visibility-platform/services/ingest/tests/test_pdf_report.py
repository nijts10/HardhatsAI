import hashlib
import uuid

import pdfplumber
from psycopg.types.json import Json

from ingest.pdf_report import (
    RemediationItem,
    _aggregate_competitive_picture,
    _build_remediation,
    gather_report_data,
    generate_pdf_report,
)


# ---------------------------------------------------------------------
# Pure-Python unit tests -- no DB needed.
# ---------------------------------------------------------------------

def test_aggregate_competitive_picture_dedupes_mentions_and_counts_verdicts():
    mentions = [
        {"intent": "comparative", "brand_name": "Acme", "is_competitor": False},
        {"intent": "comparative", "brand_name": "acme", "is_competitor": False},  # same answer's dup, dedup key differs by case only in a real query (distinct on) -- here just proves aggregation keys on normalised name
        {"intent": "comparative", "brand_name": "Rival", "is_competitor": True},
    ]
    verdicts = [
        {"intent": "comparative", "brand_name": "Acme", "is_competitor": False, "verdict": "correct"},
        {"intent": "comparative", "brand_name": "Acme", "is_competitor": False, "verdict": "incorrect"},
        {"intent": "comparative", "brand_name": "Rival", "is_competitor": True, "verdict": "unverifiable_product"},
    ]
    rows = _aggregate_competitive_picture(mentions, verdicts)
    by_brand = {r["brand_name"].lower(): r for r in rows}

    assert by_brand["acme"]["mentions"] == 2  # the two mention rows collapse into the SAME (intent, normalised-name) key
    assert by_brand["acme"]["correct"] == 1
    assert by_brand["acme"]["incorrect"] == 1
    assert by_brand["rival"]["is_competitor"] is True
    assert by_brand["rival"]["unverifiable"] == 1


def test_build_remediation_orders_by_impact_over_effort():
    crawlability_agents = [{"agent_name": "GPTBot", "allowed": False}, {"agent_name": "ClaudeBot", "allowed": True}]
    items = _build_remediation(
        documentation_gaps=[{"spec_name_nl": "Geluidsabsorptie"}] * 2,
        page_visibility_checks=[{"product_name": "Acme Panel", "value_found_as_text": False}] * 4,
        crawlability_agents=crawlability_agents,
        worst_findings=[{"spec_name_nl": "Brandwerendheid"}] * 6,
    )
    categories = [item.category for item in items]
    # Crawlability (effort=1, impact=3, score=3.0) must be first -- highest impact/effort of any category.
    assert categories[0] == "crawlability"
    assert all(items[i].score >= items[i + 1].score for i in range(len(items) - 1))


def test_build_remediation_empty_when_nothing_to_fix():
    items = _build_remediation([], [], [], [])
    assert items == []


def test_remediation_item_score_is_impact_over_effort():
    assert RemediationItem("t", "c", effort=2, impact=6, detail="d").score == 3.0


# ---------------------------------------------------------------------
# DB + PDF integration test.
# ---------------------------------------------------------------------

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


def _make_prompt(conn, category_id, intent, text):
    with conn.cursor() as cur:
        cur.execute("insert into prompts (category_id, intent, text) values (%s, %s, %s) returning id",
                    (category_id, intent, text))
        return cur.fetchone()["id"]


def _make_run(conn, *, organization_id, brand_id, category_id):
    with conn.cursor() as cur:
        cur.execute(
            "insert into visibility_runs (organization_id, brand_id, category_id, engine, requested_model) "
            "values (%s, %s, %s, 'anthropic', 'claude-sonnet-5') returning id",
            (organization_id, brand_id, category_id),
        )
        return cur.fetchone()["id"]


def _make_answer(conn, *, run_id, prompt_id, response_text, citations=None, resolved_model_version="m1"):
    with conn.cursor() as cur:
        cur.execute(
            "insert into visibility_answers (run_id, prompt_id, replicate_index, response_text, citations, "
            "resolved_model_version) values (%s, %s, 0, %s, %s, %s) returning id",
            (run_id, prompt_id, response_text, Json(citations or []), resolved_model_version),
        )
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


def _make_document_version(conn, org_id, product_id, title="Acme Datasheet"):
    sha256 = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "insert into documents (organization_id, product_id, title, kind) "
            "values (%s, %s, %s, 'datasheet') returning id",
            (org_id, product_id, title),
        )
        document_id = cur.fetchone()["id"]
        cur.execute(
            "insert into document_versions (document_id, storage_path, sha256, source_url, retrieved_at) "
            "values (%s, %s, %s, 'https://example.com/x.pdf', '2026-01-01') returning id",
            (document_id, f"acme/{sha256[:2]}/doc.pdf", sha256),
        )
        return cur.fetchone()["id"]


def _make_spec_attribute(conn, *, key, name_nl=None, comparison_tolerance=None, confusable_with=None):
    with conn.cursor() as cur:
        cur.execute(
            "insert into spec_attributes (key, name_nl, name_en, data_type, comparison_tolerance, confusable_with) "
            "values (%s, %s, %s, 'numeric', %s, %s) returning id",
            (key, name_nl or key, key, comparison_tolerance, confusable_with or []),
        )
        return cur.fetchone()["id"]


def _make_ground_truth(conn, *, product_id, spec_attribute_id, document_version_id, value_numeric, page_number=7,
                        source_snippet="ground truth snippet"):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into claims (product_id, spec_attribute_id, value_numeric, presence, document_version_id,
                                 page_number, source_snippet)
            values (%s, %s, %s, 'stated', %s, %s, %s)
            returning id
            """,
            (product_id, spec_attribute_id, value_numeric, document_version_id, page_number, source_snippet),
        )
        return cur.fetchone()["id"]


def _insert_answer_claim(conn, *, answer_id, product_id, brand_name, is_competitor, spec_attribute_id, verdict,
                          compared_claim_id=None, confusable_key=None, product_name="Acme Panel",
                          value_numeric=1.0, source_quote="quote"):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into answer_claims
              (answer_id, brand_name, product_name, is_competitor, resolved_product_id, resolution_score,
               spec_attribute_id, value_numeric, source_quote, verdict, compared_claim_id, confusable_key)
            values (%s, %s, %s, %s, %s, 1.0, %s, %s, %s, %s, %s, %s)
            """,
            (answer_id, brand_name, product_name, is_competitor, product_id, spec_attribute_id, value_numeric,
             source_quote, verdict, compared_claim_id, confusable_key),
        )


def test_gather_report_data_and_render_pdf_end_to_end(tmp_path, conn):
    org_id, brand_id = _make_org_brand(conn, slug="pdf-e2e")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme E2E Panel",
                                product_url="https://acme.example/e2e")
    doc_version_id = _make_document_version(conn, org_id, product_id, title="Acme E2E Datasheet")

    alpha_id = _make_spec_attribute(conn, key="pdf_e2e_alpha", name_nl="Geluidsabsorptie",
                                     comparison_tolerance=0.02, confusable_with=["pdf_e2e_nrc"])
    nrc_id = _make_spec_attribute(conn, key="pdf_e2e_nrc", name_nl="NRC", comparison_tolerance=0.02,
                                   confusable_with=["pdf_e2e_alpha"])
    gap_spec_id = _make_spec_attribute(conn, key="pdf_e2e_gap", name_nl="Ongedocumenteerde spec")

    alpha_gt = _make_ground_truth(conn, product_id=product_id, spec_attribute_id=alpha_id,
                                   document_version_id=doc_version_id, value_numeric=0.70)
    nrc_gt = _make_ground_truth(conn, product_id=product_id, spec_attribute_id=nrc_id,
                                 document_version_id=doc_version_id, value_numeric=0.90)

    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)
    prompt_id = _make_prompt(conn, category_id, "spec_constrained", "Wat is de alpha_w waarde van Acme?")

    answer_id = _make_answer(conn, run_id=run_id, prompt_id=prompt_id,
                              response_text="Acme Systeemplafonds haalt 0.90 op deze spec.",
                              citations=["https://acme.example/e2e"])

    # An own-brand claim that's actually the CONFUSED value (matches nrc's ground truth, not alpha's)
    # -> verdict likely_confusion, feeds worst findings + remediation.
    _insert_answer_claim(conn, answer_id=answer_id, product_id=product_id, brand_name="Acme Systeemplafonds",
                          is_competitor=False, spec_attribute_id=alpha_id, verdict="likely_confusion",
                          compared_claim_id=nrc_gt, confusable_key="pdf_e2e_nrc", value_numeric=0.90,
                          source_quote="haalt 0.90 op deze spec")
    # A documentation gap on the audited brand.
    _insert_answer_claim(conn, answer_id=answer_id, product_id=product_id, brand_name="Acme Systeemplafonds",
                          is_competitor=False, spec_attribute_id=gap_spec_id, verdict="unverifiable_spec",
                          value_numeric=5.0, source_quote="haalt 0.90 op deze spec")
    # A competitor mention.
    _insert_answer_claim(conn, answer_id=answer_id, product_id=product_id, brand_name="Rival Corp",
                          is_competitor=True, spec_attribute_id=alpha_id, verdict="correct",
                          compared_claim_id=alpha_gt, value_numeric=0.70, source_quote="haalt 0.90 op deze spec")

    with conn.cursor() as cur:
        cur.execute(
            "insert into crawlability_checks (brand_id, website_url, robots_txt_url, http_status, robots_txt_body) "
            "values (%s, %s, %s, 200, 'User-agent: GPTBot\nDisallow: /') returning id",
            (brand_id, "https://acme.example", "https://acme.example/robots.txt"),
        )
        check_id = cur.fetchone()["id"]
        cur.execute(
            "insert into crawlability_agents (check_id, agent_name, allowed, matched_rule, matched_group) "
            "values (%s, 'GPTBot', false, 'Disallow: /', 'GPTBot')",
            (check_id,),
        )

        cur.execute(
            """
            insert into page_spec_visibility
              (product_id, claim_id, product_url, http_status, value_found_as_text, schema_org_product_present)
            values (%s, %s, %s, 200, false, false)
            """,
            (product_id, alpha_gt, "https://acme.example/e2e"),
        )

    data = gather_report_data(conn, run_id=run_id)

    assert data.brand_name == "Acme Systeemplafonds"
    assert len(data.worst_findings) == 1
    assert data.worst_findings[0]["verdict"] == "likely_confusion"
    assert data.worst_findings[0]["gt_document_title"] == "Acme E2E Datasheet"
    assert len(data.documentation_gaps) == 1
    assert data.crawlability_check is not None
    assert any(a["agent_name"] == "GPTBot" and a["allowed"] is False for a in data.crawlability_agents)
    assert len(data.page_visibility_gaps) == 1
    assert any(item.category == "crawlability" for item in data.remediation)
    assert any(item.category == "documentation gap" for item in data.remediation)
    assert any(item.category == "incorrect answer" for item in data.remediation)
    assert any(row["brand_name"] == "Rival Corp" and row["is_competitor"] for row in data.competitive_picture)

    output_path = tmp_path / "report.pdf"
    generated = generate_pdf_report(conn, run_id=run_id, output_path=str(output_path))
    assert output_path.exists()
    assert output_path.stat().st_size > 1000  # a real multi-page PDF, not an empty/corrupt stub
    assert generated.brand_name == "Acme Systeemplafonds"

    with pdfplumber.open(str(output_path)) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    for expected in (
        "AI Visibility Audit: Acme Systeemplafonds", "1. The problem", "2. Introduction",
        "3. About HardhatsAI", "4. Methodology", "5. The research", "6. Conclusions", "Scores",
        "Critical Notes", "Competitive Picture", "Documentation Gaps", "Crawlability",
        "Page-Visibility Gaps", "Remediation", "7. Appendix", "GPTBot", "Rival Corp", "likely_confusion",
        "Presence Rate", "Share of Voice", "Spec Accuracy", "Data Completeness",
    ):
        assert expected in full_text, f"expected {expected!r} in rendered PDF text"

    # Critical notes must always carry advisory content, and the appendix
    # must reproduce the actual raw answer text, not just extracted claims.
    assert "Advice:" in full_text
    assert "Acme Systeemplafonds haalt 0.90 op deze spec." in full_text


def test_unknown_run_id_raises_value_error(conn):
    try:
        gather_report_data(conn, run_id="00000000-0000-0000-0000-000000000000")
        assert False, "expected ValueError for unknown run_id"
    except ValueError:
        pass
