"""Spec-diff (MVP brief Part 4) -- the platform's core differentiator.
An AI's answer is "content"; a demonstrated wrong number next to the
right one, both cited, is evidence.

Two passes per visibility_answers row:

  1. extract_answer_claims() -- one Claude tool-use call per answer,
     scoped to the run's category vocabulary (reuses
     extraction.spec_attributes_for_category(), the same scoping
     pipeline.py uses for documents). Reuses verification.py's
     snippet_in_page/value_digits_in_snippet DIRECTLY -- an answer's
     response_text is this pass's "page text": a claim whose
     source_quote isn't a verbatim substring of response_text is
     discarded, exactly like an unverifiable document claim would be,
     never inserted. A spec_key outside spec_attributes is silently
     dropped against the FULL vocabulary (not the category-scoped one),
     same rule and same reasoning as persistence.py's _persist_claims:
     the scoping only narrows what got ASKED about, not what's
     acceptable to have been answered.

  2. diff_claim() -- fuzzy-resolves product_name against `products` via
     pg_trgm (global, not scoped to the audited brand -- a competitor's
     product lives under a different brand_id and resolving it is the
     point), then compares the extracted value against the CURRENT
     ground-truth claims row for that (product, spec_attribute), within
     spec_attributes.comparison_tolerance, normalising units first via
     verification.convert_unit. A claim that doesn't match ground truth
     but DOES match one of spec_attribute.confusable_with's ground-truth
     values is 'likely_confusion', not a plain miss -- the "AI mixed up
     Rw and Dn,f,w" narrative the brief calls out explicitly. No ground
     truth at all (no row, or a 'not_stated' row) is 'unverifiable_spec'
     and must never count as an error -- it's the documentation-gap
     upsell, not a strike against the AI's answer.

Comparison is only ever against the PRODUCT-level current claim
(variant_id is null) -- an AI answer essentially never states which
variant of a product it means, so there is nothing to resolve a variant
claim against; this is a deliberate MVP simplification, not a silent gap.

Like visibility.py, the extraction call itself is injected as a plain
callable (`caller`) rather than a bound Anthropic client, so tests can
script exact model output without touching the network -- see
make_anthropic_extractor() for the real implementation cli.py wires in.

Resumable per answer, not per claim: an answer that already has ANY
answer_claims rows is treated as fully processed and skipped entirely on
a re-run (mirrors visibility_answers' own per-(prompt,replicate)
resumability, just at answer granularity instead -- there's no partial
extraction state worth preserving mid-answer the way there is
mid-benchmark-run).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from . import db
from .extraction import coerce_array, spec_attributes_for_category
from .verification import convert_unit, normalize_text, snippet_in_page, value_digits_in_snippet

RESOLUTION_THRESHOLD = 0.45

_TOOL_NAME = "emit_answer_claims"

_ANSWER_CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "brand_name": {"type": "string"},
        "product_name": {"type": "string"},
        "spec_key": {"type": "string"},
        "value_numeric": {"type": ["number", "null"]},
        "value_numeric_max": {"type": ["number", "null"]},
        "value_text": {"type": ["string", "null"]},
        "value_bool": {"type": ["boolean", "null"]},
        "value_enum": {"type": ["string", "null"]},
        "unit": {"type": ["string", "null"]},
        "source_quote": {"type": "string"},
    },
    "required": ["brand_name", "product_name", "spec_key", "source_quote"],
}

_TOOL = {
    "name": _TOOL_NAME,
    "description": "Emit every technical spec claim made about a specific named product in this AI answer.",
    "input_schema": {
        "type": "object",
        "properties": {"claims": {"type": "array", "items": _ANSWER_CLAIM_SCHEMA}},
        "required": ["claims"],
    },
}

# prompt_text -> raw claim dicts matching _ANSWER_CLAIM_SCHEMA. The real
# implementation (make_anthropic_extractor) makes the API call; tests
# script this directly, same pattern as visibility.VisibilityCaller.
AnswerClaimCaller = Callable[[str], list[dict]]


def build_prompt(*, response_text: str, spec_attributes: list[dict]) -> str:
    vocab_lines = []
    for d in spec_attributes:
        enum_note = f" one of {d['enum_values']}" if d.get("enum_values") else ""
        vocab_lines.append(
            f"- {d['key']} ({d['data_type']}{', unit ' + d['unit'] if d.get('unit') else ''}):"
            f" {d['name_en']} / {d['name_nl']}.{enum_note}"
        )

    return f"""You are auditing an AI product-visibility answer for factual, checkable
technical claims about named products.

Controlled spec vocabulary -- you may ONLY use these keys for anything you
are confident is a real spec being claimed:
{chr(10).join(vocab_lines)}

Rules:
1. Only extract a claim when the answer names a SPECIFIC product (a
   model/article name or code, not just a brand or generic category) AND
   states a specific value for one of the vocabulary keys above.
2. Extract claims for EVERY brand mentioned, not just the main one being
   discussed -- competitor products matter as much as the primary one.
3. source_quote must be copied VERBATIM from the answer text below,
   character for character -- not paraphrased or reformatted. A claim
   whose quote does not literally appear in the text will be discarded.
4. Do not invent a new key for a claim that doesn't match the vocabulary
   above -- skip it instead of guessing the closest key.
5. One entry per distinct (product, spec) claim; if the same fact is
   repeated, extract it once.

Answer text to audit:

{response_text}

Call {_TOOL_NAME} with every claim you found."""


@dataclass
class ExtractedAnswerClaim:
    brand_name: str
    product_name: str
    spec_key: str
    source_quote: str
    value_numeric: Optional[float] = None
    value_numeric_max: Optional[float] = None
    value_text: Optional[str] = None
    value_bool: Optional[bool] = None
    value_enum: Optional[str] = None
    unit: Optional[str] = None


def make_anthropic_extractor(client, model: str) -> AnswerClaimCaller:
    def _call(prompt_text: str) -> list[dict]:
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            messages=[{"role": "user", "content": prompt_text}],
        )
        for block in message.content:
            if block.type == "tool_use" and block.name == _TOOL_NAME:
                # Same double-encoding failure mode extraction.py's
                # coerce_array guards against (Claude occasionally returns
                # an array field as a JSON string instead of structured
                # content) -- without this, that case iterates as
                # characters below and kills the whole extraction pass.
                return coerce_array(block.input.get("claims", []), wrapper_key="claims")
        return []

    return _call


def extract_answer_claims(
    caller: AnswerClaimCaller, *, response_text: str, spec_attributes: list[dict],
) -> list[ExtractedAnswerClaim]:
    if not response_text.strip():
        return []
    prompt = build_prompt(response_text=response_text, spec_attributes=spec_attributes)
    raw = caller(prompt)
    return [
        ExtractedAnswerClaim(
            brand_name=c["brand_name"], product_name=c["product_name"], spec_key=c["spec_key"],
            source_quote=c["source_quote"], value_numeric=c.get("value_numeric"),
            value_numeric_max=c.get("value_numeric_max"), value_text=c.get("value_text"),
            value_bool=c.get("value_bool"), value_enum=c.get("value_enum"), unit=c.get("unit"),
        )
        for c in raw
    ]


def _is_own_brand(claimed_brand: str, audited_brand: str) -> bool:
    a, b = normalize_text(claimed_brand), normalize_text(audited_brand)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def _numeric_match(spec_attribute: dict, claim: ExtractedAnswerClaim, ground_truth: dict) -> bool:
    if claim.value_numeric is None or ground_truth.get("value_numeric") is None:
        return False
    converted, _ = convert_unit(claim.value_numeric, claim.unit, spec_attribute.get("unit"))
    tolerance = float(spec_attribute.get("comparison_tolerance") or 0)
    lo = float(ground_truth["value_numeric"])
    hi = float(ground_truth.get("value_numeric_max") or ground_truth["value_numeric"])
    return (lo - tolerance) <= converted <= (hi + tolerance)


def _values_match(spec_attribute: dict, claim: ExtractedAnswerClaim, ground_truth: dict) -> bool:
    data_type = spec_attribute["data_type"]
    if data_type in ("numeric", "numeric_range"):
        return _numeric_match(spec_attribute, claim, ground_truth)
    if data_type == "boolean":
        return claim.value_bool is not None and claim.value_bool == ground_truth.get("value_bool")
    if data_type == "enum":
        return bool(claim.value_enum) and normalize_text(claim.value_enum) == normalize_text(
            ground_truth.get("value_enum") or ""
        )
    return bool(claim.value_text) and normalize_text(claim.value_text) == normalize_text(
        ground_truth.get("value_text") or ""
    )


@dataclass
class DiffResult:
    verdict: str
    compared_claim_id: Optional[str]
    confusable_key: Optional[str]


def diff_claim(
    conn, *, product_id: str, spec_attribute: dict, claim: ExtractedAnswerClaim,
    spec_attrs_by_key: dict[str, dict],
) -> DiffResult:
    ground_truth = db.get_current_claim(conn, product_id, spec_attribute["id"])
    if ground_truth is None or ground_truth["presence"] == "not_stated":
        return DiffResult("unverifiable_spec", ground_truth["id"] if ground_truth else None, None)

    if _values_match(spec_attribute, claim, ground_truth):
        return DiffResult("correct", ground_truth["id"], None)

    for other_key in spec_attribute.get("confusable_with") or []:
        other_attr = spec_attrs_by_key.get(other_key)
        if other_attr is None:
            continue
        other_truth = db.get_current_claim(conn, product_id, other_attr["id"])
        if other_truth is None or other_truth["presence"] == "not_stated":
            continue
        if _values_match(other_attr, claim, other_truth):
            return DiffResult("likely_confusion", other_truth["id"], other_key)

    return DiffResult("incorrect", ground_truth["id"], None)


def _row_values(claim: ExtractedAnswerClaim, spec_attribute: dict) -> Optional[dict]:
    """The value/unit columns to persist, normalised to spec_attribute's
    canonical unit -- so answer_claims stores comparable values the same
    way claims does, not whatever unit the AI happened to phrase it in.

    Dispatches strictly on spec_attribute.data_type and nulls out every
    OTHER value_* field, rather than passing all of the extracted claim's
    fields straight through -- answer_claims_value_ck requires exactly one
    of value_numeric/value_text/value_bool/value_enum to be non-null, but
    _ANSWER_CLAIM_SCHEMA declares them as independent optional properties
    (no cross-provider way to express "exactly one of" in the tool
    schema). Without this, a tool call that populates more than one field
    -- e.g. value_numeric plus a stray value_text restating the unit --
    would reach db.insert_answer_claim and trip the check constraint,
    aborting the entire extraction run over one imprecise LLM response
    instead of just that one claim.

    Returns None -- meaning "reject this claim, don't insert it" -- when
    the field matching spec_attribute.data_type wasn't actually populated
    (e.g. the tool call tagged a numeric spec but only filled value_text).
    Nulling every OTHER field in that case would still leave all four
    null, which trips the same check constraint from the opposite
    direction; a value/type mismatch is exactly as unusable as no value
    at all, so it's rejected the same way."""
    data_type = spec_attribute["data_type"]
    unit = spec_attribute.get("unit")

    if data_type in ("numeric", "numeric_range"):
        if claim.value_numeric is None:
            return None
        value_numeric, unit = convert_unit(claim.value_numeric, claim.unit, unit)
        value_numeric_max = None
        if claim.value_numeric_max is not None:
            value_numeric_max, _ = convert_unit(claim.value_numeric_max, claim.unit, unit)
            # answer_claims_range_ck requires value_numeric_max >= value_numeric.
            # Observed in practice: the model uses value_numeric/value_numeric_max
            # to express two alternate values for different variants ("αw = 1.00
            # for black, or 0.95 for other colours"), not a true min/max range,
            # and doesn't order them -- that crashed the insert instead of being
            # caught here like every other malformed claim. Not safe to guess
            # which of the two the model actually meant as the "real" value, so
            # drop the invalid max and keep value_numeric as a single point
            # value rather than reject the whole claim.
            if value_numeric_max < value_numeric:
                value_numeric_max = None
        return {"value_numeric": value_numeric, "value_numeric_max": value_numeric_max,
                "value_text": None, "value_bool": None, "value_enum": None, "unit": unit}
    if data_type == "boolean":
        if claim.value_bool is None:
            return None
        return {"value_numeric": None, "value_numeric_max": None, "value_text": None,
                "value_bool": claim.value_bool, "value_enum": None, "unit": unit}
    if data_type == "enum":
        if not claim.value_enum:
            return None
        return {"value_numeric": None, "value_numeric_max": None, "value_text": None,
                "value_bool": None, "value_enum": claim.value_enum, "unit": unit}
    if not claim.value_text:
        return None
    return {"value_numeric": None, "value_numeric_max": None, "value_text": claim.value_text,
            "value_bool": None, "value_enum": None, "unit": unit}


@dataclass
class ClaimsExtractionStats:
    answers_processed: int = 0
    answers_skipped_already_done: int = 0
    claims_extracted: int = 0
    claims_rejected_unverifiable_quote: int = 0
    claims_rejected_unknown_spec: int = 0
    claims_rejected_value_type_mismatch: int = 0
    verdict_counts: dict = field(default_factory=dict)


def run_claims_extraction(
    conn, *, run_id: str, caller: AnswerClaimCaller, commit_each_answer: bool = False,
) -> ClaimsExtractionStats:
    """commit_each_answer: when True, commits after each answer's claims
    are fully inserted, so has_answer_claims()-based resumability is
    durable across a process crash, not just within one open connection --
    without it, NOTHING is committed until the caller commits after the
    whole run finishes (cli.py previously did exactly that), so a failure
    partway through discarded every already-extracted answer's claims,
    forcing a full re-send to the LLM on retry. Defaults to False so
    existing callers/tests that manage their own transaction boundaries
    (in particular this test suite's rollback-based isolation, see
    tests/conftest.py) are unaffected -- cli.py's real `claims extract`
    command passes True."""
    run = db.get_visibility_run(conn, run_id)
    if run is None:
        raise ValueError(f"visibility_runs {run_id} not found")

    spec_attributes = db.get_spec_attributes(conn)
    spec_attrs_by_key = {d["key"]: d for d in spec_attributes}
    category_codes = db.get_category_ancestor_codes(conn, run["category_id"])
    prompt_spec_attributes = spec_attributes_for_category(spec_attributes, category_codes)

    stats = ClaimsExtractionStats()

    for answer in db.get_visibility_answers_for_run(conn, run_id):
        if db.has_answer_claims(conn, answer["id"]):
            stats.answers_skipped_already_done += 1
            continue

        extracted = extract_answer_claims(
            caller, response_text=answer["response_text"], spec_attributes=prompt_spec_attributes,
        )
        stats.answers_processed += 1

        for claim in extracted:
            if not snippet_in_page(claim.source_quote, answer["response_text"]):
                stats.claims_rejected_unverifiable_quote += 1
                continue
            if claim.value_numeric is not None and not value_digits_in_snippet(claim.value_numeric, claim.source_quote):
                stats.claims_rejected_unverifiable_quote += 1
                continue

            spec_attribute = spec_attrs_by_key.get(claim.spec_key)
            if spec_attribute is None:
                # The extractor cannot invent spec keys -- silently dropped,
                # same rule as persistence.py's _persist_claims. Part 4's
                # brief doesn't specify a review_queue equivalent for
                # answer-side unmapped findings, so there's nowhere to
                # queue this for triage yet -- just a dropped count.
                stats.claims_rejected_unknown_spec += 1
                continue

            values = _row_values(claim, spec_attribute)
            if values is None:
                # The tool call tagged this spec_key but didn't actually
                # populate the value_* field matching its data_type (or
                # populated more than one) -- can't be stored without
                # tripping answer_claims_value_ck either way, so it's
                # dropped exactly like an unknown spec key rather than
                # risking a half-guessed value.
                stats.claims_rejected_value_type_mismatch += 1
                continue

            is_competitor = not _is_own_brand(claim.brand_name, run["brand_name"])
            match = db.resolve_product_by_name(conn, claim.product_name)
            row = {
                "answer_id": answer["id"], "brand_name": claim.brand_name, "product_name": claim.product_name,
                "is_competitor": is_competitor, "spec_attribute_id": spec_attribute["id"],
                "source_quote": claim.source_quote, **values,
            }

            if match is None or float(match["score"]) < RESOLUTION_THRESHOLD:
                row.update(
                    resolved_product_id=None, resolution_score=float(match["score"]) if match else None,
                    verdict="unverifiable_product", compared_claim_id=None, confusable_key=None,
                )
            else:
                diff = diff_claim(
                    conn, product_id=match["id"], spec_attribute=spec_attribute, claim=claim,
                    spec_attrs_by_key=spec_attrs_by_key,
                )
                row.update(
                    resolved_product_id=match["id"], resolution_score=float(match["score"]),
                    verdict=diff.verdict, compared_claim_id=diff.compared_claim_id,
                    confusable_key=diff.confusable_key,
                )

            db.insert_answer_claim(conn, row)
            stats.claims_extracted += 1
            stats.verdict_counts[row["verdict"]] = stats.verdict_counts.get(row["verdict"], 0) + 1

        if commit_each_answer:
            conn.commit()

    return stats
