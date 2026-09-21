import json
from types import SimpleNamespace

import pytest

import main


VALID_RESULT = {
    "evidence_summary": "Evidence found",
    "hypotheses": ["Cause A", "Cause B"],
    "confidence": 80,
    "recommended_investigation": "Inspect the affected path",
}


def response(**changes):
    data = VALID_RESULT | changes
    return json.dumps(data)


def test_read_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.cpp").write_text("int main() {}", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "worker.py").write_text("print('ok')", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("ignored", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"\xff\xfe")

    result = main.read_repository(tmp_path)

    assert "main.cpp" in result
    assert "src/worker.py" in result
    assert "int main() {}" in result
    assert ".git/config" not in result
    assert "binary.bin" not in result


def test_read_repository_missing(tmp_path):
    assert main.read_repository(tmp_path) == "No repository files found."


def test_read_repository_empty(tmp_path):
    (tmp_path / "repo").mkdir()
    assert main.read_repository(tmp_path) == "No repository files found."


def test_read_git_history_success(monkeypatch):
    def fake_run(command, **kwargs):
        assert command[-3:] == ["log", "--oneline", "-5"]
        return SimpleNamespace(returncode=0, stdout="abc123 Fix issue\n")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    assert main.read_git_history("/repo") == "abc123 Fix issue"


def test_read_git_history_failure(monkeypatch):
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    assert main.read_git_history("/repo") == "No git history available."


def test_read_case(monkeypatch, tmp_path):
    (tmp_path / "issue.md").write_text("Issue", encoding="utf-8")
    (tmp_path / "logs.txt").write_text("Logs", encoding="utf-8")
    (tmp_path / "repo").mkdir()

    monkeypatch.setattr(main, "read_repository", lambda path: "Repository")
    monkeypatch.setattr(main, "read_git_history", lambda path: "Git history")

    result = main.read_case(tmp_path)

    assert result == {
        "issue": "Issue",
        "logs": "Logs",
        "repository": "Repository",
        "git_history": "Git history",
    }


def test_read_case_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        main.read_case(tmp_path / "missing")


@pytest.mark.parametrize("missing_name", ["issue.md", "logs.txt"])
def test_read_case_missing_required_file(tmp_path, missing_name):
    (tmp_path / "issue.md").write_text("Issue", encoding="utf-8")
    (tmp_path / "logs.txt").write_text("Logs", encoding="utf-8")
    (tmp_path / missing_name).unlink()

    with pytest.raises(FileNotFoundError, match=missing_name):
        main.read_case(tmp_path)


def test_parse_response():
    result = main.parse_response(response())

    assert result["confidence"] == 80
    assert result["hypotheses"] == ["Cause A", "Cause B"]


def test_parse_response_code_block():
    result = main.parse_response(f"```json\n{response()}\n```")

    assert result["evidence_summary"] == "Evidence found"


def test_parse_response_with_surrounding_text():
    result = main.parse_response(f"Model output:\n{response()}\nDone")

    assert result["recommended_investigation"] == "Inspect the affected path"


def test_parse_response_empty():
    with pytest.raises(ValueError, match="empty response"):
        main.parse_response("")


def test_parse_response_without_json():
    with pytest.raises(ValueError, match="does not contain"):
        main.parse_response("No JSON returned")


def test_parse_response_invalid_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        main.parse_response('{"confidence": }')


def test_parse_response_missing_fields():
    with pytest.raises(ValueError, match="missing fields"):
        main.parse_response("{}")


@pytest.mark.parametrize("hypotheses", [None, "", {}, []])
def test_parse_response_invalid_hypotheses(hypotheses):
    with pytest.raises(ValueError, match="non-empty list"):
        main.parse_response(response(hypotheses=hypotheses))


def test_parse_response_removes_empty_hypotheses():
    result = main.parse_response(
        response(hypotheses=[" Cause A ", "", "   ", "Cause B"])
    )

    assert result["hypotheses"] == ["Cause A", "Cause B"]


def test_parse_response_hypotheses_without_text():
    with pytest.raises(ValueError, match="must contain text"):
        main.parse_response(response(hypotheses=["", "   "]))


def test_parse_response_numeric_string_confidence():
    result = main.parse_response(response(confidence="82.5"))

    assert result["confidence"] == 82.5


def test_parse_response_invalid_confidence():
    with pytest.raises(ValueError, match="numeric"):
        main.parse_response(response(confidence="unknown"))


def test_format_result_without_feedback():
    output = main.format_result(VALID_RESULT, "")

    assert "Confidence: 80%" in output
    assert "Evidence found" in output
    assert "1. Cause A" in output
    assert "2. Cause B" in output
    assert "Additional Evidence:" not in output
    assert "Inspect the affected path" in output


def test_format_result_with_feedback():
    output = main.format_result(VALID_RESULT, "Timeout increase did not help")

    assert "Additional Evidence:" in output
    assert "Timeout increase did not help" in output


def test_investigate(monkeypatch):
    case = {
        "issue": "Issue",
        "logs": "Logs",
        "repository": "Repository",
        "git_history": "History",
    }
    captured = {}

    monkeypatch.setattr(main, "read_case", lambda path: case)

    def fake_prompt(issue, logs, repository, history, feedback):
        captured["arguments"] = (issue, logs, repository, history, feedback)
        return "Generated prompt"

    monkeypatch.setattr(main, "build_investigation_prompt", fake_prompt)

    class FakeClient:
        def generate(self, prompt):
            captured["prompt"] = prompt
            return response()

    result = main.investigate("case", FakeClient(), "New evidence")

    assert captured["arguments"] == (
        "Issue",
        "Logs",
        "Repository",
        "History",
        "New evidence",
    )
    assert captured["prompt"] == "Generated prompt"
    assert result["confidence"] == 80


def test_interactive_loop_accept(monkeypatch, capsys):
    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr(
        main,
        "investigate",
        lambda case_dir, client, feedback: VALID_RESULT.copy(),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "A")

    main.interactive_loop("case", None)

    assert "Investigation accepted." in capsys.readouterr().out


def test_interactive_loop_quit(monkeypatch, capsys):
    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr(
        main,
        "investigate",
        lambda case_dir, client, feedback: VALID_RESULT.copy(),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "Q")

    main.interactive_loop("case", None)

    assert "Investigation session ended." in capsys.readouterr().out


def test_interactive_loop_feedback(monkeypatch, capsys):
    answers = iter(["R", "New evidence", "Q"])
    feedback_values = []

    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    def fake_investigate(case_dir, client, feedback):
        feedback_values.append(feedback)
        return VALID_RESULT.copy()

    monkeypatch.setattr(main, "investigate", fake_investigate)

    main.interactive_loop("case", None)

    assert feedback_values == ["", "New evidence"]
    assert "Additional Evidence:" in capsys.readouterr().out


def test_interactive_loop_invalid_choice(monkeypatch, capsys):
    answers = iter(["invalid", "Q"])

    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr(
        main,
        "investigate",
        lambda case_dir, client, feedback: VALID_RESULT.copy(),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    main.interactive_loop("case", None)

    assert "Invalid choice" in capsys.readouterr().out


def test_main(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "investigate", "examples/hdr_timeout", "--model", "test-model"],
    )
    monkeypatch.setattr(
        main,
        "interactive_loop",
        lambda case_dir, model, provider: captured.update(
            case_dir=case_dir,
            model=model,
            provider=provider,
        ),
    )

    main.main()

    assert captured == {
        "case_dir": "examples/hdr_timeout",
        "model": "test-model",
        "provider": "ollama",
    }


def test_main_converts_error_to_system_exit(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "investigate", "examples/hdr_timeout"],
    )

    def fail(case_dir, model, provider):
        raise RuntimeError("Ollama unavailable")

    monkeypatch.setattr(main, "interactive_loop", fail)

    with pytest.raises(SystemExit, match="Ollama unavailable"):
        main.main()

def test_interactive_loop_empty_feedback(monkeypatch, capsys):
    answers = iter(["R", "", "Q"])

    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr(
        main,
        "investigate",
        lambda case_dir, client, feedback: VALID_RESULT.copy(),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    main.interactive_loop("case", None)

    assert "No additional evidence was provided." in capsys.readouterr().out


def test_module_entrypoint(monkeypatch):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["main.py", "--help"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path("main.py", run_name="__main__")

    assert exc.value.code == 0
