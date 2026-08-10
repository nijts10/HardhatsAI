from ingest.visibility import (
    AnswerResult,
    estimate_cost_usd,
    plan_run,
    run_visibility,
)


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


def _active_prompt_count(conn, category_id):
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from prompts where category_id = %s and is_active", (category_id,))
        return cur.fetchone()["n"]


def _canned_caller(text="Ik raad Acme aan.", model="gpt-4o-2026-08-01", citations=None):
    def _call(prompt_text):
        return AnswerResult(
            response_text=text, resolved_model_version=model,
            citations=citations or ["https://acme.example/product"],
            input_tokens=1000, output_tokens=200,
        )
    return _call


def test_estimate_cost_usd_scales_with_tokens_and_engine_pricing():
    cheap = estimate_cost_usd("gemini", 1_000_000, 1_000_000)
    expensive = estimate_cost_usd("anthropic", 1_000_000, 1_000_000)
    assert 0 < cheap < expensive
    assert estimate_cost_usd("openai", 0, 0) == 0.0


def test_plan_run_counts_active_prompts_and_estimates_cost(conn):
    category_id = _systeemplafond_category(conn)
    prompt_count = _active_prompt_count(conn, category_id)

    plan = plan_run(conn, category_id=category_id, engine="openai", replicates=3)

    assert plan.prompt_count == prompt_count
    assert plan.already_answered == 0
    assert plan.planned_answers == prompt_count * 3
    assert plan.estimated_cost_usd > 0


def test_run_visibility_persists_answers_with_resolved_model_and_citations(conn):
    category_id = _systeemplafond_category(conn)
    org_id, brand_id = _make_org_brand(conn)
    prompt_count = _active_prompt_count(conn, category_id)

    run_id, stats = run_visibility(
        conn, organization_id=org_id, brand_id=brand_id, category_id=category_id,
        engine="openai", requested_model="gpt-4o", caller=_canned_caller(),
        replicates=1, max_answers_per_run=1000, cost_ceiling_usd=1000,
    )

    assert stats.answers_succeeded == prompt_count
    assert stats.answers_failed == 0

    with conn.cursor() as cur:
        cur.execute("select status, cost_usd from visibility_runs where id = %s", (run_id,))
        run_row = cur.fetchone()
        cur.execute(
            "select resolved_model_version, citations, cost_usd from visibility_answers "
            "where run_id = %s limit 1",
            (run_id,),
        )
        answer_row = cur.fetchone()

    assert run_row["status"] == "succeeded"
    assert float(run_row["cost_usd"]) > 0
    assert answer_row["resolved_model_version"] == "gpt-4o-2026-08-01"
    assert answer_row["citations"] == ["https://acme.example/product"]
    assert answer_row["cost_usd"] is not None


def test_run_visibility_default_replicates_is_three(conn):
    category_id = _systeemplafond_category(conn)
    org_id, brand_id = _make_org_brand(conn, slug="triplicate")
    prompt_count = _active_prompt_count(conn, category_id)

    run_id, stats = run_visibility(
        conn, organization_id=org_id, brand_id=brand_id, category_id=category_id,
        engine="anthropic", requested_model="claude-sonnet-5", caller=_canned_caller(),
        max_answers_per_run=10_000, cost_ceiling_usd=10_000,
    )

    assert stats.answers_succeeded == prompt_count * 3
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from visibility_answers where run_id = %s", (run_id,))
        assert cur.fetchone()["n"] == prompt_count * 3


def test_run_visibility_stops_at_max_answers_without_failing_the_run(conn):
    category_id = _systeemplafond_category(conn)
    org_id, brand_id = _make_org_brand(conn, slug="maxanswers")

    run_id, stats = run_visibility(
        conn, organization_id=org_id, brand_id=brand_id, category_id=category_id,
        engine="anthropic", requested_model="claude-sonnet-5", caller=_canned_caller(),
        replicates=1, max_answers_per_run=2, cost_ceiling_usd=1000,
    )

    assert stats.answers_succeeded == 2
    with conn.cursor() as cur:
        cur.execute("select status, stats from visibility_runs where id = %s", (run_id,))
        row = cur.fetchone()
        cur.execute("select count(*) as n from visibility_answers where run_id = %s", (run_id,))
        answer_count = cur.fetchone()["n"]

    assert row["status"] == "running"  # not failed -- an intentional stop
    assert "last_stop_reason" in row["stats"]
    assert answer_count == 2


def test_run_visibility_stops_at_cost_ceiling_without_failing_the_run(conn):
    category_id = _systeemplafond_category(conn)
    org_id, brand_id = _make_org_brand(conn, slug="ceiling")

    run_id, stats = run_visibility(
        conn, organization_id=org_id, brand_id=brand_id, category_id=category_id,
        engine="openai", requested_model="gpt-4o", caller=_canned_caller(),
        replicates=1, max_answers_per_run=1000, cost_ceiling_usd=0.0001,
    )

    assert stats.answers_succeeded == 0
    with conn.cursor() as cur:
        cur.execute("select status, stats from visibility_runs where id = %s", (run_id,))
        row = cur.fetchone()

    assert row["status"] == "running"
    assert "last_stop_reason" in row["stats"]


def test_run_visibility_resumable_across_invocations_skips_existing_rows(conn):
    category_id = _systeemplafond_category(conn)
    org_id, brand_id = _make_org_brand(conn, slug="resume")
    prompt_count = _active_prompt_count(conn, category_id)

    seen_prompts = []

    def counting_caller(prompt_text):
        seen_prompts.append(prompt_text)
        return AnswerResult(response_text=f"answer {len(seen_prompts)}", resolved_model_version="m1",
                             input_tokens=100, output_tokens=50)

    run_id, stats1 = run_visibility(
        conn, organization_id=org_id, brand_id=brand_id, category_id=category_id,
        engine="gemini", requested_model="gemini-2.0-flash", caller=counting_caller,
        replicates=1, max_answers_per_run=5, cost_ceiling_usd=1000,
    )
    assert stats1.answers_succeeded == 5
    assert len(seen_prompts) == 5

    with conn.cursor() as cur:
        cur.execute("select status from visibility_runs where id = %s", (run_id,))
        assert cur.fetchone()["status"] == "running"

    run_id_2, stats2 = run_visibility(
        conn, organization_id=org_id, brand_id=brand_id, category_id=category_id,
        engine="gemini", requested_model="gemini-2.0-flash", caller=counting_caller,
        replicates=1, max_answers_per_run=1000, cost_ceiling_usd=1000,
        resume_run_id=run_id,
    )

    assert run_id_2 == run_id
    assert stats2.answers_succeeded == prompt_count - 5
    # The first 5 prompts were never re-called -- resumability skipped them.
    assert len(seen_prompts) == prompt_count

    with conn.cursor() as cur:
        cur.execute("select status from visibility_runs where id = %s", (run_id,))
        assert cur.fetchone()["status"] == "succeeded"
        cur.execute("select count(*) as n from visibility_answers where run_id = %s", (run_id,))
        assert cur.fetchone()["n"] == prompt_count


def test_run_visibility_leaves_run_running_when_a_call_fails_so_it_can_be_retried(conn):
    category_id = _systeemplafond_category(conn)
    org_id, brand_id = _make_org_brand(conn, slug="flaky")
    prompt_count = _active_prompt_count(conn, category_id)

    call_count = {"n": 0}

    def flaky_caller(prompt_text):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("upstream 500")
        return AnswerResult(response_text="ok", resolved_model_version="m1", input_tokens=10, output_tokens=10)

    run_id, stats = run_visibility(
        conn, organization_id=org_id, brand_id=brand_id, category_id=category_id,
        engine="gemini", requested_model="gemini-2.0-flash", caller=flaky_caller,
        replicates=1, max_answers_per_run=1000, cost_ceiling_usd=1000,
    )

    assert stats.answers_failed == 1
    assert stats.answers_succeeded == prompt_count - 1

    with conn.cursor() as cur:
        cur.execute("select status, stats from visibility_runs where id = %s", (run_id,))
        row = cur.fetchone()
    # NOT 'succeeded' -- one (prompt, replicate) pair is still missing, and
    # marking this done would silently hide it forever (status is engine-
    # locked once terminal; nothing would ever retry that gap again).
    assert row["status"] == "running"
    assert "last_stop_reason" in row["stats"]

    # Resuming retries only the missing pair and now completes for real.
    run_id_2, stats2 = run_visibility(
        conn, organization_id=org_id, brand_id=brand_id, category_id=category_id,
        engine="gemini", requested_model="gemini-2.0-flash", caller=flaky_caller,
        replicates=1, max_answers_per_run=1000, cost_ceiling_usd=1000,
        resume_run_id=run_id,
    )
    assert run_id_2 == run_id
    assert stats2.answers_succeeded == 1
    with conn.cursor() as cur:
        cur.execute("select status from visibility_runs where id = %s", (run_id,))
        assert cur.fetchone()["status"] == "succeeded"


def test_run_visibility_rejects_resume_with_mismatched_engine(conn):
    category_id = _systeemplafond_category(conn)
    org_id, brand_id = _make_org_brand(conn, slug="mismatch")

    run_id, _ = run_visibility(
        conn, organization_id=org_id, brand_id=brand_id, category_id=category_id,
        engine="openai", requested_model="gpt-4o", caller=_canned_caller(),
        replicates=1, max_answers_per_run=1, cost_ceiling_usd=1000,
    )

    try:
        run_visibility(
            conn, organization_id=org_id, brand_id=brand_id, category_id=category_id,
            engine="anthropic", requested_model="claude-sonnet-5", caller=_canned_caller(),
            replicates=1, max_answers_per_run=1000, cost_ceiling_usd=1000,
            resume_run_id=run_id,
        )
        assert False, "expected ValueError for engine mismatch"
    except ValueError:
        pass
