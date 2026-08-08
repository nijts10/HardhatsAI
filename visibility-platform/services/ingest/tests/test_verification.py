from ingest.verification import (
    normalize_text,
    snippet_in_page,
    value_digits_in_snippet,
    verify_claim,
)

FIRE_ATTR = {"key": "fire_resistance_ei", "unit": "min", "data_type": "numeric"}
HEIGHT_ATTR = {"key": "max_height_mm", "unit": "mm", "data_type": "numeric"}
DEMOUNTABLE_ATTR = {"key": "demountable", "unit": None, "data_type": "boolean"}


def test_normalize_text_collapses_whitespace_and_dashes_and_casefolds():
    a = normalize_text("Brandwerendheid  EI 60—volgens NEN")
    b = normalize_text("brandwerendheid ei 60-volgens nen")
    assert a == b


def test_snippet_in_page_true_and_false():
    page_text = "Brandwerendheid EI 60 volgens NEN-EN 13501-2."
    assert snippet_in_page("Brandwerendheid EI 60 volgens NEN-EN 13501-2.", page_text)
    assert not snippet_in_page("EI 90", page_text)


def test_value_digits_in_snippet():
    assert value_digits_in_snippet(60, "EI 60 volgens NEN-EN 13501-2")
    assert not value_digits_in_snippet(90, "EI 60 volgens NEN-EN 13501-2")


def test_stated_claim_verified_against_real_page_text_passes():
    claim = {
        "key": "fire_resistance_ei", "value_numeric": 60, "unit": "min", "presence": "stated",
        "page_number": 4, "source_snippet": "Brandwerendheid EI 60 volgens NEN-EN 13501-2",
    }
    page_text = "Technische gegevens\nBrandwerendheid EI 60 volgens NEN-EN 13501-2\n"
    result = verify_claim(claim, FIRE_ATTR, page_text)
    assert result.ok
    assert result.normalized_claim["value_numeric"] == 60


def test_stated_claim_rejected_when_snippet_not_on_page():
    claim = {
        "key": "fire_resistance_ei", "value_numeric": 60, "unit": "min", "presence": "stated",
        "page_number": 4, "source_snippet": "Brandwerendheid EI 60 volgens NEN-EN 13501-2",
    }
    result = verify_claim(claim, FIRE_ATTR, "This page never mentions fire resistance.")
    assert not result.ok
    assert "not found verbatim" in result.rejection_reason


def test_stated_claim_rejected_when_digits_dont_match_snippet():
    claim = {
        "key": "fire_resistance_ei", "value_numeric": 90, "unit": "min", "presence": "stated",
        "page_number": 4, "source_snippet": "Brandwerendheid EI 60 volgens NEN-EN 13501-2",
    }
    result = verify_claim(claim, FIRE_ATTR, "Brandwerendheid EI 60 volgens NEN-EN 13501-2")
    assert not result.ok
    assert "digits" in result.rejection_reason


def test_not_stated_passes_through_without_page_text():
    claim = {"key": "fire_resistance_ei", "presence": "not_stated"}
    result = verify_claim(claim, FIRE_ATTR, None)
    assert result.ok
    assert result.normalized_claim["value_numeric"] is None


def test_boolean_claim_stated_with_citation():
    claim = {
        "key": "demountable", "value_bool": True, "presence": "stated",
        "page_number": 2, "source_snippet": "Het systeem is volledig demontabel",
    }
    result = verify_claim(claim, DEMOUNTABLE_ATTR, "Het systeem is volledig demontabel")
    assert result.ok
    assert result.normalized_claim["value_bool"] is True


def test_plausibility_range_rejects_implausible_height():
    claim = {
        "key": "max_height_mm", "value_numeric": 50, "unit": "mm", "presence": "stated",
        "page_number": 1, "source_snippet": "Maximale hoogte 50 mm",
    }
    result = verify_claim(claim, HEIGHT_ATTR, "Maximale hoogte 50 mm")
    assert not result.ok
    assert "plausibility" in result.rejection_reason


def test_unit_conversion_cm_to_mm():
    claim = {
        "key": "max_height_mm", "value_numeric": 400, "unit": "cm", "presence": "stated",
        "page_number": 1, "source_snippet": "Maximale hoogte 400 cm",
    }
    result = verify_claim(claim, HEIGHT_ATTR, "Maximale hoogte 400 cm")
    assert result.ok
    assert result.normalized_claim["value_numeric"] == 4000
    assert result.normalized_claim["unit"] == "mm"


def test_stated_missing_snippet_is_rejected():
    claim = {"key": "fire_resistance_ei", "value_numeric": 60, "presence": "stated", "page_number": 4}
    result = verify_claim(claim, FIRE_ATTR, "irrelevant")
    assert not result.ok


def test_derived_claim_without_citation_is_rejected():
    # The gap this closes: a 'derived' claim used to skip the citation
    # check entirely and pass with ANY snippet, or none at all. This must
    # now be rejected exactly like an uncited 'stated' claim would be.
    claim = {"key": "fire_resistance_ei", "value_numeric": 60, "presence": "derived"}
    result = verify_claim(claim, FIRE_ATTR, "irrelevant")
    assert not result.ok
    assert "derived claim missing" in result.rejection_reason


def test_derived_claim_rejected_when_reasoning_is_stuffed_into_snippet():
    # Mirrors a real observed case: the model wrote its own reasoning
    # directly into source_snippet ("...corresponding to Euroclass A1")
    # instead of quoting the page verbatim. That must still fail, the same
    # way it would for a 'stated' claim.
    claim = {
        "key": "fire_resistance_ei", "value_numeric": 60, "presence": "derived",
        "page_number": 2, "source_snippet": "Noncombustible (derived from ASTM E136, corresponding to A1)",
    }
    result = verify_claim(claim, FIRE_ATTR, "Combustibility of Materials at 750 C - Noncombustible")
    assert not result.ok
    assert "not found verbatim" in result.rejection_reason


def test_derived_claim_with_verbatim_snippet_and_derivation_note_passes():
    claim = {
        "key": "fire_resistance_ei", "value_numeric": 60, "unit": "min", "presence": "derived",
        "page_number": 2, "source_snippet": "Combustibility of Materials at 750 C - Noncombustible",
        "derivation_note": "US noncombustibility rating implies the equivalent EI period.",
    }
    page_text = "Combustibility of Materials at 750 C - Noncombustible"
    result = verify_claim(claim, FIRE_ATTR, page_text)
    assert result.ok
    assert result.normalized_claim["derivation_note"] == "US noncombustibility rating implies the equivalent EI period."
