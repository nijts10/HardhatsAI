import json

from ingest.extraction import ExtractionResult, build_prompt, extract_claims, spec_attributes_for_category


class _FakeToolUseBlock:
    type = "tool_use"
    name = "emit_products"

    def __init__(self, input_):
        self.input = input_


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeMessagesAPI:
    def __init__(self, response_products, response_unmapped_findings=None):
        self._response_products = response_products
        self._response_unmapped_findings = response_unmapped_findings if response_unmapped_findings is not None else []
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeMessage([_FakeToolUseBlock({
            "products": self._response_products,
            "unmapped_findings": self._response_unmapped_findings,
        })])


class _FakeClient:
    def __init__(self, response_products, response_unmapped_findings=None):
        self.messages = _FakeMessagesAPI(response_products, response_unmapped_findings)


SPEC_ATTRS = [
    {"key": "fire_resistance_ei", "name_en": "Fire resistance EI", "name_nl": "Brandwerendheid EI",
     "data_type": "numeric", "unit": "min", "enum_values": None},
    {"key": "demountable", "name_en": "Demountable", "name_nl": "Demontabel",
     "data_type": "boolean", "unit": None, "enum_values": None},
]


def test_spec_attributes_for_category_keeps_universal_and_matching_scoped_rows():
    rows = [
        {"key": "thickness_mm", "category_codes": []},
        {"key": "fire_resistance_ei", "category_codes": ["brandwerende_systeemwanden", "glaswanden"]},
        {"key": "demountable", "category_codes": ["technische_binnenwanden"]},
    ]

    result = spec_attributes_for_category(rows, ["brandwerende_systeemwanden"])

    keys = {d["key"] for d in result}
    assert keys == {"thickness_mm", "fire_resistance_ei"}


def test_spec_attributes_for_category_with_no_category_keeps_only_universal_rows():
    rows = [
        {"key": "thickness_mm", "category_codes": []},
        {"key": "fire_resistance_ei", "category_codes": ["brandwerende_systeemwanden"]},
    ]

    result = spec_attributes_for_category(rows, None)

    assert {d["key"] for d in result} == {"thickness_mm"}
    assert {d["key"] for d in spec_attributes_for_category(rows, [])} == {"thickness_mm"}


def test_spec_attributes_for_category_inherits_from_ancestor_codes():
    # A spec tagged at a broad parent category ("mineral_wool") should
    # apply to a product whose own category is a descendant ("rock_wool"),
    # as long as the caller passes the ancestor-inclusive code list (what
    # db.get_category_ancestor_codes returns for that product's category).
    rows = [{"key": "thermal_conductivity", "category_codes": ["mineral_wool"]}]

    applicable_codes = ["rock_wool", "mineral_wool", "insulation"]  # leaf -> root
    assert {d["key"] for d in spec_attributes_for_category(rows, applicable_codes)} == {"thermal_conductivity"}
    assert spec_attributes_for_category(rows, ["facade_cladding"]) == []


def test_build_prompt_includes_vocabulary_brand_name_and_forbids_invented_keys():
    prompt = build_prompt(
        brand_name="Acme",
        spec_attributes=SPEC_ATTRS,
        chunks=[{"id": "c1", "page_start": 1, "page_end": 1, "heading_path": ["Technische gegevens"],
                 "content": "Brandwerendheid EI 60"}],
    )
    assert "fire_resistance_ei" in prompt
    assert "demountable" in prompt
    assert "Acme" in prompt
    assert "ONLY use these keys" in prompt
    assert "Brandwerendheid EI 60" in prompt
    assert "unmapped_findings" in prompt
    assert "value_numeric_max" in prompt


def test_extract_claims_passes_variants_through_untouched():
    fake_products = [{
        "name": "Insulatie X", "manufacturer_ref": None,
        "claims": [{"key": "demountable", "presence": "not_stated"}],
        "certifications": [],
        "variants": [
            {"label": "40mm", "manufacturer_ref": None,
             "claims": [{"key": "airborne_sound_reduction_rw", "value_numeric": 45, "unit": "dB",
                         "presence": "stated", "page_number": 1, "source_snippet": "40mm Rw 45"}]},
            {"label": "80mm", "manufacturer_ref": None,
             "claims": [{"key": "airborne_sound_reduction_rw", "value_numeric": 52, "unit": "dB",
                         "presence": "stated", "page_number": 1, "source_snippet": "80mm Rw 52"}]},
        ],
    }]
    client = _FakeClient(fake_products)

    result = extract_claims(
        client, model="claude-sonnet-5", brand_name="Acme",
        spec_attributes=SPEC_ATTRS,
        chunks=[{"id": "c1", "page_start": 1, "page_end": 1, "heading_path": [], "content": "n/a"}],
    )

    assert result.products == fake_products
    assert [v["label"] for v in result.products[0]["variants"]] == ["40mm", "80mm"]


def test_extract_claims_returns_tool_input_and_forces_tool_choice():
    fake_products = [{
        "name": "Acme 211", "manufacturer_ref": "RW-211",
        "claims": [{"key": "fire_resistance_ei", "value_numeric": 60, "presence": "stated",
                    "page_number": 1, "source_snippet": "EI 60"}],
        "certifications": [],
    }]
    client = _FakeClient(fake_products)

    result = extract_claims(
        client, model="claude-sonnet-5", brand_name="Acme",
        spec_attributes=SPEC_ATTRS,
        chunks=[{"id": "c1", "page_start": 1, "page_end": 1, "heading_path": [], "content": "EI 60"}],
    )

    assert result.products == fake_products
    assert result.unmapped_findings == []
    assert client.messages.last_kwargs["tool_choice"] == {"type": "tool", "name": "emit_products"}
    assert client.messages.last_kwargs["tools"][0]["name"] == "emit_products"


def test_extract_claims_returns_unmapped_findings():
    fake_findings = [
        {"found_term": "water_resistant", "page_number": 3, "source_snippet": "Water resistant: yes"},
        {"found_term": "corrosion_resistance", "page_number": 2, "source_snippet": "Corrosiveness to Steel - Passed"},
    ]
    client = _FakeClient([], fake_findings)

    result = extract_claims(
        client, model="claude-sonnet-5", brand_name="Acme",
        spec_attributes=SPEC_ATTRS,
        chunks=[{"id": "c1", "page_start": 1, "page_end": 3, "heading_path": [], "content": "n/a"}],
    )

    assert result.unmapped_findings == fake_findings


def test_extract_claims_unwraps_double_encoded_products_string():
    # Observed against a real document: the model sometimes returns the
    # "products" field as a JSON-encoded string of the whole
    # {"products": [...]} object instead of a structured array.
    fake_products = [{
        "name": "ROXUL Safe", "manufacturer_ref": None,
        "claims": [{"key": "demountable", "presence": "not_stated"}],
        "certifications": [],
    }]

    class _RawInputToolUseBlock:
        type = "tool_use"
        name = "emit_products"
        input = {"products": json.dumps({"products": fake_products}), "unmapped_findings": []}

    class _RawInputMessagesAPI:
        def create(self, **kwargs):
            return _FakeMessage([_RawInputToolUseBlock()])

    class _RawInputClient:
        messages = _RawInputMessagesAPI()

    result = extract_claims(
        _RawInputClient(), model="claude-sonnet-5", brand_name="Rockwool",
        spec_attributes=SPEC_ATTRS,
        chunks=[{"id": "c1", "page_start": 1, "page_end": 1, "heading_path": [], "content": "n/a"}],
    )

    assert result.products == fake_products


def test_extract_claims_unwraps_double_encoded_bare_array_string():
    fake_products = [{
        "name": "ROXUL Safe", "manufacturer_ref": None,
        "claims": [], "certifications": [],
    }]

    class _RawInputToolUseBlock:
        type = "tool_use"
        name = "emit_products"
        input = {"products": json.dumps(fake_products), "unmapped_findings": []}

    class _RawInputMessagesAPI:
        def create(self, **kwargs):
            return _FakeMessage([_RawInputToolUseBlock()])

    class _RawInputClient:
        messages = _RawInputMessagesAPI()

    result = extract_claims(
        _RawInputClient(), model="claude-sonnet-5", brand_name="Rockwool",
        spec_attributes=SPEC_ATTRS,
        chunks=[{"id": "c1", "page_start": 1, "page_end": 1, "heading_path": [], "content": "n/a"}],
    )

    assert result.products == fake_products


def test_extract_claims_returns_empty_result_when_no_tool_use_block():
    class _NoToolMessage:
        content = []

    class _NoToolMessagesAPI:
        def create(self, **kwargs):
            return _NoToolMessage()

    class _NoToolClient:
        messages = _NoToolMessagesAPI()

    result = extract_claims(
        _NoToolClient(), model="claude-sonnet-5", brand_name="Acme",
        spec_attributes=SPEC_ATTRS, chunks=[],
    )
    assert result == ExtractionResult(products=[], unmapped_findings=[])
