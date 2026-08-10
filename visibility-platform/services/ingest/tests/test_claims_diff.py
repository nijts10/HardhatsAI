"""Required tests per the MVP brief for Part 4: quote verification incl. a
deliberate hallucinated-quote fixture, unit normalisation, and tolerance
comparison including the confusion cases -- run against the real schema,
not mocks."""
import hashlib
import json
import uuid

from ingest.claims_diff import make_anthropic_extractor, run_claims_extraction


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


def _make_spec_attribute(conn, *, key, data_type="numeric", unit=None, comparison_tolerance=None, confusable_with=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into spec_attributes (key, name_nl, name_en, data_type, unit, comparison_tolerance, confusable_with)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (key, key, key, data_type, unit, comparison_tolerance, confusable_with or []),
        )
        return cur.fetchone()["id"]


def _make_ground_truth(conn, *, product_id, spec_attribute_id, document_version_id, presence="stated", **values):
    row = {"value_numeric": None, "value_numeric_max": None, "value_text": None, "value_bool": None,
           "value_enum": None, "unit": None}
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
             "document_version_id": document_version_id, "presence": presence, **row},
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


def _make_answer(conn, *, run_id, category_id, response_text, replicate_index=0):
    with conn.cursor() as cur:
        cur.execute("select id from prompts where category_id = %s limit 1", (category_id,))
        prompt_id = cur.fetchone()["id"]
        cur.execute(
            "insert into visibility_answers (run_id, prompt_id, replicate_index, response_text) "
            "values (%s, %s, %s, %s) returning id",
            (run_id, prompt_id, replicate_index, response_text),
        )
        return cur.fetchone()["id"]


def _canned_extractor(claims):
    return lambda prompt_text: claims


def _scripted_extractor(script: dict):
    def _call(prompt_text):
        for response_text, claims in script.items():
            if response_text in prompt_text:
                return claims
        return []
    return _call


def test_hallucinated_quote_is_discarded_not_persisted(conn):
    org_id, brand_id = _make_org_brand(conn, slug="quote")
    category_id = _systeemplafond_category(conn)
    _make_spec_attribute(conn, key="thickness_test_quote", unit="mm", comparison_tolerance=1)
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)

    response_text = "The Acme 40 panel is 40mm thick."
    answer_id = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=response_text)

    hallucinated = [{
        "brand_name": "Acme", "product_name": "Acme 40", "spec_key": "thickness_test_quote",
        "value_numeric": 45, "unit": "mm",
        # Not a verbatim substring of response_text (says 40mm, not 45mm) -- hallucinated.
        "source_quote": "The Acme 40 panel is 45mm thick",
    }]

    stats = run_claims_extraction(conn, run_id=run_id, caller=_canned_extractor(hallucinated))

    assert stats.claims_extracted == 0
    assert stats.claims_rejected_unverifiable_quote == 1
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from answer_claims where answer_id = %s", (answer_id,))
        assert cur.fetchone()["n"] == 0


def test_unknown_spec_key_is_silently_dropped(conn):
    org_id, brand_id = _make_org_brand(conn, slug="unknown")
    category_id = _systeemplafond_category(conn)
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)
    response_text = "The Acme 40 panel has a made-up spec of 99."
    _make_answer(conn, run_id=run_id, category_id=category_id, response_text=response_text)

    claim = [{
        "brand_name": "Acme", "product_name": "Acme 40", "spec_key": "not_a_real_spec_key_at_all",
        "value_numeric": 99, "source_quote": "made-up spec of 99",
    }]
    stats = run_claims_extraction(conn, run_id=run_id, caller=_canned_extractor(claim))

    assert stats.claims_extracted == 0
    assert stats.claims_rejected_unknown_spec == 1


def test_unit_normalisation_converts_before_comparing(conn):
    org_id, brand_id = _make_org_brand(conn, slug="units")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme Unit Panel")
    doc_version_id = _make_document_version(conn, org_id, product_id)
    spec_id = _make_spec_attribute(conn, key="thickness_test_units", unit="mm", comparison_tolerance=0)
    _make_ground_truth(conn, product_id=product_id, spec_attribute_id=spec_id,
                        document_version_id=doc_version_id, value_numeric=400, unit="mm")

    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)
    response_text = "Acme Unit Panel is 40 cm thick."
    answer_id = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=response_text)

    claim = [{
        "brand_name": "Acme", "product_name": "Acme Unit Panel", "spec_key": "thickness_test_units",
        "value_numeric": 40, "unit": "cm", "source_quote": "Acme Unit Panel is 40 cm thick",
    }]
    stats = run_claims_extraction(conn, run_id=run_id, caller=_canned_extractor(claim))
    assert stats.claims_extracted == 1

    with conn.cursor() as cur:
        cur.execute("select verdict, value_numeric, unit from answer_claims where answer_id = %s", (answer_id,))
        row = cur.fetchone()

    assert row["verdict"] == "correct"
    assert float(row["value_numeric"]) == 400.0
    assert row["unit"] == "mm"


def test_tolerance_correct_incorrect_and_confusion_verdicts(conn):
    org_id, brand_id = _make_org_brand(conn, slug="tolerance")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme Tol Panel")
    doc_version_id = _make_document_version(conn, org_id, product_id)

    alpha_id = _make_spec_attribute(conn, key="alpha_test", comparison_tolerance=0.05, confusable_with=["nrc_test"])
    nrc_id = _make_spec_attribute(conn, key="nrc_test", comparison_tolerance=0.05, confusable_with=["alpha_test"])
    _make_ground_truth(conn, product_id=product_id, spec_attribute_id=alpha_id,
                        document_version_id=doc_version_id, value_numeric=0.70)
    _make_ground_truth(conn, product_id=product_id, spec_attribute_id=nrc_id,
                        document_version_id=doc_version_id, value_numeric=0.90)

    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)

    resp_correct = "Acme Tol Panel has alpha_test of 0.72."
    resp_incorrect = "Acme Tol Panel has alpha_test of 0.50."
    resp_confused = "Acme Tol Panel has alpha_test of 0.90."

    ans_correct = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=resp_correct,
                                replicate_index=0)
    ans_incorrect = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=resp_incorrect,
                                  replicate_index=1)
    ans_confused = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=resp_confused,
                                 replicate_index=2)

    script = {
        resp_correct: [{"brand_name": "Acme", "product_name": "Acme Tol Panel", "spec_key": "alpha_test",
                         "value_numeric": 0.72, "source_quote": "alpha_test of 0.72"}],
        resp_incorrect: [{"brand_name": "Acme", "product_name": "Acme Tol Panel", "spec_key": "alpha_test",
                           "value_numeric": 0.50, "source_quote": "alpha_test of 0.50"}],
        resp_confused: [{"brand_name": "Acme", "product_name": "Acme Tol Panel", "spec_key": "alpha_test",
                          "value_numeric": 0.90, "source_quote": "alpha_test of 0.90"}],
    }
    stats = run_claims_extraction(conn, run_id=run_id, caller=_scripted_extractor(script))
    assert stats.claims_extracted == 3

    with conn.cursor() as cur:
        cur.execute(
            "select answer_id, verdict, confusable_key from answer_claims where answer_id = any(%s)",
            ([ans_correct, ans_incorrect, ans_confused],),
        )
        rows = {r["answer_id"]: r for r in cur.fetchall()}

    assert rows[ans_correct]["verdict"] == "correct"
    assert rows[ans_incorrect]["verdict"] == "incorrect"
    assert rows[ans_confused]["verdict"] == "likely_confusion"
    assert rows[ans_confused]["confusable_key"] == "nrc_test"


def test_missing_or_not_stated_ground_truth_is_unverifiable_spec(conn):
    org_id, brand_id = _make_org_brand(conn, slug="unverifspec")
    category_id = _systeemplafond_category(conn)
    product_id = _make_product(conn, brand_id, category_id, "Acme NoGT Panel")
    doc_version_id = _make_document_version(conn, org_id, product_id)

    _make_spec_attribute(conn, key="spec_no_row")
    not_stated_id = _make_spec_attribute(conn, key="spec_not_stated")
    _make_ground_truth(conn, product_id=product_id, spec_attribute_id=not_stated_id,
                        document_version_id=doc_version_id, presence="not_stated")

    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)
    response_text = "Acme NoGT Panel has spec_no_row of 5 and spec_not_stated of 7."
    answer_id = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=response_text)

    claims = [
        {"brand_name": "Acme", "product_name": "Acme NoGT Panel", "spec_key": "spec_no_row",
         "value_numeric": 5, "source_quote": "spec_no_row of 5"},
        {"brand_name": "Acme", "product_name": "Acme NoGT Panel", "spec_key": "spec_not_stated",
         "value_numeric": 7, "source_quote": "spec_not_stated of 7"},
    ]
    stats = run_claims_extraction(conn, run_id=run_id, caller=_canned_extractor(claims))
    assert stats.claims_extracted == 2

    with conn.cursor() as cur:
        cur.execute("select verdict from answer_claims where answer_id = %s", (answer_id,))
        verdicts = {r["verdict"] for r in cur.fetchall()}
    assert verdicts == {"unverifiable_spec"}


def test_unresolvable_product_name_is_unverifiable_product(conn):
    org_id, brand_id = _make_org_brand(conn, slug="noprod")
    category_id = _systeemplafond_category(conn)
    _make_spec_attribute(conn, key="spec_noprod")
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)

    response_text = "Zorbex Ultraflex 9000 has spec_noprod of 5."
    answer_id = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=response_text)

    claim = [{"brand_name": "Zorbex", "product_name": "Zorbex Ultraflex 9000", "spec_key": "spec_noprod",
              "value_numeric": 5, "source_quote": "spec_noprod of 5"}]
    stats = run_claims_extraction(conn, run_id=run_id, caller=_canned_extractor(claim))
    assert stats.claims_extracted == 1

    with conn.cursor() as cur:
        cur.execute("select verdict, resolved_product_id from answer_claims where answer_id = %s", (answer_id,))
        row = cur.fetchone()
    assert row["verdict"] == "unverifiable_product"
    assert row["resolved_product_id"] is None


def test_competitor_brand_is_flagged(conn):
    org_id, brand_id = _make_org_brand(conn, slug="brandflag")
    category_id = _systeemplafond_category(conn)
    _make_spec_attribute(conn, key="spec_brandflag")
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)

    response_text = "Both Acme Systeemplafonds Model X and Rival Corp Model Y have spec_brandflag of 5."
    answer_id = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=response_text)

    claims = [
        {"brand_name": "Acme Systeemplafonds", "product_name": "Model X", "spec_key": "spec_brandflag",
         "value_numeric": 5, "source_quote": "spec_brandflag of 5"},
        {"brand_name": "Rival Corp", "product_name": "Model Y", "spec_key": "spec_brandflag",
         "value_numeric": 5, "source_quote": "spec_brandflag of 5"},
    ]
    stats = run_claims_extraction(conn, run_id=run_id, caller=_canned_extractor(claims))
    assert stats.claims_extracted == 2

    with conn.cursor() as cur:
        cur.execute("select brand_name, is_competitor from answer_claims where answer_id = %s", (answer_id,))
        by_brand = {r["brand_name"]: r["is_competitor"] for r in cur.fetchall()}
    assert by_brand["Acme Systeemplafonds"] is False
    assert by_brand["Rival Corp"] is True


def test_answer_already_processed_is_skipped_on_rerun(conn):
    org_id, brand_id = _make_org_brand(conn, slug="resume-claims")
    category_id = _systeemplafond_category(conn)
    _make_spec_attribute(conn, key="spec_resume")
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)

    response_text = "Acme Resume Panel has spec_resume of 5."
    answer_id = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=response_text)
    claim = [{"brand_name": "Acme", "product_name": "Acme Resume Panel", "spec_key": "spec_resume",
              "value_numeric": 5, "source_quote": "spec_resume of 5"}]

    stats1 = run_claims_extraction(conn, run_id=run_id, caller=_canned_extractor(claim))
    assert stats1.answers_processed == 1
    assert stats1.claims_extracted == 1

    stats2 = run_claims_extraction(conn, run_id=run_id, caller=_canned_extractor(claim))
    assert stats2.answers_processed == 0
    assert stats2.answers_skipped_already_done == 1
    assert stats2.claims_extracted == 0

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from answer_claims where answer_id = %s", (answer_id,))
        assert cur.fetchone()["n"] == 1


def test_unknown_run_id_raises_value_error(conn):
    try:
        run_claims_extraction(conn, run_id="00000000-0000-0000-0000-000000000000", caller=_canned_extractor([]))
        assert False, "expected ValueError for unknown run_id"
    except ValueError:
        pass


def test_value_type_mismatch_is_rejected_not_a_constraint_violation(conn):
    # The tool call tags a NUMERIC spec but only fills value_text --
    # answer_claims_value_ck requires exactly one value_* column, so
    # without the dispatch-by-data_type fix this would either insert an
    # all-null row (constraint violation) or a wrongly-typed value_text
    # row past validation entirely.
    org_id, brand_id = _make_org_brand(conn, slug="mismatch-type")
    category_id = _systeemplafond_category(conn)
    _make_spec_attribute(conn, key="spec_mismatch_numeric", data_type="numeric")
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)

    response_text = "Acme Mismatch Panel has spec_mismatch_numeric of forty."
    _make_answer(conn, run_id=run_id, category_id=category_id, response_text=response_text)

    claim = [{
        "brand_name": "Acme", "product_name": "Acme Mismatch Panel", "spec_key": "spec_mismatch_numeric",
        "value_text": "forty", "source_quote": "spec_mismatch_numeric of forty",
    }]
    stats = run_claims_extraction(conn, run_id=run_id, caller=_canned_extractor(claim))

    assert stats.claims_extracted == 0
    assert stats.claims_rejected_value_type_mismatch == 1


def test_extra_stray_value_field_is_discarded_not_inserted(conn):
    # The opposite mismatch case: BOTH value_numeric (correct for this
    # spec's data_type) AND a stray value_text are set on the same claim.
    # The stray field must be silently dropped, not cause a constraint
    # violation or get persisted alongside the real value.
    org_id, brand_id = _make_org_brand(conn, slug="mismatch-extra")
    category_id = _systeemplafond_category(conn)
    _make_spec_attribute(conn, key="spec_extra_field", data_type="numeric", unit="mm")
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)

    response_text = "Acme Extra Panel has spec_extra_field of 40 mm."
    answer_id = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=response_text)

    claim = [{
        "brand_name": "Acme", "product_name": "Acme Extra Panel", "spec_key": "spec_extra_field",
        "value_numeric": 40, "unit": "mm", "value_text": "forty millimeters -- should be dropped",
        "source_quote": "spec_extra_field of 40 mm",
    }]
    stats = run_claims_extraction(conn, run_id=run_id, caller=_canned_extractor(claim))

    assert stats.claims_extracted == 1
    with conn.cursor() as cur:
        cur.execute("select value_numeric, value_text from answer_claims where answer_id = %s", (answer_id,))
        row = cur.fetchone()
    assert float(row["value_numeric"]) == 40.0
    assert row["value_text"] is None


def test_anthropic_extractor_unwraps_double_encoded_claims_string():
    # Same failure mode extraction.py's coerce_array guards against: the
    # model sometimes returns the "claims" field as a JSON-encoded string
    # instead of a structured array. Without reusing coerce_array here,
    # this would iterate as characters and raise a TypeError.
    fake_claims = [{
        "brand_name": "Acme", "product_name": "Acme Panel", "spec_key": "spec_x",
        "value_numeric": 5, "source_quote": "spec_x of 5",
    }]

    class _ToolUseBlock:
        type = "tool_use"
        name = "emit_answer_claims"
        input = {"claims": json.dumps({"claims": fake_claims})}

    class _Message:
        content = [_ToolUseBlock()]

    class _MessagesAPI:
        def create(self, **kwargs):
            return _Message()

    class _Client:
        messages = _MessagesAPI()

    caller = make_anthropic_extractor(_Client(), model="claude-sonnet-5")
    raw = caller("irrelevant prompt text")

    assert raw == fake_claims


def test_commit_each_answer_true_persists_claims_durably(conn):
    org_id, brand_id = _make_org_brand(conn, slug="claims-durable")
    category_id = _systeemplafond_category(conn)
    _make_spec_attribute(conn, key="spec_durable")
    run_id = _make_run(conn, organization_id=org_id, brand_id=brand_id, category_id=category_id)

    response_text = "Acme Durable Panel has spec_durable of 5."
    answer_id = _make_answer(conn, run_id=run_id, category_id=category_id, response_text=response_text)
    claim = [{"brand_name": "Acme", "product_name": "Acme Durable Panel", "spec_key": "spec_durable",
              "value_numeric": 5, "source_quote": "spec_durable of 5"}]

    try:
        stats = run_claims_extraction(
            conn, run_id=run_id, caller=_canned_extractor(claim), commit_each_answer=True,
        )
        assert stats.claims_extracted == 1

        # A rollback here must NOT remove anything already committed.
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("select count(*) as n from answer_claims where answer_id = %s", (answer_id,))
            assert cur.fetchone()["n"] == 1
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from answer_claims where answer_id = %s", (answer_id,))
            cur.execute("delete from visibility_answers where run_id = %s", (run_id,))
            cur.execute("delete from visibility_runs where id = %s", (run_id,))
            cur.execute("delete from brands where id = %s", (brand_id,))
            cur.execute("delete from organizations where id = %s", (org_id,))
        conn.commit()
