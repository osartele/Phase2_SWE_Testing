from __future__ import annotations

import csv
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from phase2.cases import list_cases
from phase2.java_parser import parse_java_source
from phase2.repo import REPO_INDEX_REL, resolve_repo_path


DEFAULT_CASES_ROOT_REL = Path("cases")
DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_ARTIFACTS_CASES_REL = Path("artifacts/cases")
DEFAULT_ARTIFACTS_HEALING_REL = Path("artifacts/healing")
DEFAULT_ARTIFACTS_REGEN_REL = Path("artifacts/regen")
DEFAULT_RQ1_CSV_REL = Path("artifacts/rq1_summary.csv")
DEFAULT_RQ1_CASES_REL = Path("artifacts/rq1_cases")

DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
WS_RE = re.compile(r"\s+")

JAVA_STOPWORDS = {
    "abstract",
    "assert",
    "boolean",
    "break",
    "byte",
    "case",
    "catch",
    "char",
    "class",
    "const",
    "continue",
    "default",
    "do",
    "double",
    "else",
    "enum",
    "extends",
    "final",
    "finally",
    "float",
    "for",
    "if",
    "goto",
    "implements",
    "import",
    "instanceof",
    "int",
    "interface",
    "long",
    "native",
    "new",
    "package",
    "private",
    "protected",
    "public",
    "return",
    "short",
    "static",
    "strictfp",
    "super",
    "switch",
    "synchronized",
    "this",
    "throw",
    "throws",
    "transient",
    "try",
    "void",
    "volatile",
    "while",
    "true",
    "false",
    "null",
}


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _run_git(repo_path: Path, args: list[str], check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo_path),
        text=True,
        capture_output=True,
        check=check,
    )
    return completed.stdout


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        return payload
    return None


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _normalize_line(line: str) -> str:
    return WS_RE.sub(" ", line.strip())


def _extract_diff_tokens(diff_text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in diff_text.splitlines():
        if raw.startswith("+++ ") or raw.startswith("--- "):
            continue
        if raw.startswith("+"):
            content = _normalize_line(raw[1:])
            if content:
                tokens.add(f"+{content}")
        elif raw.startswith("-"):
            content = _normalize_line(raw[1:])
            if content:
                tokens.add(f"-{content}")
    return tokens


def _changed_lines_for_ast(diff_text: str) -> list[str]:
    lines: list[str] = []
    for raw in diff_text.splitlines():
        if raw.startswith("+++ ") or raw.startswith("--- "):
            continue
        if raw.startswith("+") or raw.startswith("-"):
            content = raw[1:].rstrip()
            if not content.strip():
                continue
            lines.append(content)
    return lines


def _ast_signature_tokens(diff_text: str) -> set[str]:
    changed = _changed_lines_for_ast(diff_text)
    if not changed:
        return set()

    wrapper = "class PatchAstWrapper {\n  void patchMethod() {\n" + "\n".join(changed) + "\n  }\n}\n"
    parsed = parse_java_source(wrapper, path="<rq1-diff-wrapper>")
    tokens: set[str] = set()

    for method in parsed.get("methods", []):
        for invocation in method.get("invocations", []):
            callee = str(invocation.get("callee_name") or "").strip()
            if callee:
                tokens.add(callee)
        name = str(method.get("name") or "").strip()
        if name and name != "patchMethod":
            tokens.add(name)

    if tokens:
        return tokens

    for line in changed:
        for identifier in IDENTIFIER_RE.findall(line):
            if identifier in JAVA_STOPWORDS:
                continue
            tokens.add(identifier)
    return tokens


def _jaccard(left: set[str], right: set[str]) -> float | None:
    if not left and not right:
        return 1.0
    if not left and right:
        return 0.0
    if left and not right:
        return 0.0
    union = left | right
    if not union:
        return None
    return len(left & right) / len(union)


def _round_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 6)


def _parse_patch_stats(diff_text: str) -> dict[str, Any]:
    files: set[str] = set()
    added = 0
    removed = 0
    for raw in diff_text.splitlines():
        match = DIFF_FILE_RE.match(raw)
        if match:
            files.add(match.group(2))
            continue
        if raw.startswith("+++ ") or raw.startswith("--- "):
            continue
        if raw.startswith("+"):
            added += 1
        elif raw.startswith("-"):
            removed += 1
    return {
        "files_changed": len(files),
        "lines_added": added,
        "lines_removed": removed,
        "changed_files": sorted(files),
    }


def _line_similarity(agent_diff: str, human_diff: str) -> float | None:
    return _round_or_none(_jaccard(_extract_diff_tokens(agent_diff), _extract_diff_tokens(human_diff)))


def _ast_similarity(agent_diff: str, human_diff: str, enabled: bool) -> float | None:
    if not enabled:
        return None
    left = _ast_signature_tokens(agent_diff)
    right = _ast_signature_tokens(human_diff)
    return _round_or_none(_jaccard(left, right))


def _human_patch_for_case(repo_path: Path, base_commit: str, human_commit: str, test_file_path: str) -> str:
    try:
        return _run_git(
            repo_path,
            ["diff", "--no-color", "--unified=0", base_commit, human_commit, "--", test_file_path],
            check=True,
        )
    except subprocess.CalledProcessError:
        return ""


def _healing_metrics(case_id: str, artifacts_healing_root: Path) -> dict[str, Any]:
    summary_path = artifacts_healing_root / case_id / "healing_summary.json"
    summary = _read_json(summary_path)
    if summary is None:
        return {
            "available": False,
            "status": "missing_artifact",
            "pass": None,
            "time_seconds": None,
            "iterations": None,
            "patch_diff": "",
            "patch_path": None,
        }
    iterations = summary.get("iterations") if isinstance(summary.get("iterations"), list) else []
    last_iteration = iterations[-1] if iterations else {}
    patch_path = Path(str(last_iteration.get("patch_diff_path") or "")).expanduser()
    pass_value = any(bool(item.get("success")) for item in iterations)
    return {
        "available": True,
        "status": str(summary.get("status") or ""),
        "pass": bool(pass_value),
        "time_seconds": float(summary.get("duration_seconds") or 0.0),
        "iterations": len(iterations),
        "patch_diff": _read_text(patch_path) if patch_path and patch_path.exists() else "",
        "patch_path": str(patch_path) if patch_path else None,
    }


def _regen_metrics(case_id: str, artifacts_regen_root: Path) -> dict[str, Any]:
    summary_path = artifacts_regen_root / case_id / "regen_summary.json"
    summary = _read_json(summary_path)
    if summary is None:
        return {
            "available": False,
            "status": "missing_artifact",
            "pass": None,
            "time_seconds": None,
            "iterations": None,
            "patch_diff": "",
            "patch_path": None,
        }
    merged_suite_path = Path(str(summary.get("merged_suite_path") or "")).expanduser()
    patch_path = merged_suite_path / "merge_patch.diff"
    final_suite = summary.get("final_suite")
    pass_value: bool | None = None
    if isinstance(final_suite, dict):
        pass_value = bool(final_suite.get("success"))
    return {
        "available": True,
        "status": str(summary.get("status") or ""),
        "pass": pass_value,
        "time_seconds": float(summary.get("duration_seconds") or 0.0),
        "iterations": 1,
        "patch_diff": _read_text(patch_path) if patch_path.exists() else "",
        "patch_path": str(patch_path),
    }


def _human_case_run_metrics(
    case_id: str,
    human_commit: str,
    artifacts_cases_root: Path,
) -> dict[str, Any] | None:
    summary_path = artifacts_cases_root / case_id / "case_run_summary.json"
    summary = _read_json(summary_path)
    if summary is None:
        return None
    modified = summary.get("modified")
    if not isinstance(modified, dict):
        return None
    requested_commit = str(modified.get("requested_commit") or "")
    if requested_commit:
        req = requested_commit.lower()
        human = human_commit.lower()
        if not (req.startswith(human) or human.startswith(req)):
            return None

    commands = modified.get("commands")
    duration = 0.0
    if isinstance(commands, list):
        duration = float(sum(float((item or {}).get("duration_seconds") or 0.0) for item in commands))
    return {
        "available": True,
        "pass": bool(modified.get("success")),
        "status": str(modified.get("status") or ""),
        "time_seconds": round(duration, 6),
    }


def evaluate_rq1_cases(
    workdir: Path,
    cases_root: Path = DEFAULT_CASES_ROOT_REL,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = REPO_INDEX_REL,
    artifacts_cases_root: Path = DEFAULT_ARTIFACTS_CASES_REL,
    artifacts_healing_root: Path = DEFAULT_ARTIFACTS_HEALING_REL,
    artifacts_regen_root: Path = DEFAULT_ARTIFACTS_REGEN_REL,
    output_csv: Path = DEFAULT_RQ1_CSV_REL,
    output_cases_root: Path = DEFAULT_RQ1_CASES_REL,
    with_ast_similarity: bool = False,
) -> dict[str, Any]:
    resolved_cases_root = _resolve_path(workdir, cases_root)
    resolved_repos_root = _resolve_path(workdir, repos_root)
    resolved_repo_index = _resolve_path(workdir, repo_index_path)
    resolved_artifacts_cases = _resolve_path(workdir, artifacts_cases_root)
    resolved_artifacts_healing = _resolve_path(workdir, artifacts_healing_root)
    resolved_artifacts_regen = _resolve_path(workdir, artifacts_regen_root)
    resolved_output_csv = _resolve_path(workdir, output_csv)
    resolved_output_cases_root = _resolve_path(workdir, output_cases_root)
    resolved_output_cases_root.mkdir(parents=True, exist_ok=True)

    cases = list_cases(workdir=workdir, index_rel=Path(str(resolved_cases_root / "index" / "cases.jsonl")))
    rows: list[dict[str, Any]] = []
    per_case_paths: list[str] = []
    by_approach: dict[str, dict[str, int]] = {
        "healing": {"evaluated": 0, "passing": 0},
        "regen": {"evaluated": 0, "passing": 0},
    }

    for case in cases:
        metadata = case.metadata if isinstance(case.metadata, dict) else {}
        human_commit = str(metadata.get("human_update_commit") or "").strip()
        human_found = bool(metadata.get("human_update_found")) and bool(human_commit)
        if not human_found:
            continue

        repo_path = resolve_repo_path(
            workdir=workdir,
            repo_id=case.repo_id,
            repos_root=resolved_repos_root,
            index_path=resolved_repo_index,
        )
        if not (repo_path / ".git").exists():
            continue

        human_diff = _human_patch_for_case(
            repo_path=repo_path,
            base_commit=case.base_commit,
            human_commit=human_commit,
            test_file_path=case.test_file_path,
        )
        human_stats = _parse_patch_stats(human_diff)
        human_run = _human_case_run_metrics(
            case_id=case.case_id,
            human_commit=human_commit,
            artifacts_cases_root=resolved_artifacts_cases,
        )

        healing = _healing_metrics(case.case_id, resolved_artifacts_healing)
        regen = _regen_metrics(case.case_id, resolved_artifacts_regen)
        approach_map = {"healing": healing, "regen": regen}

        per_case_summary = {
            "case_id": case.case_id,
            "repo_id": case.repo_id,
            "base_commit": case.base_commit,
            "modified_commit": case.modified_commit,
            "human_update_commit": human_commit,
            "test_file_path": case.test_file_path,
            "human_run": human_run,
            "human_patch": {
                "files_changed": human_stats["files_changed"],
                "lines_added": human_stats["lines_added"],
                "lines_removed": human_stats["lines_removed"],
                "changed_files": human_stats["changed_files"],
            },
            "approaches": {},
        }

        for approach_name, metrics in approach_map.items():
            agent_diff = str(metrics.get("patch_diff") or "")
            agent_stats = _parse_patch_stats(agent_diff)
            line_sim = _line_similarity(agent_diff, human_diff) if metrics.get("available") else None
            ast_sim = (
                _ast_similarity(agent_diff, human_diff, enabled=with_ast_similarity)
                if metrics.get("available")
                else None
            )

            if metrics.get("available") and isinstance(metrics.get("pass"), bool):
                by_approach[approach_name]["evaluated"] += 1
                if metrics["pass"]:
                    by_approach[approach_name]["passing"] += 1

            row = {
                "case_id": case.case_id,
                "repo_id": case.repo_id,
                "approach": approach_name,
                "human_update_commit": human_commit,
                "human_pass": human_run.get("pass") if human_run else None,
                "human_time_seconds": human_run.get("time_seconds") if human_run else None,
                "available": bool(metrics.get("available")),
                "status": metrics.get("status"),
                "pass": metrics.get("pass"),
                "time_seconds": metrics.get("time_seconds"),
                "iterations": metrics.get("iterations"),
                "agent_files_changed": agent_stats["files_changed"],
                "agent_lines_added": agent_stats["lines_added"],
                "agent_lines_removed": agent_stats["lines_removed"],
                "human_files_changed": human_stats["files_changed"],
                "human_lines_added": human_stats["lines_added"],
                "human_lines_removed": human_stats["lines_removed"],
                "patch_line_similarity": line_sim,
                "patch_ast_similarity": ast_sim,
                "agent_patch_path": metrics.get("patch_path"),
            }
            rows.append(row)

            per_case_summary["approaches"][approach_name] = {
                "available": bool(metrics.get("available")),
                "status": metrics.get("status"),
                "pass": metrics.get("pass"),
                "time_seconds": metrics.get("time_seconds"),
                "iterations": metrics.get("iterations"),
                "agent_patch_path": metrics.get("patch_path"),
                "agent_patch": {
                    "files_changed": agent_stats["files_changed"],
                    "lines_added": agent_stats["lines_added"],
                    "lines_removed": agent_stats["lines_removed"],
                    "changed_files": agent_stats["changed_files"],
                },
                "similarity": {
                    "line_diff_jaccard": line_sim,
                    "ast_similarity_optional": ast_sim,
                    "ast_similarity_enabled": with_ast_similarity,
                },
            }

        case_path = resolved_output_cases_root / f"{case.case_id}.json"
        case_path.write_text(json.dumps(per_case_summary, indent=2, sort_keys=True), encoding="utf-8")
        per_case_paths.append(str(case_path))

    resolved_output_csv.parent.mkdir(parents=True, exist_ok=True)
    csv_columns = [
        "case_id",
        "repo_id",
        "approach",
        "human_update_commit",
        "human_pass",
        "human_time_seconds",
        "available",
        "status",
        "pass",
        "time_seconds",
        "iterations",
        "agent_files_changed",
        "agent_lines_added",
        "agent_lines_removed",
        "human_files_changed",
        "human_lines_added",
        "human_lines_removed",
        "patch_line_similarity",
        "patch_ast_similarity",
        "agent_patch_path",
    ]
    with resolved_output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    pass_rates: dict[str, float | None] = {}
    for approach, stats in by_approach.items():
        evaluated = stats["evaluated"]
        passing = stats["passing"]
        pass_rates[approach] = round(passing / evaluated, 6) if evaluated > 0 else None

    return {
        "status": "ok",
        "cases_total": len(cases),
        "cases_with_human_updates": len({row["case_id"] for row in rows}),
        "rows_written": len(rows),
        "output_csv": str(resolved_output_csv),
        "per_case_dir": str(resolved_output_cases_root),
        "per_case_paths": per_case_paths,
        "with_ast_similarity": with_ast_similarity,
        "approach_pass_rates": pass_rates,
        "approach_counts": by_approach,
    }
