from ingest.http_fetch import make_httpx_fetcher


def test_malformed_url_returns_a_clean_fetch_error_not_an_unhandled_exception():
    # brands.website / products.product_url are unconstrained free text --
    # a malformed value (here: a typo'd non-numeric port) raises httpx.
    # InvalidURL at URL-parse time, before any request is sent. InvalidURL
    # is NOT a subclass of httpx.HTTPError (confirmed against the
    # installed httpx version -- httpx.UnsupportedProtocol, the more
    # obvious "malformed URL" candidate, actually IS an HTTPError
    # subclass and was already handled; InvalidURL is the real gap), so a
    # fetcher that only catches HTTPError lets this escape and crash the
    # whole crawlability/page-visibility check instead of recording a
    # fetch error like any other unreachable URL.
    fetcher = make_httpx_fetcher()
    result = fetcher("https://example.com:notaport")

    assert result.status_code is None
    assert result.text is None
    assert result.error is not None
