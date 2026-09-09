from ingest.chunking import build_chunks
from ingest.http_fetch import FetchResult
from ingest.webpage import compute_allowed_prefix, crawl, discover_links, extract_title, is_product_page, parse_webpage


def test_parse_webpage_strips_boilerplate_and_keeps_body_text():
    html = """
    <html><head><title>Test</title></head>
    <body>
      <nav>Menu item one</nav>
      <header>Site header</header>
      <main>
        <h1>HeartFelt Ceiling System</h1>
        <p>Gemaakt van thermogevormde polyestervezels.</p>
      </main>
      <footer>Copyright 2026</footer>
      <script>var x = 1;</script>
    </body></html>
    """
    page = parse_webpage(html, "https://example.com/nl-nl/heartfelt/")
    assert "Menu item one" not in page.text
    assert "Site header" not in page.text
    assert "Copyright 2026" not in page.text
    assert "var x = 1" not in page.text
    assert "HeartFelt Ceiling System" in page.text
    assert "thermogevormde polyestervezels" in page.text
    assert page.page_number == 1


def test_parse_webpage_headings_become_real_l1_l2_chunks_via_build_chunks():
    # Real end-to-end proof this reuses build_chunks completely unchanged:
    # an h1/h2 pair from real HTML must be detected exactly the way a
    # PDF's font-size-based heading would be.
    html = """
    <body>
      <h1>Systeemplafonds</h1>
      <h2>Brandwerendheid</h2>
      <p>Dit product heeft brandklasse A2-s1,d0.</p>
    </body>
    """
    page = parse_webpage(html, "https://example.com/nl-nl/plafonds/")
    chunks = build_chunks([page])
    assert len(chunks) == 1
    assert chunks[0].heading_path == ["Systeemplafonds", "Brandwerendheid"]
    assert "A2-s1,d0" in chunks[0].content


def test_parse_webpage_extracts_table_rows():
    html = """
    <body>
      <table>
        <tr><th>Spec</th><th>Waarde</th></tr>
        <tr><td>Brandklasse</td><td>A2-s1,d0</td></tr>
      </table>
    </body>
    """
    page = parse_webpage(html, "https://example.com/nl-nl/plafonds/")
    assert len(page.tables) == 1
    assert page.tables[0].rows == [["Spec", "Waarde"], ["Brandklasse", "A2-s1,d0"]]


def test_extract_title_returns_title_tag_text():
    assert extract_title("<html><head><title> HeartFelt | Hunter Douglas </title></head></html>") == \
        "HeartFelt | Hunter Douglas"
    assert extract_title("<html><head></head></html>") is None


def test_discover_links_stays_within_prefix_and_drops_non_content():
    html = """
    <body>
      <a href="/nl-nl/plafonds/heartfelt/">HeartFelt</a>
      <a href="/nl-nl/plafonds/datasheet.pdf">Datasheet</a>
      <a href="/en-eu/ceilings/">English version</a>
      <a href="https://other-domain.com/nl-nl/x">Other domain</a>
      <a href="#section">Anchor only</a>
      <a href="mailto:info@example.com">Email</a>
      <a href="/nl-nl/plafonds/heartfelt/?utm=1#top">With query and fragment</a>
    </body>
    """
    links = discover_links(html, "https://example.com/nl-nl/", "https://example.com/nl-nl/")
    assert "https://example.com/nl-nl/plafonds/heartfelt/" in links
    assert not any(link.endswith(".pdf") for link in links)
    assert not any("en-eu" in link for link in links)
    assert not any("other-domain.com" in link for link in links)
    assert not any("#" in link for link in links)
    assert not any("mailto:" in link for link in links)
    # query-string variant of an already-included URL still resolves to the
    # same normalised (query-stripped) form
    assert links.count("https://example.com/nl-nl/plafonds/heartfelt/") == 2


def test_crawl_follows_links_breadth_first_within_prefix():
    pages = {
        "https://example.com/nl-nl/": '<body><a href="/nl-nl/a/">A</a><a href="/en-eu/x/">X</a></body>',
        "https://example.com/nl-nl/a/": '<body><a href="/nl-nl/b/">B</a></body>',
        "https://example.com/nl-nl/b/": "<body>leaf page</body>",
    }

    def fake_fetcher(url):
        html = pages.get(url)
        if html is None:
            return FetchResult(status_code=404, text=None, error="not found")
        return FetchResult(status_code=200, text=html, error=None)

    results = crawl("https://example.com/nl-nl/", fetcher=fake_fetcher, max_pages=10)
    urls = {r.url for r in results}
    assert urls == {
        "https://example.com/nl-nl/",
        "https://example.com/nl-nl/a/",
        "https://example.com/nl-nl/b/",
    }


def test_crawl_respects_max_pages():
    def fake_fetcher(url):
        # every page nests one level deeper under the crawl root (/nl-nl/)
        # and links to the next -- unbounded unless crawl caps itself.
        depth = url.rstrip("/").count("/") - 2  # path segments after the host
        html = f'<body><a href="{url}p{depth + 1}/">next</a></body>'
        return FetchResult(status_code=200, text=html, error=None)

    results = crawl("https://example.com/nl-nl/", fetcher=fake_fetcher, max_pages=3)
    assert len(results) == 3


def test_is_product_page_whitelists_known_product_sections_only():
    prefix = compute_allowed_prefix("https://example.com/nl-nl/")
    assert is_product_page("https://example.com/nl-nl/plafonds-nl/heartfelt/", prefix)
    assert is_product_page("https://example.com/nl-nl/wanden/derako/", prefix)
    assert is_product_page("https://example.com/nl-nl/product/luxalon-x/", prefix)
    # Real junk this filter exists to catch: a project case-study page
    # whose "product name" turned out to be the building itself.
    assert not is_product_page("https://example.com/nl-nl/project/de-alliantie-amersfoort/", prefix)
    assert not is_product_page("https://example.com/nl-nl/nieuwsbericht/some-news/", prefix)
    assert not is_product_page("https://example.com/nl-nl/brandgedrag/", prefix)
    assert not is_product_page("https://example.com/en-eu/plafonds-nl/heartfelt/", prefix)  # wrong locale


def test_crawl_skips_non_200_responses():
    def fake_fetcher(url):
        if url.endswith("/broken/"):
            return FetchResult(status_code=500, text=None, error="server error")
        return FetchResult(status_code=200, text='<body><a href="/nl-nl/broken/">bad</a></body>', error=None)

    results = crawl("https://example.com/nl-nl/", fetcher=fake_fetcher, max_pages=10)
    assert len(results) == 1
    assert results[0].url == "https://example.com/nl-nl/"
