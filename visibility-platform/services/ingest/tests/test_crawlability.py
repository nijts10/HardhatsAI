from ingest.crawlability import TEST_AGENTS, run_crawlability_check
from ingest.http_fetch import FetchResult


def _make_org_brand(conn, slug="acme"):
    with conn.cursor() as cur:
        cur.execute("insert into organizations (name, slug) values (%s, %s) returning id", (slug.title(), slug))
        org_id = cur.fetchone()["id"]
        cur.execute(
            "insert into brands (organization_id, name, slug, website) values (%s, %s, %s, %s) returning id",
            (org_id, "Acme Systeemplafonds", slug, f"https://{slug}.example"),
        )
        brand_id = cur.fetchone()["id"]
    return org_id, brand_id


def _canned_fetcher(result: FetchResult):
    return lambda url: result


def test_successful_fetch_evaluates_every_test_agent_and_persists(conn):
    org_id, brand_id = _make_org_brand(conn, slug="crawl-ok")
    robots = """
User-agent: *
Disallow: /private

User-agent: GPTBot
Disallow: /
"""
    check_id, stats = run_crawlability_check(
        conn, brand_id=brand_id, website_url="https://crawl-ok.example",
        fetcher=_canned_fetcher(FetchResult(status_code=200, text=robots, error=None)),
    )

    assert "GPTBot" in stats.blocked
    assert "ClaudeBot" in stats.allowed  # falls back to '*' group, not blocked
    assert not stats.undetermined
    assert stats.fetch_error is None

    with conn.cursor() as cur:
        cur.execute("select robots_txt_url, http_status from crawlability_checks where id = %s", (check_id,))
        check_row = cur.fetchone()
        cur.execute("select count(*) as n from crawlability_agents where check_id = %s", (check_id,))
        agent_count = cur.fetchone()["n"]

    assert check_row["robots_txt_url"] == "https://crawl-ok.example/robots.txt"
    assert check_row["http_status"] == 200
    assert agent_count == len(TEST_AGENTS)


def test_404_robots_txt_is_treated_as_no_restrictions_not_parsed_as_garbage(conn):
    org_id, brand_id = _make_org_brand(conn, slug="crawl-404")
    # A realistic 404 page body that, if naively parsed as robots.txt text,
    # would NOT coincidentally contain any "User-agent:"/"Disallow:" lines
    # -- proving the 404 path treats this as an EMPTY file (default allow),
    # not "parse whatever HTML came back".
    not_found_body = "<html><body><h1>404 Not Found</h1><p>The page you requested does not exist.</p></body></html>"

    check_id, stats = run_crawlability_check(
        conn, brand_id=brand_id, website_url="https://crawl-404.example",
        fetcher=_canned_fetcher(FetchResult(status_code=404, text=not_found_body, error=None)),
    )

    assert stats.blocked == []
    assert len(stats.allowed) == len(TEST_AGENTS)
    assert stats.fetch_error is None


def test_network_error_is_undetermined_not_guessed(conn):
    org_id, brand_id = _make_org_brand(conn, slug="crawl-err")

    check_id, stats = run_crawlability_check(
        conn, brand_id=brand_id, website_url="https://crawl-err.example",
        fetcher=_canned_fetcher(FetchResult(status_code=None, text=None, error="connection timed out")),
    )

    assert stats.allowed == []
    assert stats.blocked == []
    assert len(stats.undetermined) == len(TEST_AGENTS)
    assert stats.fetch_error == "connection timed out"

    with conn.cursor() as cur:
        cur.execute("select allowed from crawlability_agents where check_id = %s", (check_id,))
        rows = cur.fetchall()
    assert all(r["allowed"] is None for r in rows)


def test_unexpected_status_is_undetermined(conn):
    org_id, brand_id = _make_org_brand(conn, slug="crawl-500")

    check_id, stats = run_crawlability_check(
        conn, brand_id=brand_id, website_url="https://crawl-500.example",
        fetcher=_canned_fetcher(FetchResult(status_code=500, text="Internal Server Error", error=None)),
    )

    assert len(stats.undetermined) == len(TEST_AGENTS)
    assert "500" in stats.fetch_error
