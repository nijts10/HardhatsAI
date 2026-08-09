"""Claim extraction — one Claude pass per document version. The model is
handed the controlled spec_attributes vocabulary (it cannot invent keys)
and the chunk texts with page numbers, and must return, per product it
identifies within the given brand, every plausible key either with a
value or as presence='not_stated'.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

PARSER_VERSION = "1"
PROMPT_VERSION = "1"

_TOOL_NAME = "emit_products"

_CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string"},
        "value_numeric": {"type": ["number", "null"]},
        "value_numeric_max": {"type": ["number", "null"]},
        "value_text": {"type": ["string", "null"]},
        "value_bool": {"type": ["boolean", "null"]},
        "value_enum": {"type": ["string", "null"]},
        "unit": {"type": ["string", "null"]},
        "presence": {"type": "string", "enum": ["stated", "derived", "not_stated"]},
        "page_number": {"type": ["integer", "null"]},
        "source_snippet": {"type": ["string", "null"]},
        "derivation_note": {"type": ["string", "null"]},
        "confidence": {"type": ["number", "null"]},
    },
    "required": ["key", "presence"],
}

_VARIANT_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "manufacturer_ref": {"type": ["string", "null"]},
        "claims": {"type": "array", "items": _CLAIM_SCHEMA},
    },
    "required": ["label", "claims"],
}

_CERTIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "scheme": {"type": "string"},
        "number": {"type": ["string", "null"]},
        "issuer": {"type": ["string", "null"]},
        "page_number": {"type": ["integer", "null"]},
        "source_snippet": {"type": ["string", "null"]},
    },
    "required": ["scheme"],
}

# A fact the model finds that doesn't map to any spec_attributes key.
# Document-level, not per-product -- see 0011_review_queue.sql. Feeds
# review_queue, never claims, so it structurally can't affect coverage.
_UNMAPPED_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "found_term": {"type": "string"},
        "page_number": {"type": "integer"},
        "source_snippet": {"type": "string"},
    },
    "required": ["found_term", "page_number", "source_snippet"],
}

_TOOL = {
    "name": _TOOL_NAME,
    "description": "Emit the products, claims, and certifications found in this document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "manufacturer_ref": {"type": ["string", "null"]},
                        "claims": {"type": "array", "items": _CLAIM_SCHEMA},
                        "certifications": {"type": "array", "items": _CERTIFICATION_SCHEMA},
                        "variants": {"type": "array", "items": _VARIANT_SCHEMA},
                    },
                    "required": ["name", "claims"],
                },
            },
            "unmapped_findings": {"type": "array", "items": _UNMAPPED_FINDING_SCHEMA},
        },
        "required": ["products"],
    },
}


@dataclass
class ExtractionResult:
    products: list[dict[str, Any]]
    unmapped_findings: list[dict[str, Any]]


def spec_attributes_for_category(spec_attributes: list[dict], applicable_codes: list[str] | None) -> list[dict]:
    """Scope the vocabulary handed to the model to what's actually relevant
    to this product's category, instead of asking about all 12+ rows on
    every document regardless of what kind of product it is.

    applicable_codes is the product's own category code PLUS every
    ancestor's (see db.get_category_ancestor_codes /
    category_and_ancestor_codes() in 0006_category_hierarchy.sql) -- a spec
    tagged at a broad parent category (e.g. "mineral wool") is relevant to
    every descendant (e.g. "rock wool") without needing to be re-listed on
    each one. An empty spec_attribute.category_codes means "universal"
    (applies everywhere); a product with no category assigned yet only
    gets the universal rows -- we can't yet judge relevance of
    category-scoped ones for it, so we correctly never ask about them (a
    "no row" / not-yet-evaluated state) rather than guessing. Mirrors the
    same filter report.py uses for coverage."""
    codes = set(applicable_codes or [])
    return [
        d for d in spec_attributes
        if not d.get("category_codes") or codes & set(d["category_codes"])
    ]


def build_prompt(*, brand_name: str, spec_attributes: list[dict], chunks: list[dict]) -> str:
    vocab_lines = []
    for d in spec_attributes:
        enum_note = f" one of {d['enum_values']}" if d.get("enum_values") else ""
        vocab_lines.append(
            f"- {d['key']} ({d['data_type']}{', unit ' + d['unit'] if d.get('unit') else ''}):"
            f" {d['name_en']} / {d['name_nl']}.{enum_note}"
        )

    chunk_lines = []
    for c in chunks:
        heading = " > ".join(c.get("heading_path") or [])
        chunk_lines.append(
            f"[chunk {c['id']} | pages {c['page_start']}-{c['page_end']}"
            f"{' | ' + heading if heading else ''}]\n{c['content']}"
        )

    return f"""You are extracting structured product claims from a manufacturer
document for the brand "{brand_name}", for an AI-visibility auditing platform.

Controlled claim vocabulary — you may ONLY use these keys for anything you
are confident is a real spec:
{chr(10).join(vocab_lines)}

Rules:
1. One document often describes a product FAMILY belonging to "{brand_name}".
   Emit one entry per distinct article/system code rather than collapsing
   them into one product.
2. For every claim key that plausibly applies to a product, include it in
   that product's claims array — either with a value and presence 'stated'
   (or 'derived' if computed from other stated values), or with presence
   'not_stated' and no value fields. Do not simply omit a key because the
   document is silent about it; 'not_stated' IS the answer in that case.
3. source_snippet must be copied VERBATIM from the chunk text, character for
   character — not paraphrased or reformatted. It must include the
   page_number it came from. This applies to 'derived' claims too, not
   just 'stated' ones — see rule 4.
4. Only set presence 'stated' when the value is explicitly written in the
   document. Use 'derived' when you compute a value from other stated
   facts instead — but source_snippet must STILL be a pure verbatim quote
   of the underlying stated text you derived from (same rule as #3), never
   your own reasoning or wording. Put your reasoning for the derivation in
   derivation_note instead — that field is your own words explaining the
   inference and is NOT required to be verbatim. Never write things like
   "(derived from X, corresponding to Y)" inside source_snippet — that
   sentence fragment doesn't literally appear in the document and will be
   rejected; it belongs in derivation_note.
5. Also emit any certifications found (CE, KOMO, BREEAM, DoP, EPD, FSC, ...)
   with their scheme, number, issuer, page and a verbatim snippet.
6. Do not invent a new key for a fact that doesn't match the vocabulary
   above. Instead, flag it for human review: add an entry to the
   top-level unmapped_findings array with a short found_term label, the
   real page_number, and a verbatim source_snippet (same verbatim rule as
   #3) for that specific fact. One entry per distinct fact — do not
   combine several into one.
7. If the document gives DIFFERENT measured values for different variants
   of the same product (e.g. performance that changes by thickness), do
   NOT force one value onto the product or silently pick one. Emit a
   "variants" array on that product, one entry per distinct variant (a
   short label like "40mm", plus its own claims for the specs that differ
   by variant). Keep specs that are the SAME across every variant at the
   product level (in the product's own "claims"), not repeated under each
   variant — only put a spec under a variant when its value genuinely
   differs from variant to variant.

Document chunks:

{chr(10).join(chunk_lines)}

Call {_TOOL_NAME} with every product you found."""


def extract_claims(
    client: Anthropic, *, model: str, brand_name: str,
    spec_attributes: list[dict], chunks: list[dict],
) -> ExtractionResult:
    prompt = build_prompt(brand_name=brand_name, spec_attributes=spec_attributes, chunks=chunks)

    message = client.messages.create(
        model=model,
        max_tokens=8192,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            return ExtractionResult(
                products=_coerce_array(block.input.get("products", []), wrapper_key="products"),
                unmapped_findings=_coerce_array(
                    block.input.get("unmapped_findings", []), wrapper_key="unmapped_findings"
                ),
            )
    return ExtractionResult(products=[], unmapped_findings=[])


def _coerce_array(value: Any, *, wrapper_key: str) -> list[dict[str, Any]]:
    """Observed in the wild: the model sometimes double-encodes an array
    field, returning a JSON string (of either the bare array or the whole
    wrapping object) instead of structured content matching the declared
    tool schema. Parse it back rather than let a string reach persistence,
    where iterating over it yields characters, not dicts."""
    if isinstance(value, str):
        parsed = json.loads(value)
        value = parsed.get(wrapper_key, parsed) if isinstance(parsed, dict) else parsed
    return value
