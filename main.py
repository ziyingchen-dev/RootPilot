import argparse
import difflib
import json
import re
import subprocess
from pathlib import Path

from llm.ollama_client import OllamaClient
from llm.mistral_client import MistralClient
from prompts.investigation import build_investigation_prompt

REQUIRED_FIELDS = {
    "evidence_summary",
    "hypotheses",
    "confidence",
    "recommended_investigation",
}

def read_repository(case_path, max_file_size=100_000, max_total_size=1_000_000):
    repo_path = case_path / "repo"
    if not repo_path.is_dir():
        return "No repository files found."

    parts = []
    total_size = 0
    for path in sorted(repo_path.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.stat().st_size > max_file_size or total_size >= max_total_size:
            continue
        try:
            content = path.read_text(encoding="utf-8")
            content = content[:max_total_size - total_size]
            total_size += len(content)
        except (OSError, UnicodeDecodeError):
            continue
        parts.append(f"File: {path.relative_to(repo_path).as_posix()}\n{content}")

    return "\n\n".join(parts) or "No repository files found."

def read_git_history(repo_path):
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--oneline", "-5"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "No git history available."

def read_case(case_dir):
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

def parse_response(raw_response):
    text = (raw_response or "").strip()
    if not text:
        raise ValueError("Ollama returned an empty response.")

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("Ollama response does not contain a JSON object.")

    try:
        result = json.loads(match.group())
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

def format_diff(case_dir, changes):
    if not isinstance(changes, list):
        return ""

    repo_path = (Path(case_dir) / "repo").resolve()
    sections = []

    for change in changes:
        if not isinstance(change, dict):
            continue

        relative_path = str(change.get("file", "")).strip()
        relative_path = relative_path.removeprefix("repo/")
        original = change.get("original", "")
        replacement = change.get("replacement", "")
        target = (repo_path / relative_path).resolve()

        if not relative_path or repo_path not in target.parents or not target.is_file():
            continue
        if not isinstance(original, str) or not isinstance(replacement, str):
            continue

        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        if not original or content.count(original) != 1:
            continue

        modified = content.replace(original, replacement, 1)

        if target.suffix.lower() in {".cpp", ".cc", ".cxx"}:
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
                sections.append(
                    f"Rejected {relative_path}: C++ syntax validation failed.\n"
                    f"{check.stderr.strip()}"
                )
                continue

        sections.append("\n".join(difflib.unified_diff(
            content.splitlines(),
            modified.splitlines(),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
            lineterm="",
        )))

    return "\n\n".join(filter(None, sections))



def apply_changes(case_dir, changes):
    repo_path = (Path(case_dir) / "repo").resolve()
    applied = []
    for change in changes or []:
        if not isinstance(change, dict):
            continue
        relative_path = str(change.get("file", "")).strip().removeprefix("repo/")
        target = (repo_path / relative_path).resolve()
        original = change.get("original", "")
        replacement = change.get("replacement", "")
        if not relative_path or repo_path not in target.parents or not target.is_file():
            continue
        if not isinstance(original, str) or not isinstance(replacement, str):
            continue
        content = target.read_text(encoding="utf-8")
        if not original or content.count(original) != 1:
            continue
        if not format_diff(case_dir, [change]).startswith("--- "):
            continue
        target.write_text(content.replace(original, replacement, 1), encoding="utf-8")
        applied.append(relative_path)
    return applied

def format_result(result, feedback):
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

def investigate(case_dir, client, feedback):
    case = read_case(case_dir)
    prompt = build_investigation_prompt(
        case["issue"],
        case["logs"],
        case["repository"],
        case["git_history"],
        feedback or None,
    )
    return parse_response(client.generate(prompt))


def interactive_loop(case_dir, model, provider="ollama"):
    clients = {"ollama": OllamaClient, "mistral": MistralClient}
    client = clients[provider](model=model)
    feedback = []

    while True:
        combined_feedback = "\n".join(feedback)
        result = investigate(case_dir, client, combined_feedback)
        print("\n" + format_result(result, combined_feedback))

        diff = format_diff(case_dir, result.get("proposed_changes", []))
        if diff:
            print("\nProposed Changes:\n")
            print(diff)

        choice = input(
            "\n[A] Accept\n[R] Provide Feedback\n[Q] Quit\nChoice: "
        ).strip().lower()

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

def main():
    parser = argparse.ArgumentParser(
        description="RootPilot investigation workflow"
    )
    parser.add_argument(
        "command",
        choices=["investigate"],
    )
    parser.add_argument(
        "case_dir",
        help="Directory containing issue.md, logs.txt and repo/",
    )
    parser.add_argument(
        "--provider",
        choices=["ollama", "mistral"],
        default="ollama",
        help="LLM provider",
    )
    parser.add_argument(
        "--model",
        help="Provider model name",
    )
    args = parser.parse_args()

    try:
        interactive_loop(args.case_dir, args.model, args.provider)
    except Exception as exc:
        raise SystemExit(f"RootPilot investigation failed: {exc}") from exc

if __name__ == "__main__":
    main()
