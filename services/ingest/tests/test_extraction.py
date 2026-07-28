from ingest.extraction import build_prompt, extract_products


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


SPEC_DEFS = [
    {"key": "fire_resistance_ei", "name_en": "Fire resistance EI", "name_nl": "Brandwerendheid EI",
     "data_type": "numeric", "unit": "min", "enum_values": None},
    {"key": "reaction_to_fire", "name_en": "Reaction to fire", "name_nl": "Brandklasse",
     "data_type": "enum", "unit": None, "enum_values": ["A1", "A2", "B", "C", "D", "E", "F"]},
]


def test_build_prompt_includes_vocabulary_and_forbids_invented_keys():
    prompt = build_prompt(
        spec_definitions=SPEC_DEFS,
        category_codes=["systeemwand"],
        chunks=[{"id": "c1", "page_start": 1, "page_end": 1, "heading_path": ["Technische gegevens"],
                 "content": "Brandwerendheid EI 60"}],
    )
    assert "fire_resistance_ei" in prompt
    assert "reaction_to_fire" in prompt
    assert "ONLY use these keys" in prompt
    assert "Technische gegevens" in prompt
    assert "Brandwerendheid EI 60" in prompt


def test_extract_products_returns_tool_input_and_forces_tool_choice():
    fake_products = [{
        "canonical_name": "Rockwool 211",
        "manufacturer_ref": "RW-211",
        "category_code": "systeemwand",
        "specs": [{"key": "fire_resistance_ei", "value_numeric": 60, "presence": "stated",
                   "page_number": 1, "source_snippet": "EI 60"}],
    }]
    client = _FakeClient(fake_products)

    result = extract_products(
        client,
        model="claude-sonnet-5",
        spec_definitions=SPEC_DEFS,
        category_codes=["systeemwand"],
        chunks=[{"id": "c1", "page_start": 1, "page_end": 1, "heading_path": [], "content": "EI 60"}],
    )

    assert result == fake_products
    assert client.messages.last_kwargs["tool_choice"] == {"type": "tool", "name": "emit_products"}
    assert client.messages.last_kwargs["tools"][0]["name"] == "emit_products"


def test_extract_products_returns_empty_list_when_no_tool_use_block():
    class _NoToolMessage:
        content = []

    class _NoToolMessagesAPI:
        def create(self, **kwargs):
            return _NoToolMessage()

    class _NoToolClient:
        messages = _NoToolMessagesAPI()

    result = extract_products(
        _NoToolClient(), model="claude-sonnet-5", spec_definitions=SPEC_DEFS,
        category_codes=["systeemwand"], chunks=[],
    )
    assert result == []
