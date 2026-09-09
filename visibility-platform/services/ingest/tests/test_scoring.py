"""Part 6: the four independent scores, computed against real fixtures --
each one built to exercise: overall vs. per-intent breakdown, a
zero-denominator bucket staying absent (None, never a fabricated 0), and
that unverifiable verdicts never drag spec_accuracy down."""
import hashlib
import uuid

from ingest.scoring import compute_scorecard


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
        cur.execute(
            "insert into prompts (category_id, intent, text) values (%s, %s, %s) returning id",
            (category_id, intent, text),
        )
        return cur.fetchone()["id"]


def _make_run(conn, *, organization_id, brand_id, category_id):
    with conn.cursor() as cur:
        cur.execute(
            "insert into visibility_runs (organization_id, brand_id, category_id, engine, requested_model) "
            "values (%s, %s, %s, 'anthropic', 'claude-sonnet-5') returning id",
            (organization_id, brand_id, category_id),
        )
        return cur.fetchone()["id"]


def _make_answer(conn, *, run_id, prompt_id, response_text, replicate_index=0):
    with conn.cursor() as cur:
        cur.execute(
            "insert into visibility_answers (run_id, prompt_id, replicate_index, response_text) "
            "values (%s, %s, %s, %s) returning id",
            (run_id, prompt_id, replicate_index, response_text),
        )
        return cur.fetchone()["id"]


def _make_product(conn, brand_id, category_id, name):
    slug = name.lower().replace(" ", "-") + "-" + uuid.uuid4().hex[:8]
    with conn.cursor() as cur:
        cur.execute(
            "insert into products (brand_id, category_id, name, slug) values (%s, %s, %s, %s) returning id",
            (brand_id, category_id, name, slug),
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


def _make_spec_attribute(conn, *, key, data_type="numeric", unit=None, category_codes=None):
    with conn.cursor() as cur:
        cur.execute(
            "insert into spec_attributes (key, name_nl, name_en, data_type, unit, category_codes) "
            "values (%s, %s, %s, %s, %s, %s) returning id",
            (key, key, key, data_type, unit, category_codes or []),
        )
        return cur.fetchone()["id"]


def _make_ground_truth_claim(conn, *, product_id, spec_attribute_id, document_version_id, **values):
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


def _insert_answer_claim(conn, *, answer_id, brand_name, is_competitor, spec_attribute_id, verdict,
                          product_name="Some Product", value_numeric=None, resolved_product_id=None):
    # answer_claims_value_ck requires exactly one value_* column -- default
    # to a placeholder text value when the test doesn't care about the
    # actual value (only verdict/brand/competitor-flag matter there).
    value_text = None if value_numeric is not None else "placeholder"
    # answer_claims_verdict_product_ck: resolved_product_id is null IFF
    # verdict = 'unverifiable_product' -- any other verdict needs a real
    # (fake-but-real-row) product_id, resolution doesn't matter for these
    # scoring tests so a throwaway product is enough.
    if resolved_product_id is None and verdict != "unverifiable_product":
        with conn.cursor() as cur:
            cur.execute(
                "insert into products (brand_id, name, slug) values "
                "((select brand_id from visibility_runs vr join visibility_answers va on va.run_id = vr.id "
                " where va.id = %s), 'placeholder', %s) returning id",
                (answer_id, f"placeholder-{uuid.uuid4().hex[:8]}"),
            )
            resolved_product_id = cur.fetchone()["id"]
    resolution_score = 1.0 if resolved_product_id is not None else None

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into answer_claims
              (answer_id, brand_name, product_name, is_competitor, resolved_product_id, resolution_score,
               spec_attribute_id, value_numeric, value_text, source_quote, verdict)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'quote', %s)
            """,
            (answer_id, brand_name, product_name, is_competitor, resolved_product_id, resolution_score,
             spec_attribute_id, value_numeric, value_text, verdict),
        )


def test_presence_rate_overall_and_per_intent(conn):
    org_id, brand_id = _make_org_brand(conn, slug="score-presence")
    category_id = _systeemplafond_category(conn)
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)

    p1 = _make_prompt(conn, category_id, "brand_direct", "Wat vindt u van Acme Systeemplafonds?")
    p2 = _make_prompt(conn, category_id, "spec_constrained", "Welk systeemplafond haalt EI60?")

    _make_answer(conn, run_id=run_id, prompt_id=p1, response_text="Acme Systeemplafonds is een goede keuze.")
    _make_answer(conn, run_id=run_id, prompt_id=p2, response_text="Er zijn meerdere merken die EI60 halen.")
    _make_answer(conn, run_id=run_id, prompt_id=p2, response_text="Rockwool en Knauf zijn opties.", replicate_index=1)

    card = compute_scorecard(conn, run_id=run_id)

    assert card.presence_rate.overall == 1 / 3
    assert card.presence_rate.per_intent["brand_direct"] == 1.0
    assert card.presence_rate.per_intent["spec_constrained"] == 0.0
    # comparative had zero answers in this run -- must not appear as a fabricated 0.
    assert "comparative" not in card.presence_rate.per_intent


def test_presence_rate_credits_a_sub_brand_mention_via_product_lines(conn):
    # Real gap found 2026-09-09 (prompt-taxonomy-proposal): an AI answer
    # that correctly recommends "HeartFelt" without saying the parent
    # company name "Acme Systeemplafonds" must still count as presence.
    org_id, brand_id = _make_org_brand(conn, slug="score-subbrand")
    category_id = _systeemplafond_category(conn)
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)
    with conn.cursor() as cur:
        cur.execute(
            "insert into product_lines (brand_id, name, slug) values (%s, %s, %s)",
            (brand_id, "HeartFelt", "heartfelt"),
        )

    p1 = _make_prompt(conn, category_id, "spec_constrained", "Welk systeemplafond haalt EI60?")
    _make_answer(conn, run_id=run_id, prompt_id=p1, response_text="Ik raad HeartFelt aan vanwege de EI60-waarde.")

    card = compute_scorecard(conn, run_id=run_id)

    assert card.presence_rate.overall == 1.0


def test_share_of_voice_dedupes_multiple_claims_per_answer_and_excludes_competitors_from_denominator_correctly(conn):
    org_id, brand_id = _make_org_brand(conn, slug="score-sov")
    category_id = _systeemplafond_category(conn)
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)
    prompt_id = _make_prompt(conn, category_id, "comparative", "Vergelijk Acme met concurrenten.")

    spec_a = _make_spec_attribute(conn, key="score_sov_spec_a")
    spec_b = _make_spec_attribute(conn, key="score_sov_spec_b")

    answer_1 = _make_answer(conn, run_id=run_id, prompt_id=prompt_id, response_text="Acme en Rival worden genoemd.")
    # TWO claims about Acme in the SAME answer -- must count as ONE mention for share of voice.
    _insert_answer_claim(conn, answer_id=answer_1, brand_name="Acme Systeemplafonds", is_competitor=False,
                          spec_attribute_id=spec_a, verdict="unverifiable_product")
    _insert_answer_claim(conn, answer_id=answer_1, brand_name="Acme Systeemplafonds", is_competitor=False,
                          spec_attribute_id=spec_b, verdict="unverifiable_product")
    _insert_answer_claim(conn, answer_id=answer_1, brand_name="Rival Corp", is_competitor=True,
                          spec_attribute_id=spec_a, verdict="unverifiable_product")

    answer_2 = _make_answer(conn, run_id=run_id, prompt_id=prompt_id, response_text="Alleen Rival genoemd.",
                             replicate_index=1)
    _insert_answer_claim(conn, answer_id=answer_2, brand_name="Rival Corp", is_competitor=True,
                          spec_attribute_id=spec_a, verdict="unverifiable_product")

    card = compute_scorecard(conn, run_id=run_id)

    # Distinct (answer, brand) pairs: (a1,Acme), (a1,Rival), (a2,Rival) = 3 total, 1 is Acme.
    assert card.share_of_voice.overall == 1 / 3
    assert card.share_of_voice.per_intent["comparative"] == 1 / 3


def test_spec_accuracy_excludes_unverifiable_and_ignores_competitor_claims(conn):
    org_id, brand_id = _make_org_brand(conn, slug="score-accuracy")
    category_id = _systeemplafond_category(conn)
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)
    prompt_id = _make_prompt(conn, category_id, "spec_constrained", "Wat is de alpha_w waarde?")

    spec_id = _make_spec_attribute(conn, key="score_accuracy_spec")
    answer_id = _make_answer(conn, run_id=run_id, prompt_id=prompt_id, response_text="Acme haalt 0.85.")

    _insert_answer_claim(conn, answer_id=answer_id, brand_name="Acme Systeemplafonds", is_competitor=False,
                          spec_attribute_id=spec_id, verdict="correct")
    _insert_answer_claim(conn, answer_id=answer_id, brand_name="Acme Systeemplafonds", is_competitor=False,
                          spec_attribute_id=spec_id, verdict="incorrect")
    _insert_answer_claim(conn, answer_id=answer_id, brand_name="Acme Systeemplafonds", is_competitor=False,
                          spec_attribute_id=spec_id, verdict="unverifiable_spec")
    # A competitor claim that's WRONG must not affect the audited brand's own accuracy.
    _insert_answer_claim(conn, answer_id=answer_id, brand_name="Rival Corp", is_competitor=True,
                          spec_attribute_id=spec_id, verdict="incorrect")

    card = compute_scorecard(conn, run_id=run_id)

    # correct=1, incorrect=1, unverifiable_spec excluded entirely -> 1/2, not 1/3 or 1/4.
    assert card.spec_accuracy.overall == 0.5
    assert card.spec_accuracy.per_intent["spec_constrained"] == 0.5


def test_data_completeness_reuses_report_compute_coverage(conn):
    org_id, brand_id = _make_org_brand(conn, slug="score-completeness")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme Completeness Panel")
    doc_version_id = _make_document_version(conn, org_id, product_id)

    spec_stated = _make_spec_attribute(conn, key="score_completeness_stated")
    _make_spec_attribute(conn, key="score_completeness_missing")  # applicable but never claimed -- "missing"
    _make_ground_truth_claim(conn, product_id=product_id, spec_attribute_id=spec_stated,
                              document_version_id=doc_version_id, value_numeric=1)

    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)

    from ingest.report import compute_coverage
    expected = compute_coverage(conn, brand_id).completeness

    card = compute_scorecard(conn, run_id=run_id)
    assert card.data_completeness == expected
    assert 0 < card.data_completeness < 1  # sanity: exactly one of the two specs is stated


def test_claims_extracted_for_run_flag_reflects_whether_any_answer_claims_exist(conn):
    org_id, brand_id = _make_org_brand(conn, slug="score-flag")
    category_id = _systeemplafond_category(conn)
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)
    prompt_id = _make_prompt(conn, category_id, "brand_direct", "Ken je Acme?")
    _make_answer(conn, run_id=run_id, prompt_id=prompt_id, response_text="Ja, Acme Systeemplafonds is bekend.")

    card = compute_scorecard(conn, run_id=run_id)
    assert card.claims_extracted_for_run is False
    # No answer_claims at all -- share_of_voice/spec_accuracy have nothing to compute from.
    assert card.share_of_voice.overall is None
    assert card.spec_accuracy.overall is None
    # presence_rate is independent of answer_claims -- still works.
    assert card.presence_rate.overall == 1.0


def test_unknown_run_id_raises_value_error(conn):
    try:
        compute_scorecard(conn, run_id="00000000-0000-0000-0000-000000000000")
        assert False, "expected ValueError for unknown run_id"
    except ValueError:
        pass
