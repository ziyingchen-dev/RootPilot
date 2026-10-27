from types import SimpleNamespace

import pytest

from llm import mistral_client, ollama_client


def test_ollama_requests_json_format(monkeypatch):
    captured = {}

    class FakeClient:
        def chat(self, **kwargs):
            captured.update(kwargs)
            return {"message": {"content": '{"result": "ok"}'}}

    monkeypatch.setattr(ollama_client.ollama, "Client", lambda: FakeClient())

    result = ollama_client.OllamaClient(model="test-model").generate("prompt")

    assert result == '{"result": "ok"}'
    assert captured["format"] == "json"


def test_ollama_reports_client_without_format_support(monkeypatch):
    class IncompatibleClient:
        def chat(self, model, messages, options):
            return {"message": {"content": "{}"}}

    monkeypatch.setattr(
        ollama_client.ollama, "Client", lambda: IncompatibleClient()
    )

    with pytest.raises(RuntimeError, match="does not support.*structured JSON"):
        ollama_client.OllamaClient(model="test-model").generate("prompt")


@pytest.mark.parametrize("content", ["", " \t\n"])
def test_ollama_rejects_empty_content(monkeypatch, content):
    class FakeClient:
        def chat(self, **kwargs):
            return {"message": {"content": content}}

    monkeypatch.setattr(ollama_client.ollama, "Client", lambda: FakeClient())

    with pytest.raises(RuntimeError, match="Ollama model 'test-model'.*empty content"):
        ollama_client.OllamaClient(model="test-model").generate("prompt")


def test_ollama_rejects_unknown_response_shape(monkeypatch):
    class FakeClient:
        def chat(self, **kwargs):
            return {"unexpected": "shape"}

    monkeypatch.setattr(ollama_client.ollama, "Client", lambda: FakeClient())

    with pytest.raises(
        RuntimeError,
        match="Ollama model 'test-model' returned an unsupported response shape",
    ):
        ollama_client.OllamaClient(model="test-model").generate("prompt")


def test_ollama_error_does_not_expose_api_key(monkeypatch):
    api_key = "private-test-api-key"

    class FakeClient:
        def chat(self, **kwargs):
            raise RuntimeError(f"request failed with credential {api_key}")

    monkeypatch.setenv("MISTRAL_API_KEY", api_key)
    monkeypatch.setattr(ollama_client.ollama, "Client", lambda: FakeClient())
    client = ollama_client.OllamaClient(model=f"model-{api_key}")

    with pytest.raises(RuntimeError, match="Failed to query Ollama model") as error:
        client.generate("prompt")

    assert "model-[redacted]" in str(error.value)
    assert api_key not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"message": {"content": '{"result": "dict"}'}}, '{"result": "dict"}'),
        (
            SimpleNamespace(message=SimpleNamespace(content='{"result": "object"}')),
            '{"result": "object"}',
        ),
        (SimpleNamespace(content='{"result": "direct"}'), '{"result": "direct"}'),
        ('{"result": "string"}', '{"result": "string"}'),
    ],
)
def test_ollama_supported_response_shapes(monkeypatch, response, expected):
    class FakeClient:
        def chat(self, **kwargs):
            return response

    monkeypatch.setattr(ollama_client.ollama, "Client", lambda: FakeClient())

    assert ollama_client.OllamaClient(model="test-model").generate("prompt") == expected


def make_mistral_response(content='{"result": "ok"}'):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content))
        ]
    )


def install_fake_mistral(monkeypatch, complete):
    class FakeMistral:
        def __init__(self, api_key):
            self.chat = SimpleNamespace(complete=complete)

    monkeypatch.setattr(mistral_client, "Mistral", FakeMistral)


def test_mistral_requests_json_and_selected_model_temperature(monkeypatch):
    captured = {}

    def complete(**kwargs):
        captured.update(kwargs)
        return make_mistral_response()

    install_fake_mistral(monkeypatch, complete)
    result = mistral_client.MistralClient(
        model="selected-model", api_key="test-key"
    ).generate("prompt")

    assert result == '{"result": "ok"}'
    assert captured["model"] == "selected-model"
    assert captured["temperature"] == 0.2
    assert captured["response_format"] == {"type": "json_object"}


def test_mistral_requires_api_key(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    with pytest.raises(ValueError, match="MISTRAL_API_KEY is not set"):
        mistral_client.MistralClient()


@pytest.mark.parametrize("content", ["", " \t\n"])
def test_mistral_rejects_empty_content(monkeypatch, content):
    install_fake_mistral(monkeypatch, lambda **kwargs: make_mistral_response(content))

    with pytest.raises(
        RuntimeError,
        match="Mistral model 'test-model' returned empty content",
    ):
        mistral_client.MistralClient(
            model="test-model", api_key="test-key"
        ).generate("prompt")


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": "not a list"},
        {"choices": [SimpleNamespace(message=SimpleNamespace(content=42))]},
    ],
)
def test_mistral_rejects_malformed_response_shape(monkeypatch, response):
    install_fake_mistral(monkeypatch, lambda **kwargs: response)

    with pytest.raises(
        RuntimeError,
        match="Mistral model 'test-model' returned an unsupported response shape",
    ):
        mistral_client.MistralClient(
            model="test-model", api_key="test-key"
        ).generate("prompt")


def test_mistral_wraps_provider_exception_without_exposing_api_key(monkeypatch):
    api_key = "private-test-api-key"

    def complete(**kwargs):
        raise RuntimeError(f"request failed with credential {api_key}")

    install_fake_mistral(monkeypatch, complete)
    client = mistral_client.MistralClient(
        model=f"model-{api_key}", api_key=api_key
    )

    with pytest.raises(RuntimeError, match="Failed to query Mistral model") as error:
        client.generate("prompt")

    assert "model-[redacted]" in str(error.value)
    assert api_key not in str(error.value)
    assert error.value.__cause__ is None


def test_mistral_initialization_error_does_not_expose_api_key(monkeypatch):
    api_key = "private-test-api-key"

    def fail_initialization(api_key):
        raise RuntimeError(f"invalid credential {api_key}")

    monkeypatch.setattr(mistral_client, "Mistral", fail_initialization)

    with pytest.raises(RuntimeError, match="Failed to initialize Mistral client") as error:
        mistral_client.MistralClient(api_key=api_key)

    assert api_key not in str(error.value)
    assert error.value.__cause__ is None
