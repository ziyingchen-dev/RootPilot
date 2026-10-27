"""RootPilot investigation workflow.

This module reads a case directory, asks an LLM for an investigation result,
formats the findings, and optionally applies safe repository edits.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from llm.mistral_client import MistralClient, DEFAULT_MODEL as MISTRAL_DEFAULT
from llm.ollama_client import OllamaClient, DEFAULT_MODEL as OLLAMA_DEFAULT
from prompts.devil_review import build_devil_review_prompt
from prompts.investigation import build_investigation_prompt

REQUIRED_FIELDS: set[str] = {
    "evidence_summary",
    "observed_facts",
    "reporter_assumptions",
    "competing_interpretations",
    "design_intent_status",
    "hypotheses",
    "confidence",
    "recommended_investigation",
}
INVESTIGATION_LIST_FIELDS: set[str] = {
    "observed_facts",
    "reporter_assumptions",
    "competing_interpretations",
}
DESIGN_INTENT_STATUSES: set[str] = {"verified", "missing", "conflicting"}
QUALIFYING_DESIGN_SOURCE_TYPES: set[str] = {
    "specification",
    "design_document",
    "adr",
    "commit_rationale",
    "authoritative_test",
}
DEVIL_REVIEW_FIELDS: set[str] = {
    "reviews",
    "cross_review",
    "problem_definition_review",
}
DEVIL_REVIEW_CATEGORIES: set[str] = {
    "unsupported_assumptions",
    "missing_evidence",
    "alternative_explanations",
    "design_history_issues",
    "local_traps",
    "confidence_assessment",
}
CROSS_REVIEW_CATEGORIES: set[str] = {
    "shared_assumptions",
    "shared_blind_spots",
    "possible_design_history_loss",
    "investigation_conflicts",
}
PROBLEM_DEFINITION_CATEGORIES: set[str] = {
    "issue_claims_treated_as_facts",
    "unverified_expected_behavior",
    "code_message_test_conflicts",
    "required_design_evidence",
}
CXX_FILE_SUFFIXES: set[str] = {".cpp", ".cc", ".cxx"}


def _repo_path(case_dir: str | Path) -> Path:
    """Return the repository directory path for a case.

    Args:
        case_dir: Directory that contains the issue files and repo/ folder.

    Returns:
        The absolute path to the case's repository directory.
    """
    return (Path(case_dir) / "repo").resolve()


def _resolve_change_target(case_dir: str | Path, relative_path: str) -> Path | None:
    """Resolve a repository-relative file path and validate that it is safe.

    Args:
        case_dir: Case directory used to locate the repository.
        relative_path: File path as reported by the LLM, possibly prefixed by
            ``repo/``.

    Returns:
        A safe file path inside the repository, or ``None`` if the file is not
        valid or would escape the repo root.
    """
    repo_path = _repo_path(case_dir)
    normalized = str(relative_path).strip().removeprefix("repo/")
    if not normalized:
        return None

    target = (repo_path / normalized).resolve()
    if repo_path not in target.parents and target != repo_path:
        return None
    if not target.is_file():
        return None
    return target


def _validate_cpp_syntax(
    target: Path,
    modified: str,
    repo_path: Path,
) -> tuple[bool, str]:
    """Compile-check a C++ file after a proposed modification.

    Args:
        target: File path being checked.
        modified: Updated content to validate.
        repo_path: Repository root used for GCC invocation.

    Returns:
        A tuple of ``(is_valid, stderr)``. The first value is ``True`` when the
        syntax check passes or is not relevant. The second value contains the
        compiler output for rejected changes.
    """
    if target.suffix.lower() not in CXX_FILE_SUFFIXES:
        return True, ""

    check_path = target.with_name(f".{target.name}.rootpilot-check.cpp")
    try:
        check_path.write_text(modified, encoding="utf-8")
        check = subprocess.run(
            ["g++", "-std=c++17", "-fsyntax-only", str(check_path)],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        check_path.unlink(missing_ok=True)

    if check.returncode != 0 and "was not declared in this scope" not in check.stderr:
        return False, check.stderr.strip()

    return True, ""


def _prepare_change(
    case_dir: str | Path,
    change: Any,
) -> tuple[str, Path, str, str] | None:
    """Normalize a raw proposed change dictionary into internal values.

    Args:
        case_dir: Case directory used to resolve the repository path.
        change: Raw payload from the LLM response.

    Returns:
        A tuple of ``(relative_path, target, original, replacement)`` when the
        change is valid; otherwise ``None``.
    """
    if not isinstance(change, dict):
        return None

    relative_path = str(change.get("file", "")).strip().removeprefix("repo/")
    original = change.get("original", "")
    replacement = change.get("replacement", "")
    target = _resolve_change_target(case_dir, relative_path)

    if not relative_path or target is None:
        return None
    if not isinstance(original, str) or not isinstance(replacement, str):
        return None

    return relative_path, target, original, replacement


def read_repository(
    case_path: str | Path,
    max_file_size: int = 100_000,
    max_total_size: int = 1_000_000,
) -> str:
    """Read repository files into a single prompt-friendly string.

    Args:
        case_path: Path to the case directory.
        max_file_size: Maximum file size in bytes to include.
        max_total_size: Maximum total bytes to read across all files.

    Returns:
        A concatenated repository snapshot or a "No repository files found."
        message when no valid files are present.
    """
    repo_path = Path(case_path) / "repo"
    if not repo_path.is_dir():
        return "No repository files found."

    parts: list[str] = []
    total_size = 0
    for path in sorted(repo_path.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.stat().st_size > max_file_size or total_size >= max_total_size:
            continue
        try:
            content = path.read_text(encoding="utf-8")
            content = content[: max_total_size - total_size]
            total_size += len(content)
        except (OSError, UnicodeDecodeError):
            continue
        parts.append(f"File: {path.relative_to(repo_path).as_posix()}\n{content}")

    return "\n\n".join(parts) or "No repository files found."


def read_git_history(repo_path: str | Path) -> str:
    """Return the last five git commit summaries for a repository.

    Args:
        repo_path: Repository directory.

    Returns:
        The git log output when available, otherwise the fallback message.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--oneline", "-5"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "No git history available."


def read_case(case_dir: str | Path) -> dict[str, str]:
    """Read the required case inputs from disk.

    Args:
        case_dir: Directory containing ``issue.md``, ``logs.txt``, and ``repo/``.

    Returns:
        A mapping containing the issue text, logs, repository snapshot, and git
        history.

    Raises:
        FileNotFoundError: If the case directory or required files are missing.
    """
    case_path = Path(case_dir)
    required_files = {
        "issue": case_path / "issue.md",
        "logs": case_path / "logs.txt",
    }

    if not case_path.is_dir():
        raise FileNotFoundError(f"Case directory does not exist: {case_path}")

    missing = [path.name for path in required_files.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing required files: {', '.join(missing)}")

    return {
        "issue": required_files["issue"].read_text(encoding="utf-8"),
        "logs": required_files["logs"].read_text(encoding="utf-8"),
        "repository": read_repository(case_path),
        "git_history": read_git_history(case_path / "repo"),
    }


def _extract_json_object(text: str) -> str:
    """Extract the JSON object from a raw model response.

    Args:
        text: Raw response text from the model.

    Returns:
        The JSON object string.

    Raises:
        ValueError: If no JSON object exists in the response.
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError("Ollama response does not contain a JSON object.")
    return match.group()


def enforce_patch_policy(result: dict[str, Any]) -> dict[str, Any]:
    """Return an investigation result with patch actions gated on design intent."""
    if not isinstance(result, dict):
        raise ValueError("Investigation result must be an object.")

    design_intent_status = result.get("design_intent_status")
    if (
        not isinstance(design_intent_status, str)
        or design_intent_status not in DESIGN_INTENT_STATUSES
    ):
        raise ValueError(
            "Investigation design_intent_status must be one of: "
            f"{sorted(DESIGN_INTENT_STATUSES)}."
        )

    safe_result = result.copy()
    evidence = safe_result.get("design_intent_evidence")
    qualifying_evidence = isinstance(evidence, list) and bool(evidence)
    if qualifying_evidence:
        for item in evidence:
            if (
                not isinstance(item, dict)
                or set(item) != {"source_type", "reference", "claim"}
                or not all(
                    isinstance(item.get(field), str) and item[field].strip()
                    for field in ("source_type", "reference", "claim")
                )
                or item["source_type"].strip() not in QUALIFYING_DESIGN_SOURCE_TYPES
            ):
                qualifying_evidence = False
                break

            reference = item["reference"].strip().replace("\\", "/").casefold()
            untrusted_reference = re.search(
                r"(?:^|[/\s])(?:issue(?:\.md)?|logs?(?:\.txt)?|"
                r"status[_ -]?messages?(?:\.[a-z0-9]+)?|"
                r"reporter[_ -]?(?:statement|expectation)|"
                r"model[_ -]?inference|investigator[_ -]?agreement)"
                r"(?:$|[/\s?#.:_-])",
                reference,
            )
            if untrusted_reference:
                qualifying_evidence = False
                break

    if design_intent_status == "verified" and qualifying_evidence:
        safe_result.setdefault("proposed_changes", [])
        safe_result["patch_decision"] = {
            "status": "allowed",
            "reason": "Design intent is verified.",
        }
    else:
        if design_intent_status == "verified":
            safe_result["design_intent_status"] = "missing"
            safe_result["model_design_intent_status"] = design_intent_status
        safe_result["proposed_changes"] = []
        safe_result["patch_decision"] = {
            "status": "blocked",
            "reason": (
                "Authoritative design evidence is missing or incomplete."
                if design_intent_status == "verified"
                else "Design intent is missing or conflicting."
            ),
        }
    return safe_result


def parse_response(raw_response: str) -> dict[str, Any]:
    """Parse and validate the JSON response returned by an LLM.

    Args:
        raw_response: Raw text from the model.

    Returns:
        A validated dictionary containing the investigation result.

    Raises:
        ValueError: If the response is empty, invalid, or missing fields.
    """
    text = (raw_response or "").strip()
    if not text:
        raise ValueError("Ollama returned an empty response.")

    try:
        result = json.loads(_extract_json_object(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Ollama response is not valid JSON: {exc}") from exc

    if not isinstance(result, dict):
        raise ValueError("Ollama response must be a JSON object.")

    missing = REQUIRED_FIELDS - result.keys()
    if missing:
        raise ValueError(f"Ollama response missing fields: {sorted(missing)}")

    for field in INVESTIGATION_LIST_FIELDS:
        items = result[field]
        if not isinstance(items, list) or not all(
            isinstance(item, str) for item in items
        ):
            raise ValueError(f"Ollama {field} must be a list of strings.")

    if (
        not isinstance(result["design_intent_status"], str)
        or result["design_intent_status"] not in DESIGN_INTENT_STATUSES
    ):
        raise ValueError(
            "Ollama design_intent_status must be one of: "
            f"{sorted(DESIGN_INTENT_STATUSES)}."
        )

    if not isinstance(result["hypotheses"], list) or not result["hypotheses"]:
        raise ValueError("Ollama hypotheses must be a non-empty list.")

    result["hypotheses"] = [
        str(item).strip()
        for item in result["hypotheses"]
        if str(item).strip()
    ]

    if not result["hypotheses"]:
        raise ValueError("Ollama hypotheses must contain text.")

    try:
        result["confidence"] = float(result["confidence"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Ollama confidence must be numeric.") from exc

    result["evidence_summary"] = str(result["evidence_summary"]).strip()
    result["recommended_investigation"] = str(
        result["recommended_investigation"]
    ).strip()
    return enforce_patch_policy(result)


def load_investigation_report(report_path: str | Path) -> dict[str, Any]:
    """Load a JSON investigation report from disk."""
    try:
        with Path(report_path).open(encoding="utf-8") as report_file:
            report = json.load(report_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load investigation report {report_path}: {exc}") from exc

    if not isinstance(report, dict):
        raise ValueError(f"Investigation report must be a JSON object: {report_path}")
    return report


def parse_devil_review(raw_response: str) -> dict[str, Any]:
    """Parse and validate the structured devil-review response."""
    text = (raw_response or "").strip()
    if not text:
        raise ValueError("Devil review returned an empty response.")

    try:
        result = json.loads(_extract_json_object(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Devil review response is not valid JSON: {exc}") from exc

    if not isinstance(result, dict):
        raise ValueError("Devil review response must be a JSON object.")

    missing = DEVIL_REVIEW_FIELDS - result.keys()
    if missing:
        raise ValueError(f"Devil review response missing fields: {sorted(missing)}")

    reviews = result["reviews"]
    if not isinstance(reviews, dict) or set(reviews) != {"A", "B"}:
        raise ValueError("Devil review must contain separate reviews for A and B.")

    for report_name, review in reviews.items():
        if not isinstance(review, dict):
            raise ValueError(f"Devil review for {report_name} must be an object.")
        missing_categories = DEVIL_REVIEW_CATEGORIES - review.keys()
        if missing_categories:
            raise ValueError(
                f"Devil review for {report_name} missing fields: "
                f"{sorted(missing_categories)}"
            )
        for category in DEVIL_REVIEW_CATEGORIES - {"confidence_assessment"}:
            items = review[category]
            if not isinstance(items, list) or not all(
                isinstance(item, str) for item in items
            ):
                raise ValueError(
                    f"Devil review {report_name}.{category} must be a list of strings."
                )
        if not isinstance(review["confidence_assessment"], str):
            raise ValueError(
                f"Devil review {report_name}.confidence_assessment must be text."
            )

    cross_review = result["cross_review"]
    if not isinstance(cross_review, dict):
        raise ValueError("Devil review cross_review must be an object.")
    missing_categories = CROSS_REVIEW_CATEGORIES - cross_review.keys()
    if missing_categories:
        raise ValueError(
            f"Devil review cross_review missing fields: {sorted(missing_categories)}"
        )
    for category in CROSS_REVIEW_CATEGORIES:
        items = cross_review[category]
        if not isinstance(items, list) or not all(
            isinstance(item, str) for item in items
        ):
            raise ValueError(
                f"Devil review cross_review.{category} must be a list of strings."
            )

    problem_review = result["problem_definition_review"]
    if not isinstance(problem_review, dict):
        raise ValueError("Devil review problem_definition_review must be an object.")
    missing_categories = PROBLEM_DEFINITION_CATEGORIES - problem_review.keys()
    if missing_categories:
        raise ValueError(
            "Devil review problem_definition_review missing fields: "
            f"{sorted(missing_categories)}"
        )
    for category in PROBLEM_DEFINITION_CATEGORIES:
        items = problem_review[category]
        if not isinstance(items, list) or not all(
            isinstance(item, str) for item in items
        ):
            raise ValueError(
                f"Devil review problem_definition_review.{category} "
                "must be a list of strings."
            )

    return result


def devil_review(
    issue: str,
    logs: str,
    repository: str,
    git_history: str,
    report_a: dict[str, Any],
    report_b: dict[str, Any],
    client: Any,
) -> dict[str, Any]:
    """Generate an evidence-grounded devil review of two investigation reports."""
    prompt = build_devil_review_prompt(
        issue, logs, repository, git_history, report_a, report_b
    )
    return parse_devil_review(client.generate(prompt))


def save_report(report_path: str | Path, report: dict[str, Any]) -> None:
    """Write a report as readable JSON."""
    Path(report_path).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _is_local_ollama_host(host: str | None) -> bool:
    if not host or not host.strip():
        return True

    host = host.strip()
    if "://" not in host:
        if host == "::1":
            host = f"[{host}]"
        host = f"http://{host}"

    try:
        parsed = urlsplit(host)
        parsed.port
    except ValueError:
        return False

    return (
        parsed.username is None
        and parsed.password is None
        and parsed.hostname is not None
        and parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
    )


def _enforce_data_policy(provider: str, data_policy: str) -> None:
    if data_policy == "standard":
        return
    if provider == "mistral" or not _is_local_ollama_host(os.getenv("OLLAMA_HOST")):
        raise ValueError(
            "Confidential data requires local Ollama; Mistral and remote "
            "Ollama hosts are not permitted."
        )


def _add_generation_metadata(
    report: dict[str, Any],
    client: Any,
    provider: str,
    model: str | None,
    data_policy: str,
) -> dict[str, Any]:
    result = report.copy()
    model_name = (
        getattr(client, "model", None)
        or model
        or (MISTRAL_DEFAULT if provider == "mistral" else OLLAMA_DEFAULT)
    )
    api_key = os.getenv("MISTRAL_API_KEY")
    if api_key:
        model_name = model_name.replace(api_key, "[redacted]")
    result["generation_metadata"] = {
        "provider": provider,
        "model": model_name,
        "data_policy": data_policy,
    }
    return result


def format_diff(case_dir: str | Path, changes: Sequence[Any]) -> str:
    """Render a unified diff for a list of proposed file edits.

    Args:
        case_dir: Case directory.
        changes: Proposed change payloads from the LLM.

    Returns:
        A combined diff string. Empty when no valid patches are available.
    """
    if not isinstance(changes, list):
        return ""

    repo_path = _repo_path(case_dir)
    sections: list[str] = []

    for change in changes:
        normalized = _prepare_change(case_dir, change)
        if normalized is None:
            continue

        relative_path, target, original, replacement = normalized
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        if not original or content.count(original) != 1:
            continue

        modified = content.replace(original, replacement, 1)
        valid, stderr = _validate_cpp_syntax(target, modified, repo_path)
        if not valid:
            sections.append(
                f"Rejected {relative_path}: C++ syntax validation failed.\n{stderr}"
            )
            continue

        sections.append(
            "\n".join(
                difflib.unified_diff(
                    content.splitlines(),
                    modified.splitlines(),
                    fromfile=f"a/{relative_path}",
                    tofile=f"b/{relative_path}",
                    lineterm="",
                )
            )
        )

    return "\n\n".join(filter(None, sections))


def apply_changes(case_dir: str | Path, changes: Sequence[Any]) -> list[str]:
    """Apply safe changes from the LLM to the repository.

    Args:
        case_dir: Case directory.
        changes: Proposed change payloads from the LLM.

    Returns:
        A list of successfully applied relative file paths.
    """
    applied: list[str] = []
    for change in changes or []:
        normalized = _prepare_change(case_dir, change)
        if normalized is None:
            continue

        relative_path, target, original, replacement = normalized
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        if not original or content.count(original) != 1:
            continue

        # Only accept changes that keep the original content and pass syntax checks,
        # to avoid writing invalid patches into the repository.
        if not format_diff(case_dir, [change]).startswith("--- "):
            continue

        target.write_text(content.replace(original, replacement, 1), encoding="utf-8")
        applied.append(relative_path)

    return applied


def format_result(result: dict[str, Any], feedback: str) -> str:
    """Format the investigation result for interactive display.

    Args:
        result: Parsed investigation result.
        feedback: Additional evidence collected from the user.

    Returns:
        A human-readable report.
    """
    lines = [
        "Investigation Summary",
        "",
        f"Confidence: {result['confidence']:g}%",
        "",
        "Evidence Summary:",
        result["evidence_summary"],
    ]

    if feedback:
        lines.extend(["", "Additional Evidence:", feedback])

    lines.extend(["", "Hypotheses:"])
    lines.extend(
        f"{index}. {hypothesis}"
        for index, hypothesis in enumerate(result["hypotheses"], 1)
    )
    lines.extend([
        "",
        "Recommended Investigation:",
        result["recommended_investigation"],
    ])
    return "\n".join(lines)


def _format_list_section(title: str, items: Any) -> list[str]:
    lines = ["", f"{title}:"]
    if not isinstance(items, list) or not items:
        lines.append("None")
    else:
        lines.extend(f"- {item}" for item in items)
    return lines


def _format_generation_metadata(metadata: Any) -> list[str]:
    lines = ["", "Generation Metadata:"]
    if not isinstance(metadata, dict) or not metadata:
        lines.append("None")
        return lines

    api_key = os.getenv("MISTRAL_API_KEY")
    for key, value in metadata.items():
        if "key" in str(key).casefold():
            value = "[redacted]"
        elif isinstance(value, str) and api_key:
            value = value.replace(api_key, "[redacted]")
        lines.append(f"- {key}: {value}")
    return lines


def format_investigation_report(result: dict[str, Any]) -> str:
    """Render a saved investigation report without changing its JSON shape."""
    lines = [
        "Investigation Result",
        "",
        "Evidence Summary:",
        str(result.get("evidence_summary", "None")),
    ]
    for title, field in (
        ("Observed Facts", "observed_facts"),
        ("Reporter Assumptions", "reporter_assumptions"),
        ("Competing Interpretations", "competing_interpretations"),
    ):
        lines.extend(_format_list_section(title, result.get(field)))

    lines.extend(
        [
            "",
            "Design Intent Status:",
            str(result.get("design_intent_status", "None")),
        ]
    )
    if "model_design_intent_status" in result:
        lines.extend(
            [
                "Original Model Design Intent Status:",
                str(result["model_design_intent_status"]),
            ]
        )

    evidence = result.get("design_intent_evidence")
    lines.extend(["", "Design Intent Evidence:"])
    if not isinstance(evidence, list) or not evidence:
        lines.append("None")
    else:
        for item in evidence:
            if isinstance(item, dict):
                lines.extend(
                    [
                        f"- Source Type: {item.get('source_type', 'None')}",
                        f"  Reference: {item.get('reference', 'None')}",
                        f"  Claim: {item.get('claim', 'None')}",
                    ]
                )
            else:
                lines.append(f"- {item}")

    lines.extend(_format_list_section("Hypotheses", result.get("hypotheses")))
    lines.extend(
        [
            "",
            "Confidence:",
            f"{result.get('confidence', 'None')}%",
            "",
            "Recommended Investigation:",
            str(result.get("recommended_investigation", "None")),
        ]
    )

    patch_decision = result.get("patch_decision")
    if isinstance(patch_decision, dict):
        status = str(patch_decision.get("status", "unknown")).upper()
        lines.extend(["", f"Patch Decision: {status}"])
        if patch_decision.get("reason"):
            lines.append(f"Reason: {patch_decision['reason']}")
    else:
        lines.extend(["", "Patch Decision: None"])

    if (
        isinstance(patch_decision, dict)
        and patch_decision.get("status") == "allowed"
    ):
        changes = result.get("proposed_changes")
        lines.extend(["", "Proposed Changes (not applied):"])
        if not isinstance(changes, list) or not changes:
            lines.append("None")
        else:
            for index, change in enumerate(changes, 1):
                if not isinstance(change, dict):
                    lines.append(f"{index}. {change}")
                    continue
                lines.append(f"{index}. File: {change.get('file', 'None')}")
                if change.get("reason"):
                    lines.append(f"   Reason: {change['reason']}")
                if "original" in change:
                    lines.append(f"   Original: {change['original']}")
                if "replacement" in change:
                    lines.append(f"   Replacement: {change['replacement']}")
    lines.extend(_format_generation_metadata(result.get("generation_metadata")))
    return "\n".join(lines)


def format_devil_review_report(result: dict[str, Any]) -> str:
    """Render the A/B, cross-review, and problem-definition sections."""
    labels = {
        "unsupported_assumptions": "Unsupported Assumptions",
        "missing_evidence": "Missing Evidence",
        "alternative_explanations": "Alternative Explanations",
        "design_history_issues": "Design History Issues",
        "local_traps": "Local Traps",
        "shared_assumptions": "Shared Assumptions",
        "shared_blind_spots": "Shared Blind Spots",
        "possible_design_history_loss": "Possible Design History Loss",
        "investigation_conflicts": "Investigation Conflicts",
        "issue_claims_treated_as_facts": "Issue Claims Treated as Facts",
        "unverified_expected_behavior": "Unverified Expected Behavior",
        "code_message_test_conflicts": "Code / Message / Test Conflicts",
        "required_design_evidence": "Required Design Evidence",
    }
    lines = ["Devil Review"]

    for report_name in ("A", "B"):
        lines.extend(["", f"Review {report_name}"])
        review = result["reviews"][report_name]
        for field in (
            "unsupported_assumptions",
            "missing_evidence",
            "alternative_explanations",
            "design_history_issues",
            "local_traps",
        ):
            lines.extend(_format_list_section(labels[field], review.get(field)))
        lines.extend(
            [
                "",
                "Confidence Assessment:",
                review.get("confidence_assessment") or "None",
            ]
        )

    lines.extend(["", "Cross Review"])
    for field in (
        "shared_assumptions",
        "shared_blind_spots",
        "possible_design_history_loss",
        "investigation_conflicts",
    ):
        lines.extend(
            _format_list_section(
                labels[field], result["cross_review"].get(field)
            )
        )

    lines.extend(["", "Problem Definition Review"])
    for field in (
        "issue_claims_treated_as_facts",
        "unverified_expected_behavior",
        "code_message_test_conflicts",
        "required_design_evidence",
    ):
        lines.extend(
            _format_list_section(
                labels[field], result["problem_definition_review"].get(field)
            )
        )
    lines.extend(_format_generation_metadata(result.get("generation_metadata")))
    return "\n".join(lines)


def _print_investigation_result(result: dict[str, Any], feedback: Sequence[str]) -> None:
    """Render the investigation output in readable, labeled sections."""
    print("\n" + "=" * 80)
    print("Investigation Result")
    print("=" * 80)
    print(format_result(result, ""))

    if feedback:
        print("\nAdditional Evidence:")
        for index, item in enumerate(feedback, 1):
            print(f"{index}. {item}")


def _print_action_menu() -> None:
    """Print the interactive action choices in a compact, readable format."""
    print("\nActions")
    print("- [A] Accept")
    print("- [R] Provide Feedback")
    print("- [Q] Quit")


def format_previous_result(previous: dict[str, Any] | None) -> str | None:
    """Format the previous investigation result for the next prompt."""
    if not previous:
        return None
    lines = [
        f"{i}. {h}" for i, h in enumerate(previous.get("hypotheses") or [], 1)
    ]
    lines.append(f"Confidence: {previous.get('confidence')}%")
    recommended = previous.get("recommended_investigation")
    if recommended:
        lines.append(f"Recommended investigation: {recommended}")
    for change in previous.get("proposed_changes") or []:
        lines.append(
            f"Proposed change in {change.get('file')}:\n"
            f"--- original\n{change.get('original')}\n"
            f"+++ replacement\n{change.get('replacement')}"
        )
    return "\n".join(lines)


def investigate(
    case_dir: str | Path,
    client: Any,
    feedback: str,
    previous_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the end-to-end investigation workflow for a given case.

    Args:
        case_dir: Case directory containing input files.
        client: LLM client instance with a ``generate`` method.
        feedback: Prior user supplied evidence or follow-up context.
        previous_result: Parsed result of the previous round, if any.

    Returns:
        The parsed result from the model.
    """
    case = read_case(case_dir)
    prompt = build_investigation_prompt(
        case["issue"],
        case["logs"],
        case["repository"],
        case["git_history"],
        feedback or None,
        previous_result=format_previous_result(previous_result),
    )
    return parse_response(client.generate(prompt))


def interactive_loop(
    case_dir: str | Path,
    provider: str = "ollama",
    model: str | None = None,
) -> None:
    """Run the interactive CLI loop.

    Args:
        case_dir: Case directory to investigate.
        provider: LLM provider name (``ollama`` or ``mistral``).
        model: Optional model name override.
    """
    clients = {"ollama": OllamaClient, "mistral": MistralClient}
    client = clients[provider](model=model)
    feedback: list[str] = []
    result: dict[str, Any] | None = None

    while True:
        combined_feedback = "\n".join(feedback)
        result = investigate(
            case_dir, client, combined_feedback, previous_result=result
        )
        result = enforce_patch_policy(result)
        _print_investigation_result(result, feedback)

        patch_allowed = result.get("patch_decision", {}).get("status") == "allowed"
        changes = result.get("proposed_changes", [])
        diff = format_diff(case_dir, changes) if patch_allowed else ""
        if diff:
            print("\nProposed Changes")
            print("-" * 80)
            print(diff)
            print("-" * 80)

        _print_action_menu()
        choice = input("Choice: ").strip().lower()

        if choice in {"a", "accept"}:
            if not patch_allowed:
                print("\nApplied 0 change(s).")
                return
            applied = apply_changes(case_dir, changes)
            print(f"\nApplied {len(applied)} change(s).")
            return

        if choice in {"q", "quit"}:
            print("\nInvestigation session ended.")
            return

        if choice in {"r", "review", "provide feedback"}:
            new_feedback = input("Additional evidence: ").strip()
            if new_feedback:
                feedback.append(new_feedback)
            else:
                print("No additional evidence was provided.")
            continue

        print("Invalid choice. Please enter A, R, or Q.")


def main() -> None:
    """Parse CLI arguments and start an investigation or devil review."""
    parser = argparse.ArgumentParser(
        prog="rootpilot",
        description="RootPilot investigation workflow",
        epilog=(
            "Example:\n"
            "  python3 main.py investigate ./cases/demo --provider ollama\n"
            "  python3 main.py investigate ./cases/demo --output A.json\n"
            "  python3 main.py devil-review ./cases/demo A.json B.json --output C.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    commands = parser.add_subparsers(dest="command", required=True)
    investigate_parser = commands.add_parser(
        "investigate", help="Run one investigation."
    )
    investigate_parser.add_argument(
        "case_dir",
        help="Directory containing issue.md, logs.txt and repo/",
    )
    devil_review_parser = commands.add_parser(
        "devil-review", help="Review two investigation report JSON files."
    )
    devil_review_parser.add_argument(
        "case_dir",
        help="Case directory containing issue.md, logs.txt and repo/",
    )
    devil_review_parser.add_argument("report_a", help="Path to A's JSON report.")
    devil_review_parser.add_argument("report_b", help="Path to B's JSON report.")

    for command_parser in (investigate_parser, devil_review_parser):
        command_parser.add_argument(
            "--provider",
            choices=["ollama", "mistral"],
            default="ollama",
            help="LLM provider to use.",
        )
        command_parser.add_argument(
            "--model",
            help=(
                "Model name. If omitted, use the provider default. "
                "ollama: qwen2.5-coder:3b or qwen2.5:1.5b; "
                f"mistral: MISTRAL_MODEL or --model (configured default: {MISTRAL_DEFAULT}); "
                "the model must be enabled for your Mistral API account."
            ),
        )
        command_parser.add_argument(
            "--data-policy",
            choices=["standard", "confidential"],
            default="standard",
            help="Data handling policy (default: standard).",
        )
    investigate_parser.add_argument(
        "--output",
        help="Write the report as JSON instead of starting the interactive loop.",
    )
    devil_review_parser.add_argument(
        "--output",
        required=True,
        help="Path where C's JSON review report will be written.",
    )
    args = parser.parse_args()

    try:
        _enforce_data_policy(args.provider, args.data_policy)
        if args.command == "investigate":
            if args.output:
                clients = {"ollama": OllamaClient, "mistral": MistralClient}
                client = clients[args.provider](model=args.model)
                result = investigate(args.case_dir, client, "")
                report = _add_generation_metadata(
                    result, client, args.provider, args.model, args.data_policy
                )
                print(format_investigation_report(report))
                save_report(
                    args.output,
                    report,
                )
                print(f"Investigation report written to {args.output}")
            else:
                interactive_loop(
                    case_dir=args.case_dir,
                    provider=args.provider,
                    model=args.model,
                )
        else:
            clients = {"ollama": OllamaClient, "mistral": MistralClient}
            client = clients[args.provider](model=args.model)
            case = read_case(args.case_dir)
            report_a = load_investigation_report(args.report_a)
            report_b = load_investigation_report(args.report_b)
            result = devil_review(
                case["issue"],
                case["logs"],
                case["repository"],
                case["git_history"],
                report_a,
                report_b,
                client,
            )
            report = _add_generation_metadata(
                result, client, args.provider, args.model, args.data_policy
            )
            print(format_devil_review_report(report))
            save_report(
                args.output,
                report,
            )
            print(f"Devil review written to {args.output}")
    except Exception as exc:
        action = "investigation" if args.command == "investigate" else "devil review"
        raise SystemExit(f"RootPilot {action} failed: {exc}") from exc


if __name__ == "__main__":
    main()
