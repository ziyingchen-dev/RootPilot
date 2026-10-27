import json
from types import SimpleNamespace

import pytest

import main


VALID_RESULT = {
    "evidence_summary": "Evidence found",
    "observed_facts": ["A log entry reports the observed state."],
    "reporter_assumptions": [],
    "competing_interpretations": [],
    "design_intent_status": "verified",
    "design_intent_evidence": [
        {
            "source_type": "specification",
            "reference": "docs/design-specification.md",
            "claim": "The documented design requires the current behavior.",
        }
    ],
    "hypotheses": ["Cause A", "Cause B"],
    "confidence": 80,
    "recommended_investigation": "Inspect the affected path",
}

VALID_PROBLEM_DEFINITION_REVIEW = {
    "issue_claims_treated_as_facts": [],
    "unverified_expected_behavior": [],
    "code_message_test_conflicts": [],
    "required_design_evidence": [],
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

    def fake_prompt(issue, logs, repository, history, feedback, previous_result=None):
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


def test_load_investigation_report_reads_a_and_b(tmp_path):
    report_a = tmp_path / "A.json"
    report_b = tmp_path / "B.json"
    report_a.write_text(json.dumps(VALID_RESULT | {"evidence_summary": "A"}))
    report_b.write_text(json.dumps(VALID_RESULT | {"evidence_summary": "B"}))

    assert main.load_investigation_report(report_a)["evidence_summary"] == "A"
    assert main.load_investigation_report(report_b)["evidence_summary"] == "B"


def test_devil_review_includes_evidence_reports_and_cross_review():
    report_a = VALID_RESULT | {"evidence_summary": "Report A"}
    report_b = VALID_RESULT | {"evidence_summary": "Report B"}
    captured = {}
    cross_review = {
        "shared_assumptions": [],
        "shared_blind_spots": [],
        "possible_design_history_loss": [],
        "investigation_conflicts": [],
    }
    problem_review = {
        "issue_claims_treated_as_facts": [
            "A and B treated the issue's expected behavior as fact without independent evidence."
        ],
        "unverified_expected_behavior": ["The expected behavior is not established."],
        "code_message_test_conflicts": ["The code and status message support competing interpretations."],
        "required_design_evidence": ["Obtain design history before changing behavior."],
    }

    class FakeClient:
        def generate(self, prompt):
            captured["prompt"] = prompt
            return json.dumps({
                "reviews": {
                    name: {
                        "unsupported_assumptions": [],
                        "missing_evidence": [],
                        "alternative_explanations": [],
                        "design_history_issues": [],
                        "local_traps": [],
                        "confidence_assessment": "Not assessable from the report.",
                    }
                    for name in ("A", "B")
                },
                "cross_review": cross_review,
                "problem_definition_review": problem_review,
            })

    result = main.devil_review(
        "Issue evidence",
        "Log evidence",
        "Repository evidence",
        "Git evidence",
        report_a,
        report_b,
        FakeClient(),
    )

    for evidence in ("Issue evidence", "Log evidence", "Repository evidence", "Git evidence"):
        assert evidence in captured["prompt"]
    assert "Report A" in captured["prompt"]
    assert "Report B" in captured["prompt"]
    assert result["reviews"]["A"]["confidence_assessment"].startswith("Not assessable")
    assert "fix" in captured["prompt"].lower()
    assert "consensus" in captured["prompt"].lower()
    assert result["cross_review"] == cross_review
    assert result["problem_definition_review"] == problem_review
    for field in VALID_PROBLEM_DEFINITION_REVIEW:
        assert f'"{field}"' in captured["prompt"]
    assert '"problem_definition_review"' in captured["prompt"]
    assert "Treat issue.md" in captured["prompt"]
    assert "check whether either report accepted" in captured["prompt"]
    assert "those claims without independent evidence" in captured["prompt"]
    assert "Agreement between A and B is" in captured["prompt"]
    assert "recommend blocking any behavior-changing patch" in " ".join(
        captured["prompt"].split()
    )


def test_parse_devil_review_requires_a_and_b():
    empty_report_review = {
        "unsupported_assumptions": [],
        "missing_evidence": [],
        "alternative_explanations": [],
        "design_history_issues": [],
        "local_traps": [],
        "confidence_assessment": "",
    }
    with pytest.raises(ValueError, match="separate reviews for A and B"):
        main.parse_devil_review(json.dumps({
            "reviews": {"A": empty_report_review},
            "cross_review": {},
            "problem_definition_review": VALID_PROBLEM_DEFINITION_REVIEW,
        }))


def test_parse_response_validates_new_investigation_fields():
    result = main.parse_response(response(
        observed_facts=["Observed"],
        reporter_assumptions=["Inferred from issue.md"],
        competing_interpretations=["Code behavior", "Message wording"],
        design_intent_status="missing",
    ))

    assert result["observed_facts"] == ["Observed"]
    assert result["reporter_assumptions"] == ["Inferred from issue.md"]
    assert result["competing_interpretations"] == ["Code behavior", "Message wording"]
    assert result["design_intent_status"] == "missing"


@pytest.mark.parametrize("status", ["missing", "conflicting"])
def test_parse_response_blocks_changes_when_design_intent_unresolved(status):
    changes = [{"file": "main.cpp", "original": "old", "replacement": "new"}]
    result = main.parse_response(
        response(
            design_intent_status=status,
            proposed_changes=changes,
            evidence_summary="Keep this evidence.",
        )
    )

    assert result["proposed_changes"] == []
    assert result["patch_decision"] == {
        "status": "blocked",
        "reason": "Design intent is missing or conflicting.",
    }
    assert result["evidence_summary"] == "Keep this evidence."
    assert result["design_intent_status"] == status


def test_parse_response_allows_changes_when_design_intent_verified():
    changes = [{"file": "main.cpp", "original": "old", "replacement": "new"}]
    result = main.parse_response(
        response(design_intent_status="verified", proposed_changes=changes)
    )

    assert result["proposed_changes"] == changes
    assert result["patch_decision"] == {
        "status": "allowed",
        "reason": "Design intent is verified.",
    }


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        [],
        [{}],
        [
            {
                "source_type": "logs",
                "reference": "logs.txt",
                "claim": "The log shows the behavior.",
            }
        ],
        [
            {
                "source_type": "approved_requirement",
                "reference": "issue.md",
                "claim": "The issue states the expected behavior.",
            }
        ],
        [
            {
                "source_type": "unknown_source",
                "reference": "docs/design-specification.md",
                "claim": "The specification establishes intended behavior.",
            }
        ],
        [
            {
                "source_type": "specification",
                "reference": "status_message.txt",
                "claim": "The message describes the behavior.",
            }
        ],
        [
            {
                "source_type": "specification",
                "reference": "",
                "claim": "The specification establishes intended behavior.",
            }
        ],
        [
            {
                "source_type": "specification",
                "reference": "docs/design-specification.md",
                "claim": "",
            }
        ],
        [
            {
                "source_type": "specification",
                "reference": "issue.md",
                "claim": "The issue states the expected behavior.",
            }
        ],
        [
            {
                "source_type": "specification",
                "reference": "logs.txt",
                "claim": "The log shows the behavior.",
            }
        ],
        [
            {
                "source_type": "specification",
                "reference": "docs/design-specification.md",
                "claim": " ",
            }
        ],
    ],
)
def test_parse_response_downgrades_verified_without_qualifying_evidence(evidence):
    changes = [{"file": "main.cpp", "original": "old", "replacement": "new"}]
    result = main.parse_response(
        response(
            design_intent_status="verified",
            design_intent_evidence=evidence,
            proposed_changes=changes,
        )
    )

    assert result["design_intent_status"] == "missing"
    assert result["model_design_intent_status"] == "verified"
    assert result["proposed_changes"] == []
    assert result["patch_decision"] == {
        "status": "blocked",
        "reason": "Authoritative design evidence is missing or incomplete.",
    }


def test_parse_response_downgrades_verified_when_evidence_field_is_absent():
    result_data = VALID_RESULT.copy()
    result_data.pop("design_intent_evidence")

    result = main.parse_response(
        json.dumps(result_data | {"proposed_changes": [{"file": "main.cpp"}]})
    )

    assert result["design_intent_status"] == "missing"
    assert result["proposed_changes"] == []
    assert result["patch_decision"]["status"] == "blocked"


@pytest.mark.parametrize("status", ["missing", "conflicting"])
def test_parse_response_blocks_when_no_changes_were_proposed(status):
    result = main.parse_response(response(design_intent_status=status))

    assert result["proposed_changes"] == []
    assert result["patch_decision"]["status"] == "blocked"
    assert result["evidence_summary"] == VALID_RESULT["evidence_summary"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("observed_facts", "Observed", "observed_facts must be a list of strings"),
        ("reporter_assumptions", [1], "reporter_assumptions must be a list of strings"),
        (
            "competing_interpretations",
            None,
            "competing_interpretations must be a list of strings",
        ),
        ("design_intent_status", "unknown", "design_intent_status must be one of"),
        ("design_intent_status", [], "design_intent_status must be one of"),
    ],
)
def test_parse_response_rejects_malformed_new_fields(field, value, message):
    with pytest.raises(ValueError, match=message):
        main.parse_response(response(**{field: value}))


@pytest.mark.parametrize("status", ["unknown", None, [], 1])
def test_enforce_patch_policy_rejects_invalid_design_intent_status(status):
    with pytest.raises(ValueError, match="design_intent_status must be one of"):
        main.enforce_patch_policy({"design_intent_status": status})


@pytest.mark.parametrize("field", [
    "observed_facts",
    "reporter_assumptions",
    "competing_interpretations",
    "design_intent_status",
])
def test_parse_response_requires_new_fields(field):
    result = VALID_RESULT.copy()
    result.pop(field)

    with pytest.raises(ValueError, match="missing fields"):
        main.parse_response(json.dumps(result))


def test_parse_devil_review_validates_problem_definition_review():
    review = {
        "reviews": {
            name: {
                "unsupported_assumptions": [],
                "missing_evidence": [],
                "alternative_explanations": [],
                "design_history_issues": [],
                "local_traps": [],
                "confidence_assessment": "Not assessable.",
            }
            for name in ("A", "B")
        },
        "cross_review": {
            "shared_assumptions": [],
            "shared_blind_spots": [],
            "possible_design_history_loss": [],
            "investigation_conflicts": [],
        },
        "problem_definition_review": VALID_PROBLEM_DEFINITION_REVIEW,
    }

    assert main.parse_devil_review(json.dumps(review))["problem_definition_review"] == (
        VALID_PROBLEM_DEFINITION_REVIEW
    )

    review["problem_definition_review"] = {
        "issue_claims_treated_as_facts": []
    }
    with pytest.raises(ValueError, match="problem_definition_review missing fields"):
        main.parse_devil_review(json.dumps(review))

    review["problem_definition_review"] = VALID_PROBLEM_DEFINITION_REVIEW | {
        "required_design_evidence": "Design history"
    }
    with pytest.raises(ValueError, match="required_design_evidence must be a list of strings"):
        main.parse_devil_review(json.dumps(review))


@pytest.mark.parametrize("report_name", ["A.json", "B.json"])
def test_investigate_cli_saves_report(monkeypatch, tmp_path, report_name, capsys):
    output_path = tmp_path / report_name
    changes = [{"file": "main.cpp", "original": "old", "replacement": "new"}]
    monkeypatch.setattr(
        main,
        "read_case",
        lambda case_dir: {
            "issue": "Issue",
            "logs": "Logs",
            "repository": "Repository",
            "git_history": "History",
        },
    )

    class FakeClient:
        def generate(self, prompt):
            return response(
                design_intent_status="missing",
                proposed_changes=changes,
            )

    monkeypatch.setattr(main, "OllamaClient", lambda model=None: FakeClient())
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "investigate", "examples/uefinexus", "--output", str(output_path)],
    )

    main.main()

    saved_report_text = output_path.read_text(encoding="utf-8")
    saved_report = json.loads(saved_report_text)
    assert saved_report["design_intent_status"] == "missing"
    assert saved_report["proposed_changes"] == []
    assert saved_report["patch_decision"]["status"] == "blocked"
    assert saved_report["evidence_summary"] == VALID_RESULT["evidence_summary"]
    assert saved_report["generation_metadata"] == {
        "provider": "ollama",
        "model": main.OLLAMA_DEFAULT,
        "data_policy": "standard",
    }
    expected_report = main.parse_response(
        response(
            design_intent_status="missing",
            proposed_changes=changes,
        )
    )
    expected_report["generation_metadata"] = saved_report["generation_metadata"]
    assert saved_report == expected_report

    console = capsys.readouterr().out
    for section in (
        "Evidence Summary:",
        "Observed Facts:",
        "Reporter Assumptions:",
        "Competing Interpretations:",
        "Design Intent Status:",
        "Design Intent Evidence:",
        "Hypotheses:",
        "Confidence:",
        "Recommended Investigation:",
        "Patch Decision: BLOCKED",
        "Generation Metadata:",
    ):
        assert section in console
    assert "Reason: Design intent is missing or conflicting." in console
    assert "Proposed Changes" not in console
    assert console.rstrip().endswith(f"Investigation report written to {output_path}")
    assert "None" in console


def test_devil_review_cli_loads_reports_and_saves_review(
    monkeypatch, tmp_path, capsys
):
    report_a = tmp_path / "A.json"
    report_b = tmp_path / "B.json"
    output_path = tmp_path / "C.json"
    report_a.write_text(json.dumps(VALID_RESULT | {"evidence_summary": "A"}))
    report_b.write_text(json.dumps(VALID_RESULT | {"evidence_summary": "B"}))
    review = {
        "reviews": {
            name: {
                "unsupported_assumptions": [],
                "missing_evidence": [],
                "alternative_explanations": [],
                "design_history_issues": [],
                "local_traps": [],
                "confidence_assessment": "Not assessable.",
            }
            for name in ("A", "B")
        },
        "cross_review": {
            "shared_assumptions": ["Same unsupported premise"],
            "shared_blind_spots": [],
            "possible_design_history_loss": [],
            "investigation_conflicts": [],
        },
        "problem_definition_review": VALID_PROBLEM_DEFINITION_REVIEW,
    }

    monkeypatch.setattr(
        main,
        "read_case",
        lambda case_dir: {
            "issue": "Issue evidence",
            "logs": "Log evidence",
            "repository": "Repository evidence",
            "git_history": "Git evidence",
        },
    )

    class FakeClient:
        def __init__(self, model=None):
            self.model = model

        def generate(self, prompt):
            for evidence in (
                "Issue evidence",
                "Log evidence",
                "Repository evidence",
                "Git evidence",
            ):
                assert evidence in prompt
            assert '"evidence_summary": "A"' in prompt
            assert '"evidence_summary": "B"' in prompt
            return json.dumps(review)

    monkeypatch.setattr(main, "OllamaClient", FakeClient)
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "devil-review",
            "examples/uefinexus",
            str(report_a),
            str(report_b),
            "--output",
            str(output_path),
            "--provider",
            "mistral",
            "--model",
            "mistral-test-model",
        ],
    )
    monkeypatch.setenv("MISTRAL_API_KEY", "test-secret-api-key")

    monkeypatch.setattr(main, "MistralClient", FakeClient)
    main.main()

    saved_report_text = output_path.read_text(encoding="utf-8")
    saved_report = json.loads(saved_report_text)
    assert {
        key: value
        for key, value in saved_report.items()
        if key != "generation_metadata"
    } == review
    assert saved_report["generation_metadata"] == {
        "provider": "mistral",
        "model": "mistral-test-model",
        "data_policy": "standard",
    }
    assert "test-secret-api-key" not in saved_report_text
    console = capsys.readouterr().out
    for section in (
        "Review A",
        "Review B",
        "Unsupported Assumptions:",
        "Missing Evidence:",
        "Alternative Explanations:",
        "Design History Issues:",
        "Local Traps:",
        "Confidence Assessment:",
        "Cross Review",
        "Shared Assumptions:",
        "Shared Blind Spots:",
        "Possible Design History Loss:",
        "Investigation Conflicts:",
        "Problem Definition Review",
        "Issue Claims Treated as Facts:",
        "Unverified Expected Behavior:",
        "Code / Message / Test Conflicts:",
        "Required Design Evidence:",
        "Generation Metadata:",
    ):
        assert section in console
    assert "test-secret-api-key" not in console
    assert "None" in console
    assert console.rstrip().endswith(f"Devil review written to {output_path}")


def test_investigate_output_displays_allowed_changes_without_applying(
    monkeypatch, tmp_path, capsys
):
    output_path = tmp_path / "A.json"
    changes = [
        {
            "file": "src/example.py",
            "original": "old value",
            "replacement": "new value",
            "reason": "Supported by the authoritative specification.",
        }
    ]
    monkeypatch.setattr(
        main,
        "read_case",
        lambda case_dir: {
            "issue": "Issue",
            "logs": "Logs",
            "repository": "Repository",
            "git_history": "History",
        },
    )

    class FakeClient:
        def generate(self, prompt):
            return response(
                design_intent_status="verified",
                proposed_changes=changes,
            )

    monkeypatch.setattr(main, "OllamaClient", lambda model=None: FakeClient())
    monkeypatch.setattr(
        main,
        "apply_changes",
        lambda *args: pytest.fail("Output mode must not apply proposed changes."),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "investigate",
            "case",
            "--output",
            str(output_path),
        ],
    )

    main.main()

    console = capsys.readouterr().out
    assert "Patch Decision: ALLOWED" in console
    assert "Proposed Changes (not applied):" in console
    assert "File: src/example.py" in console
    assert "Supported by the authoritative specification." in console
    assert "Original: old value" in console
    assert "Replacement: new value" in console
    assert console.rstrip().endswith(f"Investigation report written to {output_path}")
    saved_report = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved_report["proposed_changes"] == changes


def test_generation_metadata_redacts_api_key_from_model(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "test-secret-api-key")
    client = type("FakeClient", (), {"model": "model-test-secret-api-key"})()

    metadata = main._add_generation_metadata(
        {}, client, "mistral", None, "standard"
    )

    assert metadata["generation_metadata"]["model"] == "model-[redacted]"
    assert "test-secret-api-key" not in json.dumps(metadata)


def test_investigation_prompt_requires_evidence_before_changes():
    from prompts.investigation import build_investigation_prompt

    prompt = build_investigation_prompt("Issue", "Logs", "File: FrameSync.cpp")

    assert "Relevant-looking code alone is not sufficient evidence" in prompt
    assert "return an empty list" in prompt
    assert "distinguish between the leading hypotheses" in prompt
    assert "issue.md as an unverified issue report" in prompt
    assert "observed_facts" in prompt
    assert "reporter_assumptions" in prompt
    assert "design-history evidence" in prompt
    assert "design_intent_status" in prompt
    assert "design_intent_evidence" in prompt
    assert "Never label issue.md as an approved requirement" in prompt
    assert "Never invent or upgrade a source into an authoritative source type" in prompt
    assert "A behavior test is authoritative only when the test explicitly documents intended behavior" in prompt
    assert "return an empty design_intent_evidence list" in prompt


def test_interactive_loop_accept(monkeypatch, capsys):
    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr(
        main,
        "investigate",
        lambda case_dir, client, feedback, previous_result=None: VALID_RESULT.copy(),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "A")

    main.interactive_loop("case", "ollama")

    assert "Applied 0 change(s)." in capsys.readouterr().out


def test_interactive_loop_does_not_display_or_apply_blocked_changes(monkeypatch, capsys):
    changes = [{"file": "main.cpp", "original": "old", "replacement": "new"}]
    captured = {"diff_called": False, "apply_called": False}

    class FakeClient:
        def generate(self, prompt):
            return response(
                design_intent_status="conflicting",
                proposed_changes=changes,
            )

    monkeypatch.setattr(main, "read_case", lambda path: {
        "issue": "Issue",
        "logs": "Logs",
        "repository": "Repository",
        "git_history": "History",
    })
    monkeypatch.setattr(main, "OllamaClient", lambda model=None: FakeClient())
    monkeypatch.setattr("builtins.input", lambda prompt="": "A")

    def capture_diff(case_dir, received_changes):
        captured["diff_called"] = True
        pytest.fail("A blocked patch must not reach format_diff.")

    def capture_apply(case_dir, received_changes):
        captured["apply_called"] = True
        pytest.fail("A blocked patch must not reach apply_changes.")

    monkeypatch.setattr(main, "format_diff", capture_diff)
    monkeypatch.setattr(main, "apply_changes", capture_apply)

    main.interactive_loop("case", "ollama")

    output = capsys.readouterr().out
    assert captured == {"diff_called": False, "apply_called": False}
    assert "Proposed Changes" not in output
    assert "Applied 0 change(s)." in output


def test_interactive_loop_quit(monkeypatch, capsys):
    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr(
        main,
        "investigate",
        lambda case_dir, client, feedback, previous_result=None: VALID_RESULT.copy(),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "Q")

    main.interactive_loop("case", "ollama")

    assert "Investigation session ended." in capsys.readouterr().out


def test_interactive_loop_feedback(monkeypatch, capsys):
    answers = iter(["R", "New evidence", "Q"])
    feedback_values = []

    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    def fake_investigate(case_dir, client, feedback, previous_result=None):
        feedback_values.append(feedback)
        return VALID_RESULT.copy()

    monkeypatch.setattr(main, "investigate", fake_investigate)

    main.interactive_loop("case", "ollama")

    assert feedback_values == ["", "New evidence"]
    assert "Additional Evidence:" in capsys.readouterr().out


def test_interactive_loop_invalid_choice(monkeypatch, capsys):
    answers = iter(["invalid", "Q"])

    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr(
        main,
        "investigate",
        lambda case_dir, client, feedback, previous_result=None: VALID_RESULT.copy(),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    main.interactive_loop("case", "ollama")

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


def test_confidential_mistral_is_rejected_before_case_read_or_client(
    monkeypatch,
):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Confidential Mistral request reached a downstream operation.")

    monkeypatch.setattr(main, "read_case", unexpected_call)
    monkeypatch.setattr(main, "MistralClient", unexpected_call)
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "investigate",
            "missing-case",
            "--provider",
            "mistral",
            "--data-policy",
            "confidential",
        ],
    )

    with pytest.raises(SystemExit, match="Confidential data requires local Ollama"):
        main.main()


def test_confidential_mistral_devil_review_is_rejected_before_report_read(
    monkeypatch,
):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Confidential Mistral request reached a downstream operation.")

    monkeypatch.setattr(main, "read_case", unexpected_call)
    monkeypatch.setattr(main, "load_investigation_report", unexpected_call)
    monkeypatch.setattr(main, "MistralClient", unexpected_call)
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "devil-review",
            "missing-case",
            "missing-A.json",
            "missing-B.json",
            "--output",
            "missing-C.json",
            "--provider",
            "mistral",
            "--data-policy",
            "confidential",
        ],
    )

    with pytest.raises(SystemExit, match="Confidential data requires local Ollama"):
        main.main()


@pytest.mark.parametrize(
    "host", ["http://ollama.internal:11434", "http://192.0.2.10:11434"]
)
def test_confidential_remote_ollama_is_rejected_before_case_read_or_client(
    monkeypatch, host
):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Confidential remote Ollama request reached a downstream operation.")

    monkeypatch.setenv("OLLAMA_HOST", host)
    monkeypatch.setattr(main, "read_case", unexpected_call)
    monkeypatch.setattr(main, "OllamaClient", unexpected_call)
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "investigate",
            "missing-case",
            "--provider",
            "ollama",
            "--data-policy",
            "confidential",
        ],
    )

    with pytest.raises(SystemExit, match="Confidential data requires local Ollama"):
        main.main()


@pytest.mark.parametrize(
    "host",
    [
        None,
        "localhost",
        "127.0.0.1",
        "::1",
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
    ],
)
def test_confidential_local_ollama_is_allowed_and_report_has_metadata(
    monkeypatch, tmp_path, host
):
    output_path = tmp_path / "confidential.json"
    if host is None:
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_HOST", host)
    monkeypatch.setattr(
        main,
        "read_case",
        lambda case_dir: {
            "issue": "Issue",
            "logs": "Logs",
            "repository": "Repository",
            "git_history": "History",
        },
    )

    class FakeClient:
        model = "local-test-model"

        def generate(self, prompt):
            return response()

    monkeypatch.setattr(main, "OllamaClient", lambda model=None: FakeClient())
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "investigate",
            "case",
            "--output",
            str(output_path),
            "--data-policy",
            "confidential",
        ],
    )

    main.main()

    saved_report = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved_report["generation_metadata"] == {
        "provider": "ollama",
        "model": "local-test-model",
        "data_policy": "confidential",
    }


def test_interactive_loop_empty_feedback(monkeypatch, capsys):
    answers = iter(["R", "", "Q"])

    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr(
        main,
        "investigate",
        lambda case_dir, client, feedback, previous_result=None: VALID_RESULT.copy(),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    main.interactive_loop("case", "ollama")

    assert "No additional evidence was provided." in capsys.readouterr().out


def test_module_entrypoint(monkeypatch):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["main.py", "--help"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path("main.py", run_name="__main__")

    assert exc.value.code == 0


def test_interactive_loop_passes_previous_result(monkeypatch):
    answers = iter(["R", "New evidence", "Q"])
    seen = []

    monkeypatch.setattr(main, "OllamaClient", lambda model=None: object())
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    def fake_investigate(case_dir, client, feedback, previous_result=None):
        seen.append(previous_result)
        return VALID_RESULT.copy()

    monkeypatch.setattr(main, "investigate", fake_investigate)

    main.interactive_loop("case", "ollama")

    assert seen[0] is None
    assert seen[1] == main.enforce_patch_policy(VALID_RESULT)


def test_format_previous_result_includes_recommendation_and_change():
    text = main.format_previous_result(
        {
            "hypotheses": ["A", "B", "C"],
            "confidence": 70,
            "recommended_investigation": "Check X",
            "proposed_changes": [
                {"file": "a.c", "original": "old", "replacement": "new"}
            ],
        }
    )
    assert "1. A" in text
    assert "Recommended investigation: Check X" in text
    assert "Proposed change in a.c" in text
    assert main.format_previous_result(None) is None
