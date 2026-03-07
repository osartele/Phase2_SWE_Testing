from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phase2.cases import Case, load_case, load_case_by_id
from phase2.repo import REPO_INDEX_REL, resolve_repo_path
from phase2.test_runner import (
    _collect_junit_xml_paths,
    _dedupe_failures,
    _parse_console_failures,
    _parse_junit_xml_reports,
    detect_java_build_tool,
)


DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_ARTIFACTS_ROOT_REL = Path("artifacts/cases")


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    return completed.stdout.strip()


def _repo_is_clean(repo_path: Path) -> bool:
    status = _run_git(repo_path, ["status", "--porcelain"], check=False)
    return not bool(status.strip())


def _resolve_case_input(case_input: str, workdir: Path) -> tuple[Case, str]:
    candidate = Path(case_input).expanduser()
    if candidate.exists() or candidate.suffix.lower() == ".json" or os.sep in case_input:
        case_obj = load_case(candidate if candidate.is_absolute() else workdir / candidate)
        return case_obj, str((candidate if candidate.is_absolute() else (workdir / candidate)).resolve())

    case_obj = load_case_by_id(case_input, workdir=workdir)
    return case_obj, f"<index:{case_input}>"


def _copy_reports(report_paths: list[Path], repo_path: Path, stage_dir: Path) -> list[str]:
    target_root = stage_dir / "junit"
    copied: list[str] = []
    for source in report_paths:
        try:
            rel = source.resolve().relative_to(repo_path.resolve())
            target = target_root / rel
        except Exception:
            target = target_root / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(target))
    return copied


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_build_commands(
    repo_path: Path,
    commands: list[str],
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], str, str, str | None]:
    command_results: list[dict[str, Any]] = []
    all_stdout: list[str] = []
    all_stderr: list[str] = []
    execution_error: str | None = None

    for cmd in commands:
        start = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(repo_path),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                shell=True,
            )
            duration = round(time.monotonic() - start, 3)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            all_stdout.append(f"$ {cmd}\n{stdout}")
            all_stderr.append(f"$ {cmd}\n{stderr}")
            command_results.append(
                {
                    "command": cmd,
                    "exit_code": completed.returncode,
                    "duration_seconds": duration,
                }
            )
            if completed.returncode != 0:
                break
        except subprocess.TimeoutExpired:
            duration = round(time.monotonic() - start, 3)
            execution_error = f"timeout_after_seconds: {timeout_seconds}"
            command_results.append(
                {
                    "command": cmd,
                    "exit_code": -1,
                    "duration_seconds": duration,
                    "error": execution_error,
                }
            )
            break
        except Exception as exc:  # pragma: no cover - defensive path
            duration = round(time.monotonic() - start, 3)
            execution_error = f"command_error_{type(exc).__name__}: {exc}"
            command_results.append(
                {
                    "command": cmd,
                    "exit_code": -1,
                    "duration_seconds": duration,
                    "error": execution_error,
                }
            )
            break

    return command_results, "\n".join(all_stdout), "\n".join(all_stderr), execution_error


def _stage_artifact_dir(workdir: Path, case_id: str, stage: str, artifacts_root: Path) -> Path:
    root = _resolve_path(workdir, artifacts_root)
    path = root / case_id / stage
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run_case_stage(
    workdir: Path,
    repo_path: Path,
    case: Case,
    stage: str,
    commit: str,
    timeout_seconds: int,
    artifacts_root: Path,
) -> dict[str, Any]:
    stage_dir = _stage_artifact_dir(workdir, case.case_id, stage, artifacts_root)
    _run_git(repo_path, ["checkout", "--detach", commit])
    checked_out_sha = _run_git(repo_path, ["rev-parse", "HEAD"])
    _write_text(stage_dir / "commit_sha.txt", checked_out_sha + "\n")

    if not case.build_commands:
        raise ValueError(f"Case {case.case_id} has no build_commands.")

    started_epoch = time.time()
    commands, stdout_text, stderr_text, execution_error = _run_build_commands(
        repo_path=repo_path,
        commands=case.build_commands,
        timeout_seconds=timeout_seconds,
    )
    _write_text(stage_dir / "stdout.log", stdout_text)
    _write_text(stage_dir / "stderr.log", stderr_text)

    detected = detect_java_build_tool(repo_path)
    report_paths = _collect_junit_xml_paths(
        repo_path=repo_path,
        tool=detected.get("tool"),
        modified_after_epoch=started_epoch - 2.0,
    )
    copied_reports = _copy_reports(report_paths, repo_path=repo_path, stage_dir=stage_dir)
    xml_failures = _parse_junit_xml_reports(report_paths)
    console_failures = _parse_console_failures(f"{stdout_text}\n{stderr_text}")
    failing_tests = _dedupe_failures([*xml_failures, *console_failures])

    last_exit = commands[-1]["exit_code"] if commands else -1
    if execution_error is not None:
        status = "error"
        success = False
    else:
        success = last_exit == 0 and not failing_tests
        status = "pass" if success else "fail"

    result = {
        "case_id": case.case_id,
        "stage": stage,
        "status": status,
        "success": success,
        "execution_error": execution_error,
        "checked_out_commit": checked_out_sha,
        "requested_commit": commit,
        "commands": commands,
        "failing_tests": failing_tests,
        "failing_test_count": len(failing_tests),
        "report_paths_original": [str(p) for p in report_paths],
        "report_paths_copied": copied_reports,
        "stdout_path": str(stage_dir / "stdout.log"),
        "stderr_path": str(stage_dir / "stderr.log"),
        "commit_sha_path": str(stage_dir / "commit_sha.txt"),
        "run_result_path": str(stage_dir / "run_result.json"),
    }
    (stage_dir / "run_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def run_case_deterministic(
    workdir: Path,
    case_input: str,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = REPO_INDEX_REL,
    artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT_REL,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    case, case_source = _resolve_case_input(case_input, workdir=workdir)
    repo_path = resolve_repo_path(
        workdir=workdir,
        repo_id=case.repo_id,
        repos_root=repos_root,
        index_path=repo_index_path,
    )
    if not (repo_path / ".git").exists():
        raise FileNotFoundError(f"Repository not found for case repo_id={case.repo_id}: {repo_path}")

    if not _repo_is_clean(repo_path):
        raise RuntimeError(
            f"Repository has uncommitted changes and cannot run deterministically: {repo_path}"
        )

    original_sha = _run_git(repo_path, ["rev-parse", "HEAD"])
    started_at = _utc_now_iso()
    baseline_result: dict[str, Any] | None = None
    modified_result: dict[str, Any] | None = None

    try:
        baseline_result = _run_case_stage(
            workdir=workdir,
            repo_path=repo_path,
            case=case,
            stage="baseline",
            commit=case.base_commit,
            timeout_seconds=timeout_seconds,
            artifacts_root=artifacts_root,
        )
        modified_result = _run_case_stage(
            workdir=workdir,
            repo_path=repo_path,
            case=case,
            stage="modified",
            commit=case.modified_commit,
            timeout_seconds=timeout_seconds,
            artifacts_root=artifacts_root,
        )
    finally:
        _run_git(repo_path, ["checkout", "--detach", original_sha], check=False)

    root = _resolve_path(workdir, artifacts_root) / case.case_id
    summary = {
        "case_id": case.case_id,
        "case_source": case_source,
        "repo_id": case.repo_id,
        "repo_path": str(repo_path),
        "started_at_utc": started_at,
        "restored_original_sha": original_sha,
        "base_commit": case.base_commit,
        "modified_commit": case.modified_commit,
        "baseline_verified_pass": bool(baseline_result and baseline_result.get("status") == "pass"),
        "baseline": baseline_result,
        "modified": modified_result,
        "status": "ok",
    }
    root.mkdir(parents=True, exist_ok=True)
    summary_path = root / "case_run_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary
