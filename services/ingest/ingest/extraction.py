"""Stage 6: spec extraction, per the output contract in docs/INGESTION.md.

One Claude pass per document. The model is handed the controlled vocabulary
(never allowed to invent keys — CLAUDE.md invariant 7) and the chunk texts
with page numbers, and must return, per product it identifies, every
requested key either with a value or as presence='not_stated'. Structured
output is enforced with a forced tool call rather than free-text JSON, since
a dropped field here is a silent data gap the UI would wrongly show as "we
looked and it's not there" when actually the model just forgot to answer.
"""
from __future__ import annotations

from typing import Any

from anthropic import Anthropic

PARSER_VERSION = "1"
PROMPT_VERSION = "1"

_TOOL_NAME = "emit_products"

_SPEC_SCHEMA = {
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

_TOOL = {
    "name": _TOOL_NAME,
    "description": "Emit the products and cited specs found in this document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "canonical_name": {"type": "string"},
                        "manufacturer_ref": {"type": ["string", "null"]},
                        "category_code": {"type": ["string", "null"]},
                        "specs": {"type": "array", "items": _SPEC_SCHEMA},
                    },
                    "required": ["canonical_name", "specs"],
                },
            },
        },
        "required": ["products"],
    },
}


def build_prompt(
    *,
    spec_definitions: list[dict],
    category_codes: list[str],
    chunks: list[dict],
) -> str:
    vocab_lines = []
    for d in spec_definitions:
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

    return f"""You are extracting structured product specifications from a manufacturer
document for a building-products search engine used by engineers.

Categories available: {', '.join(category_codes)}

Controlled spec vocabulary — you may ONLY use these keys. Do not invent keys
outside this list; if a value doesn't map to one of these, omit it entirely:
{chr(10).join(vocab_lines)}

Rules:
1. One document often describes a product FAMILY. Emit one entry per distinct
   article/system code rather than collapsing them into one product.
2. For every spec key that plausibly applies to a product's category, include
   it in that product's specs array — either with a value and presence
   'stated' (or 'derived' if computed from other stated values), or with
   presence 'not_stated' and no value fields. Do not simply omit a key because
   the document is silent about it; 'not_stated' IS the answer in that case.
3. source_snippet must be copied VERBATIM from the chunk text, character for
   character — not paraphrased or reformatted. It must include the
   page_number it came from.
4. Only set presence 'stated' when the value is explicitly written in the
   document. Use 'derived' for values you computed from other stated values
   and say so in source_snippet.

Document chunks:

{chr(10).join(chunk_lines)}

Call {_TOOL_NAME} with every product you found."""


def extract_products(
    client: Anthropic,
    *,
    model: str,
    spec_definitions: list[dict],
    category_codes: list[str],
    chunks: list[dict],
) -> list[dict[str, Any]]:
    prompt = build_prompt(spec_definitions=spec_definitions, category_codes=category_codes, chunks=chunks)

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
