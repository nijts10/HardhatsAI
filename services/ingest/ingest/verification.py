"""Stage 7: verification, per docs/INGESTION.md.

This is the anti-hallucination mechanism (CLAUDE.md invariant 1), not a
prompt instruction — a value the model claims is 'stated' but that does not
literally appear on the cited page is discarded here, unconditionally.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# (min, max) sanity ranges per spec key, in the key's canonical unit. Catches
# the classic misread (a lambda of "35" meant as 0.035 W/mK). Keys not listed
# here get no range check — text/enum/boolean specs don't need one.
PLAUSIBILITY_RANGES: dict[str, tuple[float, float]] = {
    "fire_resistance_ei": (0, 240),
    "thermal_conductivity_lambda": (0.001, 1.0),
    "thermal_resistance_rd": (0.01, 20),
    "rc_value": (0.01, 20),
    "u_value": (0.1, 10),
    "airborne_sound_reduction_rw": (10, 80),
    "rw_c": (-30, 20),
    "rw_ctr": (-30, 20),
    "impact_sound_lnw": (10, 90),
    "sound_absorption_alpha_w": (0, 1),
    "thickness_mm": (0.1, 1000),
    "width_mm": (1, 20000),
    "height_mm": (1, 20000),
    "length_mm": (1, 20000),
    "max_height_mm": (100, 20000),
    "density": (1, 3000),
    "weight_per_m2": (0.1, 1000),
    "compressive_strength_cs": (0.1, 10000),
    "water_vapour_resistance_mu": (0.1, 100000),
    "service_temp_min": (-100, 500),
    "service_temp_max": (-100, 500),
    "recycled_content_pct": (0, 100),
    "gwp_a1a3": (-1000, 100000),
    "mki_value": (0, 100000),
}

# Multiplier to convert INTO the canonical unit, keyed by (from_unit, key)
# falling back to (from_unit, None) for unit-agnostic conversions. Real
# datasheets are inconsistent about units far beyond this list; extend as
# real corpus data surfaces new cases.
_UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    ("cm", "mm"): 10.0,
    ("m", "mm"): 1000.0,
    ("mpa", "kpa"): 1000.0,
    ("g/m2", "kg/m2"): 0.001,
    ("g/m3", "kg/m3"): 0.001,
}


@dataclass
class VerificationResult:
    ok: bool
    normalized_spec: dict | None
    rejection_reason: str | None


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.replace(" ", " ").replace("‑", "-").replace("–", "-").replace("—", "-")
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


def _convert_unit(value: float, from_unit: str | None, to_unit: str | None, key: str) -> tuple[float, str | None]:
    if not from_unit or not to_unit or from_unit.strip().lower() == to_unit.strip().lower():
        return value, to_unit
    factor = _UNIT_CONVERSIONS.get((from_unit.strip().lower(), to_unit.strip().lower()))
    if factor is None:
        # Unknown conversion: leave the value as-is but keep the declared
        # unit so it doesn't silently masquerade as canonical.
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


def verify_spec(spec: dict, spec_def: dict, page_text: str | None) -> VerificationResult:
    """spec: one entry from extraction.extract_products()['specs'], already
    matched to its spec_definitions row (spec_def)."""
    presence = spec.get("presence")

    if presence == "not_stated":
        return VerificationResult(True, {**spec, "value_numeric": None, "value_numeric_max": None,
                                          "value_text": None, "value_bool": None, "value_enum": None,
                                          "unit": None}, None)

    snippet = spec.get("source_snippet")
    page_number = spec.get("page_number")

    if presence == "stated":
        if not snippet or page_number is None:
            return VerificationResult(False, None, "stated value missing source_snippet or page_number")
        if page_text is None:
            return VerificationResult(False, None, f"no page text available for page {page_number} to verify against")
        if not snippet_in_page(snippet, page_text):
            return VerificationResult(False, None, "source_snippet not found verbatim on cited page")

    normalized = dict(spec)

    if spec.get("value_numeric") is not None:
        if presence == "stated" and not value_digits_in_snippet(spec["value_numeric"], snippet or ""):
            return VerificationResult(False, None, "numeric value's digits do not appear in source_snippet")

        value, unit = _convert_unit(spec["value_numeric"], spec.get("unit"), spec_def.get("unit"), spec_def["key"])
        value_max, _ = _convert_unit(spec.get("value_numeric_max"), spec.get("unit"), spec_def.get("unit"), spec_def["key"]) \
            if spec.get("value_numeric_max") is not None else (None, unit)

        reason = _check_plausibility(spec_def["key"], value, value_max)
        if reason:
            return VerificationResult(False, None, reason)

        normalized["value_numeric"] = value
        normalized["value_numeric_max"] = value_max
        normalized["unit"] = unit

    return VerificationResult(True, normalized, None)
