"""Upsert products/claims/certifications and report what happened.
Verification (verification.py) decides whether a claim is trustworthy;
this module decides what happens to the database once it is.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import psycopg

from . import db
from .verification import snippet_in_page, verify_claim


def slugify(*parts: str | None) -> str:
    text = " ".join(p for p in parts if p).strip().lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "product"


@dataclass
class PersistStats:
    products_created_or_updated: int = 0
    variants_created_or_updated: int = 0
    claims_inserted: int = 0
    claims_superseded: int = 0
    claims_rejected: int = 0
    certifications_inserted: int = 0
    unmapped_findings_queued: int = 0
    unmapped_findings_rejected: int = 0
    rejections: list[str] = field(default_factory=list)
    # Warn-only: a brand-new product name that's suspiciously similar (pg_trgm)
    # to an existing product already in the same product_line. Never blocks
    # creation or auto-merges -- see db.find_similar_products_in_line's
    # docstring for why. Surfaced in extraction_runs.stats so it's visible
    # in the CLI output instead of requiring forensic SQL after the fact.
    possible_duplicate_products: list[dict] = field(default_factory=list)


def _chunk_id_for_page(chunks: list[dict], page_number: int | None) -> str | None:
    if page_number is None:
        return None
    for c in chunks:
        if c["page_start"] <= page_number <= c["page_end"]:
            return c["id"]
    return None


def _values_equal(existing: dict, normalized: dict) -> bool:
    for col in ("value_numeric", "value_numeric_max", "value_text", "value_bool", "value_enum", "presence"):
        if existing.get(col) != normalized.get(col):
            return False
    return True


def _upsert_product_line(conn: psycopg.Connection, *, brand_id: str, name: str | None) -> str | None:
    """Resolve (or create) the product_lines row for this brand -- the
    missing layer in brand -> line -> product -> product type -> spec (+
    data). None in, None out: not every document names a sub-line, and
    absence must stay "not yet known", never a guessed value (same
    three-state philosophy as everywhere else in this project)."""
    if not name:
        return None
    slug = slugify(name)
    existing = db.get_product_line_by_slug(conn, brand_id, slug)
    if existing:
        return existing["id"]
    return db.create_product_line(conn, brand_id=brand_id, name=name, slug=slug)


def _upsert_product(conn: psycopg.Connection, *, brand_id: str, category_id: str | None,
                     name: str, manufacturer_ref: str | None, product_line_name: str | None,
                     stats: PersistStats) -> str:
    # Look up by slug of NAME ONLY -- manufacturer_ref must never be part of
    # the identity key. Originally it was (see git history), to survive
    # casing/whitespace drift in manufacturer_ref between extraction calls
    # ("Rockwool" vs "ROCKWOOL"). But the same drift happens far more
    # destructively when a document simply doesn't STATE a manufacturer_ref
    # at all: observed for real across the HeartFelt line (2026-08-14) --
    # two documents both plainly say "HeartFelt® Origami" (identical name,
    # verified against the source PDFs) but one extraction call emitted
    # manufacturer_ref="Hunter Douglas Europe B.V." and the other left it
    # None, which produced two DIFFERENT slugs and therefore two DIFFERENT
    # product rows -- each holding only HALF the real product's ground
    # truth (claims split 38/5 across them), silently, with no error raised
    # anywhere. A per-document manufacturer_ref is optional supplementary
    # data, not part of what makes two mentions "the same product"; only
    # name (scoped to this brand's extraction) should decide that.
    slug = slugify(name)
    product_line_id = _upsert_product_line(conn, brand_id=brand_id, name=product_line_name)

    with conn.cursor() as cur:
        cur.execute("select id, manufacturer_ref, product_line_id from products where slug = %s", (slug,))
        existing = cur.fetchone()
        if existing:
            # Backfill manufacturer_ref if a later call supplies one and the
            # existing row doesn't have one yet -- never overwrite a value
            # that's already there with a possibly-worse one from this call.
            new_ref = manufacturer_ref if existing["manufacturer_ref"] is None else existing["manufacturer_ref"]
            cur.execute(
                "update products set name = %s, manufacturer_ref = %s where id = %s",
                (name, new_ref, existing["id"]),
            )
            # Same backfill rule for product_line_id -- fill it in if this
            # call identified one and the existing row doesn't have one yet.
            if existing["product_line_id"] is None and product_line_id is not None:
                db.set_product_line(conn, existing["id"], product_line_id)
            return existing["id"]

    # No exact-name match. If this product belongs to a known line, warn
    # (never block/auto-merge) when an existing product in that SAME line
    # has a suspiciously similar name -- this exact pattern (a genuinely
    # different name string for the same real product) has silently
    # fragmented one product into two rows three times now (2026-08-14,
    # 08-19, 09-07), each time only caught by a human diffing the catalog
    # after the fact. Surfacing it here doesn't fix the fragmentation, but
    # it means it no longer requires forensic SQL to notice.
    if product_line_id is not None:
        candidates = db.find_similar_products_in_line(
            conn, product_line_id=product_line_id, name=name,
        )
        for c in candidates:
            stats.possible_duplicate_products.append({
                "new_name": name, "existing_name": c["name"],
                "existing_id": str(c["id"]), "similarity": round(c["score"], 2),
            })

    return db.create_product(
        conn, brand_id=brand_id, category_id=category_id, name=name,
        slug=slug, manufacturer_ref=manufacturer_ref, product_line_id=product_line_id,
    )


def _upsert_variant(conn: psycopg.Connection, *, product_id: str, label: str,
                     manufacturer_ref: str | None) -> str:
    # Same slug-based lookup as _upsert_product and for the same reason —
    # avoid a raw-string dedup key that can drift in casing/whitespace
    # between extraction calls. Unlike products.slug, product_variants.slug
    # is only unique PER PRODUCT (see 0007_product_variants.sql) — "40mm"
    # is a near-certain label collision across different products.
    slug = slugify(label, manufacturer_ref)
    existing = db.get_variant_by_slug(conn, product_id, slug)
    if existing:
        db.update_variant_label(conn, existing["id"], label)
        return existing["id"]
    return db.create_variant(conn, product_id=product_id, label=label, slug=slug,
                              manufacturer_ref=manufacturer_ref)


def _persist_claims(
    conn: psycopg.Connection, *, claims: list[dict], product_id: str, variant_id: str | None,
    spec_attrs_by_key: dict[str, dict], document_chunks: list[dict], get_page_text,
    document_version_id: str, extraction_run_id: str, stats: PersistStats, label: str,
) -> None:
    for claim in claims:
        spec_attribute = spec_attrs_by_key.get(claim.get("key"))
        if spec_attribute is None:
            # The extractor cannot invent claim keys — anything that
            # doesn't map to spec_attributes is silently dropped.
            continue

        page_text = get_page_text(claim.get("page_number")) if claim.get("page_number") else None
        result = verify_claim(claim, spec_attribute, page_text)
        if not result.ok:
            stats.claims_rejected += 1
            stats.rejections.append(f"{label}/{claim.get('key')}: {result.rejection_reason}")
            continue

        normalized = result.normalized_claim
        row = {
            "product_id": product_id,
            "variant_id": variant_id,
            "spec_attribute_id": spec_attribute["id"],
            "value_numeric": normalized.get("value_numeric"),
            "value_numeric_max": normalized.get("value_numeric_max"),
            "value_text": normalized.get("value_text"),
            "value_bool": normalized.get("value_bool"),
            "value_enum": normalized.get("value_enum"),
            "unit": normalized.get("unit"),
            "presence": normalized["presence"],
            "confidence": normalized.get("confidence"),
            "document_version_id": document_version_id,
            "chunk_id": _chunk_id_for_page(document_chunks, normalized.get("page_number")),
            "page_number": normalized.get("page_number"),
            "source_snippet": normalized.get("source_snippet"),
            "derivation_note": normalized.get("derivation_note"),
            "extraction_run_id": extraction_run_id,
        }

        existing = db.get_current_claim(conn, product_id, spec_attribute["id"], variant_id=variant_id)
        if existing is None:
            db.insert_claim(conn, row, is_current=True)
            stats.claims_inserted += 1
        elif not _values_equal(existing, row):
            new_id = db.insert_claim(conn, row, is_current=False)
            db.supersede_claim(conn, existing["id"], new_id)
            db.set_claim_current(conn, new_id, True)
            stats.claims_inserted += 1
            stats.claims_superseded += 1
        # else: identical value already current, nothing to do.


def persist_extracted_products(
    conn: psycopg.Connection, *, brand_id: str, category_id: str | None,
    document_version_id: str, extraction_run_id: str, extracted_products: list[dict],
    spec_attrs_by_key: dict[str, dict], document_chunks: list[dict], get_page_text,
) -> PersistStats:
    stats = PersistStats()

    for product in extracted_products:
        product_id = _upsert_product(
            conn, brand_id=brand_id, category_id=category_id,
            name=product["name"], manufacturer_ref=product.get("manufacturer_ref"),
            product_line_name=product.get("product_line"), stats=stats,
        )
        stats.products_created_or_updated += 1

        _persist_claims(
            conn, claims=product.get("claims", []), product_id=product_id, variant_id=None,
            spec_attrs_by_key=spec_attrs_by_key, document_chunks=document_chunks,
            get_page_text=get_page_text, document_version_id=document_version_id,
            extraction_run_id=extraction_run_id, stats=stats, label=product["name"],
        )

        for variant in product.get("variants", []):
            variant_id = _upsert_variant(
                conn, product_id=product_id, label=variant["label"],
                manufacturer_ref=variant.get("manufacturer_ref"),
            )
            stats.variants_created_or_updated += 1

            _persist_claims(
                conn, claims=variant.get("claims", []), product_id=product_id, variant_id=variant_id,
                spec_attrs_by_key=spec_attrs_by_key, document_chunks=document_chunks,
                get_page_text=get_page_text, document_version_id=document_version_id,
                extraction_run_id=extraction_run_id, stats=stats,
                label=f"{product['name']}/{variant['label']}",
            )

        for cert in product.get("certifications", []):
            db.insert_certification(conn, {
                "product_id": product_id,
                "document_version_id": document_version_id,
                "scheme": cert["scheme"],
                "number": cert.get("number"),
                "issuer": cert.get("issuer"),
                "issued_on": None,
                "valid_until": None,
                "scope_text": None,
                "page_number": cert.get("page_number"),
                "source_snippet": cert.get("source_snippet"),
            })
            stats.certifications_inserted += 1

    return stats


def persist_unmapped_findings(
    conn: psycopg.Connection, *, document_version_id: str, findings: list[dict],
    get_page_text, stats: PersistStats,
) -> None:
    """Facts the extractor found but couldn't map to any spec_attributes
    key. Same anti-hallucination bar as a claim -- source_snippet must
    genuinely appear on the cited page -- but there's no spec_attribute to
    verify a value/type/plausibility against (that's the whole point: one
    doesn't exist yet), so this only does the citation check, via
    review_queue rather than claims. A finding that fails verification is
    dropped the same way an uncited claim is, not queued as unverifiable --
    the review queue is for "not yet defined", not "not yet grounded"."""
    for finding in findings:
        page_number = finding.get("page_number")
        snippet = finding.get("source_snippet")
        page_text = get_page_text(page_number) if page_number else None

        if not snippet or page_number is None or page_text is None or not snippet_in_page(snippet, page_text):
            stats.unmapped_findings_rejected += 1
            stats.rejections.append(
                f"unmapped/{finding.get('found_term')}: source_snippet not found verbatim on cited page"
            )
            continue

        db.insert_review_queue_entry(
            conn, found_term=finding["found_term"], source_snippet=snippet,
            document_version_id=document_version_id, page_number=page_number,
        )
        stats.unmapped_findings_queued += 1
