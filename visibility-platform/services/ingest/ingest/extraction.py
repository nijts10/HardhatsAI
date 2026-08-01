"""Claim extraction — one Claude pass per document version. The model is
handed the controlled spec_attributes vocabulary (it cannot invent keys)
and the chunk texts with page numbers, and must return, per product it
identifies within the given brand, every plausible key either with a
value or as presence='not_stated'.
"""
from __future__ import annotations

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
        "confidence": {"type": ["number", "null"]},
    },
    "required": ["key", "presence"],
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
                    },
                    "required": ["name", "claims"],
                },
            },
        },
        "required": ["products"],
    },
}


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

Controlled claim vocabulary — you may ONLY use these keys. Do not invent keys
outside this list; if a value doesn't map to one of these, omit it entirely:
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
   page_number it came from.
4. Only set presence 'stated' when the value is explicitly written in the
   document. Use 'derived' for values you computed from other stated values
   and say so in source_snippet.
5. Also emit any certifications found (CE, KOMO, BREEAM, DoP, EPD, FSC, ...)
   with their scheme, number, issuer, page and a verbatim snippet.

Document chunks:

{chr(10).join(chunk_lines)}

Call {_TOOL_NAME} with every product you found."""


def extract_claims(
    client: Anthropic, *, model: str, brand_name: str,
    spec_attributes: list[dict], chunks: list[dict],
) -> list[dict[str, Any]]:
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
            return block.input.get("products", [])
    return []
