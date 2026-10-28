import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import api_server


@pytest.fixture(autouse=True)
def clear_investigation_results():
    api_server._investigation_results.clear()
    yield
    api_server._investigation_results.clear()


def test_providers_include_supported_ollama_models():
    providers = api_server.providers_endpoint()["providers"]["ollama"]

    assert "qwen2.5-coder:3b" in providers["models"]
    assert "qwen2.5:1.5b" in providers["models"]


def test_providers_use_magistral_medium_by_default():
    providers = api_server.providers_endpoint()["providers"]["mistral"]

    assert providers["models"] == ["magistral-medium-latest"]


def _allowed_result(changes=None):
    return {
        "design_intent_status": "verified",
        "design_intent_evidence": [
            {
                "source_type": "specification",
                "reference": "docs/design.md",
                "claim": "This behavior is required.",
            }
        ],
        "proposed_changes": changes or [],
    }


def test_investigate_endpoint_passes_previous_result_and_metadata(monkeypatch):
    previous_result = {
        "evidence_summary": "Previous evidence",
        "hypotheses": ["Cause A"],
        "confidence": 60,
        "recommended_investigation": "Inspect the logs",
    }
    captured = {}

    class FakeClient:
        def __init__(self, model=None):
            self.model = model or "default-test-model"

    def fake_investigate(case_dir, client, feedback, previous_result=None):
        captured["arguments"] = (case_dir, feedback, previous_result)
        return {"design_intent_status": "missing", "proposed_changes": []}

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
    assert response.result["patch_decision"]["status"] == "blocked"
    assert response.result["generation_metadata"] == {
        "provider": "ollama",
        "model": "default-test-model",
        "data_policy": "standard",
    }
    assert response.provider == "ollama"
    assert response.model == "default-test-model"
    assert response.data_policy == "standard"
    assert response.result_id is None
    assert api_server._investigation_results == {}


def test_confidential_mistral_rejected_before_case_read_or_client(monkeypatch):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Confidential Mistral request reached a downstream operation.")

    monkeypatch.setattr(api_server, "MistralClient", unexpected_call)
    monkeypatch.setattr(api_server, "read_case", unexpected_call)

    with pytest.raises(HTTPException) as error:
        api_server.investigate_endpoint(
            api_server.InvestigationRequest(
                case_dir="missing-case",
                provider="mistral",
                data_policy="confidential",
            )
        )

    assert error.value.status_code == 400
    assert "Confidential data requires local Ollama" in error.value.detail


def test_confidential_remote_ollama_rejected_before_case_read(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama.internal:11434")
    monkeypatch.setattr(
        api_server,
        "read_case",
        lambda *args: pytest.fail("Remote confidential request read the case."),
    )

    with pytest.raises(HTTPException) as error:
        api_server.devil_review_endpoint(
            api_server.DevilReviewRequest(
                case_dir="missing-case",
                provider="ollama",
                data_policy="confidential",
                report_a={},
                report_b={},
            )
        )

    assert error.value.status_code == 400
    assert "Confidential data requires local Ollama" in error.value.detail


def test_blocked_investigation_returns_empty_diff(monkeypatch):
    changes = [{"file": "src/example.py", "original": "old", "replacement": "new"}]

    class FakeClient:
        model = "test-model"

    monkeypatch.setattr(api_server, "OllamaClient", lambda model=None: FakeClient())
    monkeypatch.setattr(
        api_server,
        "investigate",
        lambda *args, **kwargs: {
            "design_intent_status": "missing",
            "proposed_changes": changes,
        },
    )
    monkeypatch.setattr(
        api_server,
        "format_diff",
        lambda *args: pytest.fail("Blocked results must not be diffed."),
    )

    response = api_server.investigate_endpoint(
        api_server.InvestigationRequest(case_dir="case")
    )

    assert response.result["patch_decision"]["status"] == "blocked"
    assert response.result["proposed_changes"] == []
    assert response.diff == ""
    assert response.result_id is None
    assert api_server._investigation_results == {}


def test_allowed_investigation_returns_diff_without_applying(monkeypatch):
    changes = [{"file": "src/example.py", "original": "old", "replacement": "new"}]
    captured = {}

    class FakeClient:
        model = "test-model"

    monkeypatch.setattr(api_server, "OllamaClient", lambda model=None: FakeClient())
    monkeypatch.setattr(
        api_server,
        "investigate",
        lambda *args, **kwargs: _allowed_result(changes),
    )
    monkeypatch.setattr(
        api_server,
        "format_diff",
        lambda case_dir, received: captured.update(diff_args=(case_dir, received))
        or "--- a/src/example.py\n+++ b/src/example.py",
    )
    monkeypatch.setattr(
        api_server,
        "apply_changes",
        lambda *args: pytest.fail("Investigation must not apply changes."),
    )

    response = api_server.investigate_endpoint(
        api_server.InvestigationRequest(case_dir="case")
    )

    assert response.result["patch_decision"]["status"] == "allowed"
    assert response.diff == "--- a/src/example.py\n+++ b/src/example.py"
    assert captured["diff_args"] == ("case", changes)
    assert response.result_id is not None
    assert response.result_id in api_server._investigation_results
    assert api_server._investigation_results[response.result_id] == {
        "case_dir": "case",
        "result": response.result,
    }


def test_allowed_investigation_without_changes_has_no_result_id(monkeypatch):
    class FakeClient:
        model = "test-model"

    monkeypatch.setattr(api_server, "OllamaClient", lambda model=None: FakeClient())
    monkeypatch.setattr(
        api_server,
        "investigate",
        lambda *args, **kwargs: _allowed_result(),
    )
    monkeypatch.setattr(
        api_server,
        "format_diff",
        lambda *args: pytest.fail("No proposed changes should be diffed."),
    )

    response = api_server.investigate_endpoint(
        api_server.InvestigationRequest(case_dir="case")
    )

    assert response.result["patch_decision"]["status"] == "allowed"
    assert response.result["proposed_changes"] == []
    assert response.result_id is None
    assert response.diff == ""
    assert api_server._investigation_results == {}


def test_allowed_changes_without_diff_have_no_result_id(monkeypatch):
    changes = [{"file": "src/example.py", "original": "old", "replacement": "new"}]

    class FakeClient:
        model = "test-model"

    monkeypatch.setattr(api_server, "OllamaClient", lambda model=None: FakeClient())
    monkeypatch.setattr(
        api_server,
        "investigate",
        lambda *args, **kwargs: _allowed_result(changes),
    )
    monkeypatch.setattr(api_server, "format_diff", lambda *args: "")

    response = api_server.investigate_endpoint(
        api_server.InvestigationRequest(case_dir="case")
    )

    assert response.result["patch_decision"]["status"] == "allowed"
    assert response.result_id is None
    assert response.diff == ""
    assert api_server._investigation_results == {}


def test_blocked_result_cannot_reach_apply(monkeypatch):
    api_server._investigation_results["blocked-result"] = {
        "case_dir": "case",
        "result": {
            "design_intent_status": "conflicting",
            "proposed_changes": [{"file": "file.py"}],
        },
    }
    monkeypatch.setattr(
        api_server,
        "apply_changes",
        lambda *args: pytest.fail("Blocked result reached apply_changes."),
    )

    with pytest.raises(HTTPException) as error:
        api_server.apply_endpoint(
            api_server.ApplyRequest(
                case_dir="case",
                result_id="blocked-result",
            )
        )

    assert error.value.status_code == 400
    assert "missing or conflicting" in error.value.detail


def test_unknown_result_id_is_rejected(monkeypatch):
    monkeypatch.setattr(
        api_server,
        "apply_changes",
        lambda *args: pytest.fail("Unknown result reached apply_changes."),
    )

    with pytest.raises(HTTPException) as error:
        api_server.apply_endpoint(
            api_server.ApplyRequest(case_dir="case", result_id="unknown-result")
        )

    assert error.value.status_code == 404


def test_result_id_cannot_be_used_with_another_case_directory(monkeypatch):
    api_server._investigation_results["case-bound-result"] = {
        "case_dir": "trusted-case",
        "result": _allowed_result(
            [{"file": "src/file.py", "original": "old", "replacement": "new"}]
        ),
    }
    monkeypatch.setattr(
        api_server,
        "apply_changes",
        lambda *args: pytest.fail("Result was applied to a different case."),
    )

    with pytest.raises(HTTPException) as error:
        api_server.apply_endpoint(
            api_server.ApplyRequest(
                case_dir="other-case", result_id="case-bound-result"
            )
        )

    assert error.value.status_code == 400


def test_apply_uses_stored_patch_and_rejects_client_patch_fields(monkeypatch):
    trusted_changes = [
        {"file": "src/trusted.py", "original": "trusted old", "replacement": "trusted new"}
    ]
    injected_changes = [
        {"file": "src/attacker.py", "original": "old", "replacement": "injected"}
    ]
    applied = {}

    class FakeClient:
        model = "test-model"

    monkeypatch.setattr(api_server, "OllamaClient", lambda model=None: FakeClient())
    monkeypatch.setattr(
        api_server,
        "investigate",
        lambda *args, **kwargs: _allowed_result(trusted_changes),
    )
    monkeypatch.setattr(api_server, "format_diff", lambda *args: "trusted diff")

    response = api_server.investigate_endpoint(
        api_server.InvestigationRequest(case_dir="case")
    )
    response.result["proposed_changes"] = injected_changes

    def fake_apply_changes(case_dir, changes):
        applied["arguments"] = (case_dir, changes)
        return ["src/trusted.py"]

    monkeypatch.setattr(api_server, "apply_changes", fake_apply_changes)
    apply_response = api_server.apply_endpoint(
        api_server.ApplyRequest(case_dir="case", result_id=response.result_id)
    )

    assert applied["arguments"] == ("case", trusted_changes)
    assert apply_response == {"applied": ["src/trusted.py"], "count": 1}
    assert response.result_id not in api_server._investigation_results

    for field in ("proposed_changes", "patch_decision", "design_intent_evidence"):
        with pytest.raises(ValidationError):
            api_server.ApplyRequest(
                case_dir="case",
                result_id="some-id",
                **{field: injected_changes},
            )


def test_devil_review_receives_a_b_and_original_evidence(monkeypatch):
    report_a = {"report": "A data"}
    report_b = {"report": "B data"}
    captured = {}
    case = {
        "issue": "Issue text",
        "logs": "Log text",
        "repository": "Repository source",
        "git_history": "Commit evidence",
    }

    class FakeClient:
        model = "devil-test-model"

    monkeypatch.setattr(api_server, "OllamaClient", lambda model=None: FakeClient())
    monkeypatch.setattr(api_server, "read_case", lambda case_dir: case)

    def fake_devil_review(*args):
        captured["args"] = args
        return {"reviews": {"A": {}, "B": {}}, "cross_review": {}}

    monkeypatch.setattr(api_server, "devil_review", fake_devil_review)

    response = api_server.devil_review_endpoint(
        api_server.DevilReviewRequest(
            case_dir="case",
            provider="ollama",
            model="devil-test-model",
            data_policy="standard",
            report_a=report_a,
            report_b=report_b,
        )
    )

    assert captured["args"][:6] == (
        "Issue text",
        "Log text",
        "Repository source",
        "Commit evidence",
        report_a,
        report_b,
    )
    assert isinstance(captured["args"][6], FakeClient)
    assert response.result["generation_metadata"] == {
        "provider": "ollama",
        "model": "devil-test-model",
        "data_policy": "standard",
    }
    assert response.provider == "ollama"
    assert response.model == "devil-test-model"
    assert response.data_policy == "standard"
    assert response.generation_metadata == {
        "provider": "ollama",
        "model": "devil-test-model",
        "data_policy": "standard",
    }


def test_devil_review_policy_rejection_precedes_read_case(monkeypatch):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Confidential request read case before policy enforcement.")

    monkeypatch.setenv("OLLAMA_HOST", "https://remote.example")
    monkeypatch.setattr(api_server, "read_case", unexpected_call)
    monkeypatch.setattr(api_server, "OllamaClient", unexpected_call)

    with pytest.raises(HTTPException) as error:
        api_server.devil_review_endpoint(
            api_server.DevilReviewRequest(
                case_dir="case",
                provider="ollama",
                data_policy="confidential",
                report_a={},
                report_b={},
            )
        )

    assert error.value.status_code == 400


def test_metadata_includes_provider_model_policy_without_secrets(monkeypatch):
    secret = "test-api-secret"
    monkeypatch.setenv("MISTRAL_API_KEY", secret)

    class FakeClient:
        model = secret

    monkeypatch.setattr(api_server, "MistralClient", lambda model=None: FakeClient())
    monkeypatch.setattr(
        api_server,
        "devil_review",
        lambda *args: {"reviews": {"A": {}, "B": {}}},
    )
    monkeypatch.setattr(
        api_server,
        "read_case",
        lambda case_dir: {
            "issue": "Issue",
            "logs": "Logs",
            "repository": "Repository",
            "git_history": "History",
        },
    )

    response = api_server.devil_review_endpoint(
        api_server.DevilReviewRequest(
            case_dir="case",
            provider="mistral",
            data_policy="standard",
            report_a={},
            report_b={},
        )
    )

    assert response.generation_metadata == {
        "provider": "mistral",
        "model": "[redacted]",
        "data_policy": "standard",
    }
    assert secret not in repr(response.model_dump())
