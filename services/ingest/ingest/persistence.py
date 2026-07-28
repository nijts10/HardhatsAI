"""Stage 7 (second half): upsert products/specs and recompute completeness.

Verification (verification.py) decides whether a value is trustworthy; this
module decides what happens to the database once it is.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import psycopg

from . import db
from .verification import verify_spec


def slugify(*parts: str | None) -> str:
    text = " ".join(p for p in parts if p).strip().lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "product"


@dataclass
class PersistStats:
    products_created_or_updated: int = 0
    specs_inserted: int = 0
    specs_superseded: int = 0
    specs_rejected: int = 0
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


def persist_extracted_products(
    conn: psycopg.Connection,
    *,
    document_id: str,
    supplier_company_id: str,
    extraction_run_id: str,
    extracted_products: list[dict],
    spec_defs_by_key: dict[str, dict],
    category_by_code: dict[str, dict],
    document_chunks: list[dict],
    get_page_text,
) -> PersistStats:
    stats = PersistStats()

    for product in extracted_products:
        category = category_by_code.get(product.get("category_code") or "")
        slug = slugify(product.get("canonical_name"), product.get("manufacturer_ref"))
        product_id = db.upsert_product(
            conn,
            supplier_company_id=supplier_company_id,
            category_id=category["id"] if category else None,
            canonical_name=product["canonical_name"],
            slug=slug,
            manufacturer_ref=product.get("manufacturer_ref"),
            description=None,
            primary_document_id=document_id,
        )
        db.link_product_document(conn, product_id, document_id, relation="datasheet")
        stats.products_created_or_updated += 1

        for spec in product.get("specs", []):
            spec_def = spec_defs_by_key.get(spec.get("key"))
            if spec_def is None:
                # Invariant 7: the extractor cannot invent spec keys. Anything
                # that doesn't map to spec_definitions is silently dropped.
                continue

            page_text = get_page_text(spec.get("page_number")) if spec.get("page_number") else None
            result = verify_spec(spec, spec_def, page_text)
            if not result.ok:
                stats.specs_rejected += 1
                stats.rejections.append(f"{product['canonical_name']}/{spec.get('key')}: {result.rejection_reason}")
                continue

            normalized = result.normalized_spec
            row = {
                "product_id": product_id,
                "spec_definition_id": spec_def["id"],
                "value_numeric": normalized.get("value_numeric"),
                "value_numeric_max": normalized.get("value_numeric_max"),
                "value_text": normalized.get("value_text"),
                "value_bool": normalized.get("value_bool"),
                "value_enum": normalized.get("value_enum"),
                "unit": normalized.get("unit"),
                "presence": normalized["presence"],
                "confidence": normalized.get("confidence"),
                "document_id": document_id,
                "chunk_id": _chunk_id_for_page(document_chunks, normalized.get("page_number")),
                "page_number": normalized.get("page_number"),
                "source_snippet": normalized.get("source_snippet"),
                "source_bbox": None,
                "extraction_run_id": extraction_run_id,
            }

            existing = db.get_current_spec(conn, product_id, spec_def["id"])
            if existing is None:
                db.insert_spec(conn, row, is_current=True)
                stats.specs_inserted += 1
            elif not _values_equal(existing, row):
                new_id = db.insert_spec(conn, row, is_current=False)
                db.supersede_spec(conn, existing["id"], new_id)
                db.set_spec_current(conn, new_id, True)
                db.bump_product_version(conn, product_id)
                stats.specs_inserted += 1
                stats.specs_superseded += 1
            # else: identical value already current, nothing to do.

    db.recompute_data_completeness(conn, supplier_company_id)
    return stats
