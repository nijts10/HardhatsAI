from ingest.benchmark import detect_mention, run_benchmark


def _make_org_brand(conn, slug="acme"):
    with conn.cursor() as cur:
        cur.execute("insert into organizations (name, slug) values (%s, %s) returning id", (slug.title(), slug))
        org_id = cur.fetchone()["id"]
        cur.execute("insert into brands (organization_id, name, slug) values (%s, %s, %s) returning id",
                    (org_id, "Acme Systeemwanden", slug))
        brand_id = cur.fetchone()["id"]
    return org_id, brand_id


def test_detect_mention_case_insensitive():
    assert detect_mention("Ik raad Acme Systeemwanden aan.", "Acme Systeemwanden") == (True, 8)
    assert detect_mention("IK RAAD ACME SYSTEEMWANDEN AAN.", "Acme Systeemwanden") == (True, 8)


def test_detect_mention_false_when_absent():
    mentioned, offset = detect_mention("Ik raad Rockwool aan.", "Acme Systeemwanden")
    assert mentioned is False
    assert offset is None


def test_detect_mention_empty_brand_name_never_matches():
    assert detect_mention("anything at all", "") == (False, None)


def test_detect_mention_credits_a_sub_brand_alias():
    # The real gap found 2026-09-09: an answer that correctly recommends
    # "HeartFelt" without saying "Hunter Douglas" must count as a mention.
    mentioned, offset = detect_mention(
        "Voor akoestiek raad ik HeartFelt aan.", "Hunter Douglas", aliases=["HeartFelt", "PareauLux", "Luxalon"],
    )
    assert mentioned is True
    assert offset == 23


def test_detect_mention_returns_earliest_offset_across_brand_and_aliases():
    text = "Luxalon is een goede keuze, net als Hunter Douglas zelf."
    mentioned, offset = detect_mention(text, "Hunter Douglas", aliases=["Luxalon"])
    assert mentioned is True
    assert offset == 0  # "Luxalon" appears before "Hunter Douglas" in the text


def test_detect_mention_no_aliases_behaves_exactly_as_before():
    assert detect_mention("Ik raad Acme Systeemwanden aan.", "Acme Systeemwanden", aliases=None) == (True, 8)
    assert detect_mention("Ik raad Acme Systeemwanden aan.", "Acme Systeemwanden", aliases=[]) == (True, 8)


def test_run_benchmark_persists_run_and_per_prompt_results(conn):
    org_id, brand_id = _make_org_brand(conn)

    call_count = {"n": 0}

    def fake_caller(prompt_text):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "Voor kantoorscheidingen raad ik Acme Systeemwanden aan vanwege de EI60 certificering."
        return "Er zijn verschillende leveranciers, waaronder Rockwool en Saint-Gobain."

    run_id, stats = run_benchmark(
        conn, organization_id=org_id, brand_id=brand_id, brand_name="Acme Systeemwanden",
        model="chatgpt", caller=fake_caller,
    )

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from prompt_library where is_active")
        prompt_count = cur.fetchone()["n"]

    assert stats.prompts_run == prompt_count
    assert stats.mentions == 1  # only the first canned response mentions Acme
    assert 0 < stats.mention_rate < 1

    with conn.cursor() as cur:
        cur.execute("select status, stats from benchmark_runs where id = %s", (run_id,))
        run_row = cur.fetchone()
        cur.execute("select count(*) as n from benchmark_results where benchmark_run_id = %s", (run_id,))
        result_count = cur.fetchone()["n"]

    assert run_row["status"] == "succeeded"
    assert run_row["stats"]["mentions"] == 1
    assert result_count == prompt_count


def test_run_benchmark_marks_run_failed_on_caller_error(conn):
    org_id, brand_id = _make_org_brand(conn, slug="beta")

    def failing_caller(prompt_text):
        raise RuntimeError("upstream API error")

    try:
        run_benchmark(
            conn, organization_id=org_id, brand_id=brand_id, brand_name="Beta",
            model="gemini", caller=failing_caller,
        )
        assert False, "expected RuntimeError to propagate"
    except RuntimeError:
        pass

    with conn.cursor() as cur:
        cur.execute(
            "select status, error from benchmark_runs where organization_id = %s order by started_at desc limit 1",
            (org_id,),
        )
        run_row = cur.fetchone()

    assert run_row["status"] == "failed"
    assert "upstream API error" in run_row["error"]
