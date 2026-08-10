"""Verification — the anti-hallucination mechanism, not a prompt
instruction. A claim the model marks 'stated' but that does not literally
appear on the cited page is discarded here, unconditionally. Same approach
as the HardhatsAI ingest worker's verification.py, adapted to this
project's spec_attributes vocabulary.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

PLAUSIBILITY_RANGES: dict[str, tuple[float, float]] = {
    "fire_resistance_ei": (0, 240),
    "airborne_sound_reduction_rw": (10, 80),
    "max_height_mm": (100, 20000),
    "thickness_mm": (0.1, 1000),
}

_UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    ("cm", "mm"): 10.0,
    ("m", "mm"): 1000.0,
}


@dataclass
class VerificationResult:
    ok: bool
    normalized_claim: dict | None
    rejection_reason: str | None


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.replace(" ", " ").replace("‑", "-").replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s)
    return s.strip().casefold()


def snippet_in_page(snippet: str, page_text: str) -> bool:
    return normalize_text(snippet) in normalize_text(page_text)


def digits_of(value: float) -> str:
    return re.sub(r"[^0-9]", "", ("%g" % value))


def value_digits_in_snippet(value: float, snippet: str) -> bool:
    digits = digits_of(value)
    if not digits:
        return True
    return digits in re.sub(r"[^0-9]", "", snippet)


def convert_unit(value: float, from_unit: str | None, to_unit: str | None) -> tuple[float, str | None]:
    """Public -- also reused by claims_diff.py (Part 4) to normalise an AI
    answer's claimed unit to spec_attributes.unit before comparing against
    ground truth, same conversion table as document-claim verification."""
    if not from_unit or not to_unit or from_unit.strip().lower() == to_unit.strip().lower():
        return value, to_unit
    factor = _UNIT_CONVERSIONS.get((from_unit.strip().lower(), to_unit.strip().lower()))
    if factor is None:
        return value, from_unit
    return value * factor, to_unit


def _check_plausibility(key: str, value: float | None, value_max: float | None) -> str | None:
    bounds = PLAUSIBILITY_RANGES.get(key)
    if bounds is None or value is None:
        return None
    lo, hi = bounds
    for v in (value, value_max):
        if v is not None and not (lo <= v <= hi):
            return f"{key}={v} outside plausibility range [{lo}, {hi}]"
    return None


def verify_claim(claim: dict, spec_attribute: dict, page_text: str | None) -> VerificationResult:
    """claim: one entry from extraction.extract_claims()['claims'], already
    matched to its spec_attributes row (spec_attribute)."""
    presence = claim.get("presence")

    if presence == "not_stated":
        return VerificationResult(True, {**claim, "value_numeric": None, "value_numeric_max": None,
                                          "value_text": None, "value_bool": None, "value_enum": None,
                                          "unit": None, "derivation_note": None}, None)

    snippet = claim.get("source_snippet")
    page_number = claim.get("page_number")

    # 'derived' gets the SAME citation requirement as 'stated', not a free
    # pass. A derived value is computed from other stated facts, but
    # source_snippet must still be a pure verbatim quote of those
    # underlying facts -- the model's own reasoning for the derivation
    # belongs in derivation_note (checked below, deliberately NOT
    # verbatim-checked since it's the model's own words), never mixed into
    # source_snippet. Before this, a 'derived' claim skipped this whole
    # block and could carry any snippet -- or none -- and still be
    # accepted; that's the gap this closes.
    if presence in ("stated", "derived"):
        if not snippet or page_number is None:
            return VerificationResult(False, None, f"{presence} claim missing source_snippet or page_number")
        if page_text is None:
            return VerificationResult(False, None, f"no page text available for page {page_number} to verify against")
        if not snippet_in_page(snippet, page_text):
            return VerificationResult(False, None, "source_snippet not found verbatim on cited page")

    normalized = dict(claim)

    if claim.get("value_numeric") is not None:
        if presence == "stated" and not value_digits_in_snippet(claim["value_numeric"], snippet or ""):
            return VerificationResult(False, None, "numeric value's digits do not appear in source_snippet")

        value, unit = convert_unit(claim["value_numeric"], claim.get("unit"), spec_attribute.get("unit"))
        value_max, _ = convert_unit(claim["value_numeric_max"], claim.get("unit"), spec_attribute.get("unit")) \
            if claim.get("value_numeric_max") is not None else (None, unit)

        reason = _check_plausibility(spec_attribute["key"], value, value_max)
        if reason:
            return VerificationResult(False, None, reason)

        normalized["value_numeric"] = value
        normalized["value_numeric_max"] = value_max
        normalized["unit"] = unit

    return VerificationResult(True, normalized, None)
