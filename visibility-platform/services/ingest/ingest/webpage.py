"""Ground truth's second real source, alongside uploaded PDFs: a brand's
own live website. Found while auditing Hunter Douglas -- several
"documentation gaps" the AI got right were genuinely, correctly published
on hunterdouglasarchitectural.eu, just never ingested, because the
pipeline only ever read PDFs.

Design choice, stated explicitly: rather than writing a second, parallel
chunking/extraction/persistence path for HTML, one page's content is
converted into the SAME parsing.ParsedPage shape parse_pdf() produces --
real HTML headings (h1-h6) become synthetic-font-size Line objects (see
_HEADING_FONT_SIZES), real <table> elements become parsing.Table objects
-- so chunking.build_chunks(), extraction.py, verification.py, and
persistence.py all run completely unchanged. Only the source-specific
step (bytes -> ParsedPage) is new code; everything downstream is the
exact same proven pipeline a PDF goes through, which is the whole point:
same anti-hallucination citation standard, not a second, weaker one.

Crawling is intentionally simple: breadth-first, same-domain, same
locale-path-prefix only (e.g. only /nl-nl/), no JS execution (same
known limitation already documented in page_visibility.py -- a
JS-rendered SPA page can under-read), capped at max_pages. Uses the same
caller-injectable Fetcher as http_fetch.py (crawlability.py/
page_visibility.py's own pattern) so tests never hit the real network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urldefrag

from bs4 import BeautifulSoup

from .http_fetch import Fetcher
from .parsing import Line, ParsedPage, Table

# Synthetic font sizes so build_chunks' existing font-size-ratio heading
# detection (HEADING_L1_RATIO=1.4, HEADING_L2_RATIO=1.15 against the page's
# median) works unchanged: median/body = 14, L1 threshold = 19.6, L2
# threshold = 16.1. build_chunks only tracks two heading tiers total, so
# h1 alone maps to L1 and h2/h3 both collapse to L2 (a real but acceptable
# simplification -- HTML's deeper h1-h6 nesting doesn't fully survive,
# same 2-tier limitation the PDF pipeline already has). h4-h6 fall back to
# plain body text.
_BODY_FONT_SIZE = 14.0
_HEADING_FONT_SIZES = {"h1": 24.0, "h2": 18.0, "h3": 17.0}

# Tags whose content is never real page content -- stripped before walking.
_BOILERPLATE_TAGS = ["script", "style", "nav", "header", "footer", "aside", "noscript", "form", "svg"]

MIN_PAGE_TEXT_CHARS = 200  # skip near-empty pages (pure image/listing pages) -- not worth an extraction call


@dataclass
class CrawlResult:
    url: str
    html: str


# Real, confirmed for Hunter Douglas's site by inspecting the URL path of
# every product a first full crawl actually created (2026-09-09): "project"
# case-study pages and generic info pages ("brandgedrag", "toepassing", ...)
# describe products in loose prose that never matches an existing product's
# exact name, and repeatedly created fresh near-duplicate products -- one
# even got a real BUILDING/PROJECT NAME as its "product name" ("De
# Alliantie", "Zwembad Abingdon"), not a product at all. A denylist of "bad"
# path segments is fragile against a topic page we haven't seen yet, so this
# is a WHITELIST instead: only these path segments are treated as real
# product-bearing pages. crawl() still follows links through non-whitelisted
# pages (a project page can legitimately link to a real product page we'd
# otherwise miss) -- only extraction is skipped for them, in
# is_product_page's caller.
PRODUCT_PATH_SEGMENTS = {"plafonds-nl", "wanden", "gevelbekleding", "zonwering-buiten", "product"}


def is_product_page(url: str, allowed_prefix: str) -> bool:
    """True only if the first path segment after allowed_prefix is a known
    product-bearing section. allowed_prefix is the same crawl-root prefix
    webpage.crawl() itself computes (e.g. https://example.com/nl-nl/)."""
    if not url.startswith(allowed_prefix):
        return False
    rest = url[len(allowed_prefix):]
    first_segment = rest.split("/", 1)[0]
    return first_segment in PRODUCT_PATH_SEGMENTS


def extract_title(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("title")
    text = tag.get_text(strip=True) if tag else None
    return text or None


def parse_webpage(html: str, url: str) -> ParsedPage:
    """One page = one logical ParsedPage, page_number always 1 -- a
    webpage has no page boundaries the way a PDF does."""
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in _BOILERPLATE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    body = soup.body or soup
    lines: list[Line] = []
    tables: list[Table] = []
    top = 0.0

    def walk(node) -> None:
        nonlocal top
        for child in getattr(node, "children", []):
            name = getattr(child, "name", None)
            if name is None:
                continue  # NavigableString handled via child.get_text() at leaf tags below
            if name == "table":
                rows = [
                    [cell.get_text(" ", strip=True) or None for cell in tr.find_all(["td", "th"])]
                    for tr in child.find_all("tr")
                ]
                rows = [r for r in rows if any(r)]
                if rows:
                    tables.append(Table(rows=rows, top=top, bottom=top + 1))
                    top += 2
                continue
            if name in _HEADING_FONT_SIZES:
                text = child.get_text(" ", strip=True)
                if text:
                    lines.append(Line(text=text, top=top, avg_font_size=_HEADING_FONT_SIZES[name]))
                    top += 1
                continue
            if name in ("p", "li", "td", "th", "figcaption", "dt", "dd") or name.startswith("h"):
                text = child.get_text(" ", strip=True)
                if text:
                    size = _HEADING_FONT_SIZES.get(name, _BODY_FONT_SIZE)
                    lines.append(Line(text=text, top=top, avg_font_size=size))
                    top += 1
                continue
            # Container elements (div, section, article, ul, ...): recurse
            # instead of taking get_text() directly, so nested headings/
            # tables/paragraphs inside them are still found individually.
            walk(child)

    walk(body)

    full_text = "\n".join(line.text for line in lines)
    return ParsedPage(
        page_number=1, text=full_text, layout=None, used_vision_fallback=False,
        tables=tables, lines=lines, median_font_size=_BODY_FONT_SIZE,
    )


def discover_links(html: str, page_url: str, allowed_prefix: str) -> list[str]:
    """Every internal link on the page that stays within allowed_prefix
    (same scheme+host+path-prefix) -- fragments and query strings dropped
    for dedup (this site's own content doesn't meaningfully vary by query
    string for our purposes; a false-dedup risk we accept for a bounded
    crawl, not a general-purpose crawler)."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        absolute, _ = urldefrag(absolute)
        parsed = urlparse(absolute)
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if normalized.startswith(allowed_prefix) and not normalized.lower().endswith(
            (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".doc", ".docx", ".xls", ".xlsx")
        ):
            found.append(normalized)
    return found


def compute_allowed_prefix(start_url: str) -> str:
    """scheme://host/first-path-segment/, e.g.
    https://www.example.com/nl-nl/x -> https://www.example.com/nl-nl/ --
    shared by crawl() (what to follow) and cli.py's is_product_page check
    (what to actually ingest), so both always agree on the crawl root."""
    parsed_start = urlparse(start_url)
    allowed_prefix = f"{parsed_start.scheme}://{parsed_start.netloc}{parsed_start.path}"
    if not allowed_prefix.endswith("/"):
        allowed_prefix += "/"
    return allowed_prefix


def crawl(start_url: str, *, fetcher: Fetcher, max_pages: int = 300) -> list[CrawlResult]:
    """Breadth-first, same-prefix only, stops at max_pages. allowed_prefix
    is derived from start_url itself (scheme://host/first-path-segment/),
    e.g. https://www.example.com/nl-nl/ -> only /nl-nl/... URLs are
    followed -- other locales/sections of the same site are out of scope,
    matching "only do the main website" (the nl-nl section)."""
    allowed_prefix = compute_allowed_prefix(start_url)
    start_normalized, _ = urldefrag(start_url)
    queue = [start_normalized]
    visited: set[str] = set()
    results: list[CrawlResult] = []

    while queue and len(results) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        result = fetcher(url)
        if result.status_code != 200 or not result.text:
            continue
        results.append(CrawlResult(url=url, html=result.text))

        for link in discover_links(result.text, url, allowed_prefix):
            if link not in visited and link not in queue:
                queue.append(link)

    return results
