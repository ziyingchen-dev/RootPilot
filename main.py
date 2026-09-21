"""RootPilot investigation workflow.

This module reads a case directory, asks an LLM for an investigation result,
formats the findings, and optionally applies safe repository edits.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

from llm.mistral_client import MistralClient, DEFAULT_MODEL as MISTRAL_DEFAULT
from llm.ollama_client import OllamaClient
from prompts.investigation import build_investigation_prompt

REQUIRED_FIELDS: set[str] = {
    "evidence_summary",
    "hypotheses",
    "confidence",
    "recommended_investigation",
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

    missing = REQUIRED_FIELDS - result.keys()
    if missing:
        raise ValueError(f"Ollama response missing fields: {sorted(missing)}")

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
        _print_investigation_result(result, feedback)

        diff = format_diff(case_dir, result.get("proposed_changes", []))
        if diff:
            print("\nProposed Changes")
            print("-" * 80)
            print(diff)
            print("-" * 80)

        _print_action_menu()
        choice = input("Choice: ").strip().lower()

        if choice in {"a", "accept"}:
            applied = apply_changes(case_dir, result.get("proposed_changes", []))
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
    """Parse CLI arguments and start the interactive investigation flow."""
    parser = argparse.ArgumentParser(
        prog="rootpilot",
        description="RootPilot investigation workflow",
        epilog=(
            "Example:\n"
            "  python3 main.py investigate ./cases/demo --provider ollama"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "command",
        choices=["investigate"],
        help="Action to perform.",
    )

    parser.add_argument(
        "case_dir",
        help="Directory containing issue.md, logs.txt and repo/",
    )

    parser.add_argument(
        "--provider",
        choices=["ollama", "mistral"],
        default="ollama",
        help="LLM provider to use.",
    )

    parser.add_argument(
        "--model",
        help=(
            "Model name. If omitted, use the provider default. "
            "ollama: qwen2.5-coder:3b or qwen2.5:1.5b; "
            f"mistral: MISTRAL_MODEL or --model (configured default: {MISTRAL_DEFAULT}); "
            "the model must be enabled for your Mistral API account."
        ),
    )
    args = parser.parse_args()

    try:
        interactive_loop(
            case_dir=args.case_dir,
            provider=args.provider,
            model=args.model,
        )
    except Exception as exc:
        raise SystemExit(f"RootPilot investigation failed: {exc}") from exc


if __name__ == "__main__":
    main()
