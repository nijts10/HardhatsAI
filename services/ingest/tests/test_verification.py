from ingest.verification import (
    normalize_text,
    snippet_in_page,
    value_digits_in_snippet,
    verify_spec,
)

FIRE_DEF = {"key": "fire_resistance_ei", "unit": "min", "data_type": "numeric"}
LAMBDA_DEF = {"key": "thermal_conductivity_lambda", "unit": "W/mK", "data_type": "numeric_range"}
THICKNESS_DEF = {"key": "thickness_mm", "unit": "mm", "data_type": "numeric"}


def test_normalize_text_collapses_whitespace_and_dashes_and_casefolds():
    a = normalize_text("Brandwerendheid  EI 60—volgens NEN")
    b = normalize_text("brandwerendheid ei 60-volgens nen")
    assert a == b


def test_snippet_in_page_true_for_exact_and_whitespace_variant():
    page_text = "Vooraanzicht.\nBrandwerendheid EI 60 volgens NEN-EN 13501-2.\nOverig."
    assert snippet_in_page("Brandwerendheid EI 60 volgens NEN-EN 13501-2.", page_text)
    assert snippet_in_page("brandwerendheid   ei 60 volgens nen-en 13501-2.", page_text)


def test_snippet_in_page_false_when_not_present():
    assert not snippet_in_page("EI 90", "Brandwerendheid EI 60 volgens NEN-EN 13501-2.")


def test_value_digits_in_snippet():
    assert value_digits_in_snippet(60, "EI 60 volgens NEN-EN 13501-2")
    assert not value_digits_in_snippet(90, "EI 60 volgens NEN-EN 13501-2")


def test_stated_value_verified_against_real_page_text_passes():
    spec = {
        "key": "fire_resistance_ei",
        "value_numeric": 60,
        "unit": "min",
        "presence": "stated",
        "page_number": 4,
        "source_snippet": "Brandwerendheid EI 60 volgens NEN-EN 13501-2",
    }
    page_text = "Technische gegevens\nBrandwerendheid EI 60 volgens NEN-EN 13501-2\nOverig."
    result = verify_spec(spec, FIRE_DEF, page_text)
    assert result.ok
    assert result.normalized_spec["value_numeric"] == 60


def test_stated_value_rejected_when_snippet_not_on_page():
    spec = {
        "key": "fire_resistance_ei",
        "value_numeric": 60,
        "unit": "min",
        "presence": "stated",
        "page_number": 4,
        "source_snippet": "Brandwerendheid EI 60 volgens NEN-EN 13501-2",
    }
    result = verify_spec(spec, FIRE_DEF, "This page never mentions fire resistance at all.")
    assert not result.ok
    assert "not found verbatim" in result.rejection_reason


def test_stated_value_rejected_when_digits_dont_match_snippet():
    spec = {
        "key": "fire_resistance_ei",
        "value_numeric": 90,  # model claims 90 but snippet says 60
        "unit": "min",
        "presence": "stated",
        "page_number": 4,
        "source_snippet": "Brandwerendheid EI 60 volgens NEN-EN 13501-2",
    }
    page_text = "Brandwerendheid EI 60 volgens NEN-EN 13501-2"
    result = verify_spec(spec, FIRE_DEF, page_text)
    assert not result.ok
    assert "digits" in result.rejection_reason


def test_not_stated_passes_through_without_page_text():
    spec = {"key": "fire_resistance_ei", "presence": "not_stated"}
    result = verify_spec(spec, FIRE_DEF, None)
    assert result.ok
    assert result.normalized_spec["value_numeric"] is None


def test_plausibility_range_rejects_misread_lambda():
    # A lambda of "35" (should be 0.035 W/mK) is the textbook misread this
    # range check exists to catch.
    spec = {
        "key": "thermal_conductivity_lambda",
        "value_numeric": 35,
        "unit": "W/mK",
        "presence": "stated",
        "page_number": 2,
        "source_snippet": "Lambda 35 W/mK",
    }
    result = verify_spec(spec, LAMBDA_DEF, "Lambda 35 W/mK")
    assert not result.ok
    assert "plausibility" in result.rejection_reason


def test_unit_conversion_cm_to_mm():
    spec = {
        "key": "thickness_mm",
        "value_numeric": 10,
        "unit": "cm",
        "presence": "stated",
        "page_number": 1,
        "source_snippet": "Dikte 10 cm",
    }
    result = verify_spec(spec, THICKNESS_DEF, "Dikte 10 cm")
    assert result.ok
    assert result.normalized_spec["value_numeric"] == 100
    assert result.normalized_spec["unit"] == "mm"


def test_stated_missing_snippet_is_rejected():
    spec = {"key": "fire_resistance_ei", "value_numeric": 60, "presence": "stated", "page_number": 4}
    result = verify_spec(spec, FIRE_DEF, "irrelevant")
    assert not result.ok
