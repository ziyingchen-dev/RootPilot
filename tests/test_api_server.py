import api_server


def test_providers_include_supported_ollama_models():
    providers = api_server.providers_endpoint()["providers"]["ollama"]

    assert "qwen2.5-coder:3b" in providers["models"]
    assert "qwen2.5:1.5b" in providers["models"]


def test_providers_use_magistral_medium_by_default():
    providers = api_server.providers_endpoint()["providers"]["mistral"]

    assert providers["models"] == ["magistral-medium-latest"]


def test_investigate_endpoint_passes_previous_result(monkeypatch):
    previous_result = {
        "evidence_summary": "Previous evidence",
        "hypotheses": ["Cause A"],
        "confidence": 60,
        "recommended_investigation": "Inspect the logs",
    }
    captured = {}

    class FakeClient:
        def __init__(self, model=None):
            captured["model"] = model

    def fake_investigate(case_dir, client, feedback, previous_result=None):
        captured["arguments"] = (case_dir, feedback, previous_result)
        return {"proposed_changes": []}

    monkeypatch.setattr(api_server, "OllamaClient", FakeClient)
    monkeypatch.setattr(api_server, "investigate", fake_investigate)
    monkeypatch.setattr(api_server, "format_diff", lambda case_dir, changes: "")

    response = api_server.investigate_endpoint(
        api_server.InvestigationRequest(
            case_dir="examples/hdr_timeout",
            provider="ollama",
            feedback="The timeout increase did not help.",
            previous_result=previous_result,
        )
    )

    assert captured["arguments"] == (
        "examples/hdr_timeout",
        "The timeout increase did not help.",
        previous_result,
    )
    assert response.result == {"proposed_changes": []}