"""Part 5b: page-spec-visibility -- the brief's "highest-value diagnostic
in the whole product". Crawlability (crawlability.py) only asks whether a
crawler can fetch a page at all; this asks the next question: once
fetched, does the page's actual text contain the values ground truth
says are true? "alpha_w 0,85 exists only in a PDF/table-image, never as
page text" is exactly the finding this surfaces, and it directly explains
why an AI doesn't know a fact even when nothing blocks the crawl.

Only CURRENT claims with a real value are checked -- a 'not_stated' claim
has nothing to look for on the page, and 'boolean' claims are skipped
entirely: there's no single reliable literal-text form for a boolean fact
(unlike a number or a short enum/text value), so a text-presence check
would be guessing, not measuring. Both are documented skips, not silent
gaps -- PageVisibilityStats counts them.

Numeric matching deliberately does NOT reuse verification.value_digits_in_
snippet here, even though it's the same "does this value's digits appear"
idea: that function strips ALL non-digit characters from both sides,
which is calibrated for checking a short CLAIMED snippet (catching a
hallucinated digit swap) -- applied to an entire rendered page instead,
it's far too loose (a page's price, phone number, or an unrelated spec
could easily contain the same bare digit sequence by chance) and would
produce exactly the false "your page DOES show this" the brief warns
against. Instead this formats the actual literal value (plus its Dutch
comma-decimal variant, since this project's whole market is NL) and does
a real substring check via verification.snippet_in_page -- reused
directly for the normalisation/whitespace handling, just not for the
digit-stripping numeric comparison.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

from . import db
from .http_fetch import Fetcher
from .verification import normalize_text, snippet_in_page

_SCHEMA_ORG_ITEMTYPE_RE = re.compile(r"schema\.org/Product\b", re.I)


def _contains_product_type(node) -> bool:
    if isinstance(node, dict):
        type_value = node.get("@type")
        if type_value == "Product" or (isinstance(type_value, list) and "Product" in type_value):
            return True
        if "@graph" in node and _contains_product_type(node["@graph"]):
            return True
        return any(_contains_product_type(v) for v in node.values() if isinstance(v, (dict, list)))
    if isinstance(node, list):
        return any(_contains_product_type(item) for item in node)
    return False


def _schema_org_product_present(soup: BeautifulSoup) -> bool:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if _contains_product_type(data):
            return True
    return soup.find(attrs={"itemtype": _SCHEMA_ORG_ITEMTYPE_RE}) is not None


def _numeric_text_candidates(value: float) -> list[str]:
    base = str(int(value)) if value == int(value) else ("%g" % value)
    return list({base, base.replace(".", ",")})


def _numeric_candidate_found(candidate: str, visible_text: str) -> bool:
    """Word-boundary match, NOT verification.snippet_in_page's plain
    substring check -- a numeric candidate can be as short as a single
    digit (a legitimately zero-valued spec, e.g. recycled_content_pct=0),
    and a bare substring test against an entire rendered page would match
    almost anywhere a "0" appears (prices, phone numbers, dates, other
    specs), reporting a false "found" for every such claim. \\b boundaries
    on both ends fix this generally (not just for "0") since digits are
    word characters -- "0" cannot match inside "100" (no boundary between
    contiguous digits), only as a standalone token. Reuses verification.
    normalize_text for the same whitespace/unicode/dash handling
    snippet_in_page itself uses, just with a boundary-aware search instead
    of `in`."""
    pattern = re.compile(r"\b" + re.escape(normalize_text(candidate)) + r"\b")
    return pattern.search(normalize_text(visible_text)) is not None


def _value_found_in_text(claim: dict, visible_text: str) -> Optional[bool]:
    if claim["spec_data_type"] == "boolean":
        return None  # no reliable literal-text form for a boolean fact -- not checked, see module docstring
    if claim["spec_data_type"] in ("numeric", "numeric_range"):
        if claim.get("value_numeric") is None:
            return None
        candidates = _numeric_text_candidates(float(claim["value_numeric"]))
        return any(_numeric_candidate_found(c, visible_text) for c in candidates)
    value = claim.get("value_text") or claim.get("value_enum")
    if not value:
        return None
    return snippet_in_page(value, visible_text)


@dataclass
class PageVisibilityStats:
    products_checked: int = 0
    products_skipped_no_url: int = 0
    products_skipped_no_ground_truth: int = 0
    claims_checked: int = 0
    claims_found: int = 0
    claims_not_found: int = 0
    claims_undetermined: int = 0
    fetch_errors: list[str] = field(default_factory=list)


def run_page_visibility_check(conn, *, brand_id: str, fetcher: Fetcher) -> PageVisibilityStats:
    stats = PageVisibilityStats()

    for product in db.get_products_for_brand(conn, brand_id):
        current_claims = [c for c in db.get_current_claims_for_product(conn, product["id"])
                           if c["presence"] != "not_stated"]
        if not current_claims:
            stats.products_skipped_no_ground_truth += 1
            continue
        if not product.get("product_url"):
            stats.products_skipped_no_url += 1
            continue

        result = fetcher(product["product_url"])
        stats.products_checked += 1

        if result.error is not None or result.text is None:
            stats.fetch_errors.append(f"{product['name']}: {result.error or f'status {result.status_code}'}")
            for claim in current_claims:
                db.insert_page_spec_visibility(conn, {
                    "product_id": product["id"], "claim_id": claim["id"], "product_url": product["product_url"],
                    "http_status": result.status_code, "value_found_as_text": None,
                    "schema_org_product_present": None, "error": result.error,
                })
                stats.claims_checked += 1
                stats.claims_undetermined += 1
            continue

        soup = BeautifulSoup(result.text, "html.parser")
        schema_present = _schema_org_product_present(soup)  # must run BEFORE script tags are stripped below
        for tag in soup(["script", "style"]):
            tag.decompose()
        visible_text = soup.get_text(separator=" ")

        for claim in current_claims:
            found = _value_found_in_text(claim, visible_text)
            db.insert_page_spec_visibility(conn, {
                "product_id": product["id"], "claim_id": claim["id"], "product_url": product["product_url"],
                "http_status": result.status_code, "value_found_as_text": found,
                "schema_org_product_present": schema_present, "error": None,
            })
            stats.claims_checked += 1
            if found is True:
                stats.claims_found += 1
            elif found is False:
                stats.claims_not_found += 1
            else:
                stats.claims_undetermined += 1

    return stats
