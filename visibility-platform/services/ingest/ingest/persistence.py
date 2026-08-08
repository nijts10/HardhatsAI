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
from .verification import verify_claim


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
    rejections: list[str] = field(default_factory=list)


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


def _upsert_product(conn: psycopg.Connection, *, brand_id: str, category_id: str | None,
                     name: str, manufacturer_ref: str | None) -> str:
    # Look up by slug, not by a raw (name, manufacturer_ref) equality check.
    # The AI re-extracting the same document can return the same product
    # with slightly different capitalization/whitespace on manufacturer_ref
    # between calls (observed in practice: "Rockwool" vs "ROCKWOOL") -- an
    # exact-string lookup misses the existing row in that case, while
    # slugify() normalizes both to the same slug, so the fallback insert
    # then collides with products.slug's unique constraint. Matching on the
    # same normalized value the DB actually enforces uniqueness on avoids
    # that split entirely.
    slug = slugify(name, manufacturer_ref)
    with conn.cursor() as cur:
        cur.execute("select id from products where slug = %s", (slug,))
        existing = cur.fetchone()
        if existing:
            cur.execute("update products set name = %s where id = %s", (name, existing["id"]))
            return existing["id"]

    return db.create_product(
        conn, brand_id=brand_id, category_id=category_id, name=name,
        slug=slug, manufacturer_ref=manufacturer_ref,
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
