from ingest.extraction import build_prompt, extract_claims


class _FakeToolUseBlock:
    type = "tool_use"
    name = "emit_products"

    def __init__(self, input_):
        self.input = input_


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeMessagesAPI:
    def __init__(self, response_products):
        self._response_products = response_products
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeMessage([_FakeToolUseBlock({"products": self._response_products})])


class _FakeClient:
    def __init__(self, response_products):
        self.messages = _FakeMessagesAPI(response_products)


SPEC_ATTRS = [
    {"key": "fire_resistance_ei", "name_en": "Fire resistance EI", "name_nl": "Brandwerendheid EI",
     "data_type": "numeric", "unit": "min", "enum_values": None},
    {"key": "demountable", "name_en": "Demountable", "name_nl": "Demontabel",
     "data_type": "boolean", "unit": None, "enum_values": None},
]


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

    assert result == fake_products
    assert client.messages.last_kwargs["tool_choice"] == {"type": "tool", "name": "emit_products"}
    assert client.messages.last_kwargs["tools"][0]["name"] == "emit_products"


def test_extract_claims_returns_empty_list_when_no_tool_use_block():
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
    assert result == []
