from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phase2.case_runner import _repo_is_clean, _resolve_case_input, _resolve_path, _run_git
from phase2.evosuite import (
    build_project_test_classpath,
    focal_class_fqcn,
    prepare_project_for_evosuite,
    resolve_evosuite_jar_path,
    run_evosuite_generation,
    verify_evosuite_runtime,
)
from phase2.repo import REPO_INDEX_REL, resolve_repo_path
from phase2.test_runner import (
    _collect_junit_xml_paths,
    _dedupe_failures,
    _parse_console_failures,
    _parse_junit_xml_reports,
    detect_java_build_tool,
)


DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_REGEN_ROOT_REL = Path("artifacts/regen")
DEFAULT_SEED = 1337

PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)


@dataclass(slots=True)
class GeneratedCandidate:
    package_name: str
    fqcn: str
    class_name: str
    test_file: Path
    scaffolding_file: Path | None
    content_hash: str


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_package_name(java_text: str) -> str:
    match = PACKAGE_RE.search(java_text)
    if not match:
        return ""
    return match.group(1).strip()


def _normalize_method_identifier(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    text = text.split("(", 1)[0]
    return text.strip()


def _focal_class_fqcn(repo_path: Path, focal_file_path: str) -> str:
    focal_path = repo_path / focal_file_path
    if not focal_path.exists():
        raise FileNotFoundError(f"Focal file not found at modified commit: {focal_path}")
    source = focal_path.read_text(encoding="utf-8", errors="replace")
    package_name = _parse_package_name(source)
    class_name = focal_path.stem
    return f"{package_name}.{class_name}" if package_name else class_name


def _infer_test_source_root(repo_path: Path, test_file_path: str) -> Path:
    normalized = test_file_path.replace("\\", "/")
    marker = "src/test/java"
    if marker in normalized:
        prefix = normalized.split(marker, 1)[0].strip("/")
        root = repo_path / prefix / marker if prefix else repo_path / marker
        return root.resolve()

    case_path = (repo_path / test_file_path).resolve()
    if case_path.exists():
        for parent in case_path.parents:
            as_posix = parent.as_posix()
            if as_posix.endswith(marker):
                return parent

    candidates = sorted(repo_path.glob("**/src/test/java"), key=lambda p: len(str(p)))
    if candidates:
        return candidates[0].resolve()
    return (repo_path / marker).resolve()


def _tool_runner(tool_info: dict[str, Any]) -> str:
    command = tool_info.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError(f"Build tool command not detected: {tool_info}")
    runner = str(command[0]).strip()
    if not runner:
        raise ValueError(f"Build tool runner is empty: {tool_info}")
    return runner


def _run_command(
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
            "error": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "command": command,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "error": f"timeout_after_seconds: {timeout_seconds}",
            "duration_seconds": round(time.monotonic() - started, 3),
            "command": command,
        }
    except Exception as exc:  # pragma: no cover
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "error": f"command_error_{type(exc).__name__}: {exc}",
            "duration_seconds": round(time.monotonic() - started, 3),
            "command": command,
        }


def _prepare_build_command(tool: str, runner: str) -> list[str]:
    if tool == "maven":
        return [runner, "-B", "-DskipTests", "test-compile"]
    if tool == "gradle":
        return [runner, "testClasses", "-x", "test", "--console=plain"]
    raise ValueError(f"Unsupported build tool for regen: {tool}")


def _maven_dependency_classpath(
    repo_path: Path,
    runner: str,
    output_file: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        runner,
        "-B",
        "-q",
        "-DskipTests",
        "-DincludeScope=test",
        "dependency:build-classpath",
        f"-Dmdep.outputFile={output_file}",
    ]
    return _run_command(command=command, cwd=repo_path, timeout_seconds=timeout_seconds)


def _collect_classpath_entries(repo_path: Path, tool: str, maven_cp_file: Path) -> list[str]:
    entries: list[str] = []
    seen: set[str] = set()

    if tool == "maven":
        for path in sorted(repo_path.glob("**/target/classes")):
            text = str(path.resolve())
            if text not in seen:
                seen.add(text)
                entries.append(text)
        for path in sorted(repo_path.glob("**/target/test-classes")):
            text = str(path.resolve())
            if text not in seen:
                seen.add(text)
                entries.append(text)
    elif tool == "gradle":
        patterns = [
            "**/build/classes/java/main",
            "**/build/resources/main",
            "**/build/classes/java/test",
            "**/build/resources/test",
            "**/build/libs/*.jar",
        ]
        for pattern in patterns:
            for path in sorted(repo_path.glob(pattern)):
                text = str(path.resolve())
                if text not in seen:
                    seen.add(text)
                    entries.append(text)

    if maven_cp_file.exists():
        raw = maven_cp_file.read_text(encoding="utf-8", errors="replace").strip()
        if raw:
            for piece in raw.split(os.pathsep):
                candidate = piece.strip()
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    entries.append(candidate)
    return entries


def _collect_generated_candidates(generated_root: Path) -> list[GeneratedCandidate]:
    if not generated_root.exists():
        return []

    candidates: list[GeneratedCandidate] = []
    for test_file in sorted(generated_root.rglob("*_ESTest.java")):
        if test_file.name.endswith("_ESTest_scaffolding.java"):
            continue
        text = test_file.read_text(encoding="utf-8", errors="replace")
        package_name = _parse_package_name(text)
        class_name = test_file.stem
        fqcn = f"{package_name}.{class_name}" if package_name else class_name
        scaffolding_file = test_file.with_name(f"{class_name}_scaffolding.java")
        scaffolding = scaffolding_file if scaffolding_file.exists() else None

        payload = text
        if scaffolding is not None:
            payload += "\n" + scaffolding.read_text(encoding="utf-8", errors="replace")
        candidates.append(
            GeneratedCandidate(
                package_name=package_name,
                fqcn=fqcn,
                class_name=class_name,
                test_file=test_file,
                scaffolding_file=scaffolding,
                content_hash=_hash_text(payload),
            )
        )
    return candidates


def _candidate_matches_mapper_scope(candidate: GeneratedCandidate, mapped_focal_method: str) -> bool:
    target = _normalize_method_identifier(mapped_focal_method)
    if not target:
        return True
    text = candidate.test_file.read_text(encoding="utf-8", errors="replace")
    return f"{target}(" in text or f".{target}(" in text


def _dedupe_candidates(candidates: list[GeneratedCandidate]) -> list[GeneratedCandidate]:
    deduped: list[GeneratedCandidate] = []
    seen_hashes: set[str] = set()
    for candidate in candidates:
        if candidate.content_hash in seen_hashes:
            continue
        seen_hashes.add(candidate.content_hash)
        deduped.append(candidate)
    return deduped


def _candidate_destination_paths(candidate: GeneratedCandidate, test_root: Path) -> list[tuple[Path, Path]]:
    package_rel = Path(candidate.package_name.replace(".", "/")) if candidate.package_name else Path(".")
    destinations = [(candidate.test_file, test_root / package_rel / candidate.test_file.name)]
    if candidate.scaffolding_file is not None:
        destinations.append(
            (
                candidate.scaffolding_file,
                test_root / package_rel / candidate.scaffolding_file.name,
            )
        )
    return destinations


def _copy_candidate_into_repo(candidate: GeneratedCandidate, test_root: Path) -> dict[str, Any]:
    destinations = _candidate_destination_paths(candidate, test_root)
    conflicts: list[str] = []
    already_same: list[str] = []
    to_create: list[tuple[Path, Path]] = []
    for source, target in destinations:
        if target.exists():
            if _hash_file(source) == _hash_file(target):
                already_same.append(str(target))
            else:
                conflicts.append(str(target))
        else:
            to_create.append((source, target))

    if conflicts:
        return {
            "accepted": False,
            "reason": "path_conflict_existing_test",
            "conflicts": conflicts,
            "already_same": already_same,
            "created_files": [],
        }

    created_files: list[str] = []
    for source, target in to_create:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        created_files.append(str(target))

    if not created_files:
        return {
            "accepted": False,
            "reason": "duplicate_existing_same_content",
            "conflicts": [],
            "already_same": already_same,
            "created_files": [],
        }

    return {
        "accepted": True,
        "reason": "copied",
        "conflicts": [],
        "already_same": already_same,
        "created_files": created_files,
    }


def _remove_files_and_empty_parents(paths: list[Path], stop_at: Path) -> None:
    for path in sorted(paths, key=lambda p: len(str(p)), reverse=True):
        if path.exists():
            path.unlink()
        parent = path.parent
        while True:
            if parent == stop_at or parent == parent.parent:
                break
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _targeted_test_command(tool: str, runner: str, fqcn: str) -> list[str]:
    if tool == "maven":
        return [runner, "-B", f"-Dtest={fqcn}", "test"]
    if tool == "gradle":
        return [runner, "test", "--tests", fqcn, "--console=plain"]
    raise ValueError(f"Unsupported tool for targeted tests: {tool}")


def _run_targeted_candidate_test(
    repo_path: Path,
    tool: str,
    runner: str,
    fqcn: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    started_epoch = time.time()
    command = _targeted_test_command(tool=tool, runner=runner, fqcn=fqcn)
    run = _run_command(command=command, cwd=repo_path, timeout_seconds=timeout_seconds)
    combined = f"{run['stdout']}\n{run['stderr']}".strip()

    report_paths = _collect_junit_xml_paths(
        repo_path=repo_path,
        tool=tool,
        modified_after_epoch=started_epoch - 2.0,
    )
    xml_failures = _parse_junit_xml_reports(report_paths)
    console_failures = _parse_console_failures(combined)
    failing_tests = _dedupe_failures([*xml_failures, *console_failures])
    success = bool(run["exit_code"] == 0 and not failing_tests and run["error"] is None)
    return {
        "command": command,
        "exit_code": run["exit_code"],
        "error": run["error"],
        "duration_seconds": run["duration_seconds"],
        "success": success,
        "stdout": run["stdout"],
        "stderr": run["stderr"],
        "failing_tests": failing_tests,
        "failing_test_count": len(failing_tests),
        "report_paths": [str(p) for p in report_paths],
    }


def _run_suite_commands(
    repo_path: Path,
    commands: list[str],
    timeout_seconds: int,
) -> dict[str, Any]:
    command_results: list[dict[str, Any]] = []
    all_stdout: list[str] = []
    all_stderr: list[str] = []
    execution_error: str | None = None
    started_epoch = time.time()

    for cmd in commands:
        started = time.monotonic()
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
            all_stdout.append(f"$ {cmd}\n{completed.stdout or ''}")
            all_stderr.append(f"$ {cmd}\n{completed.stderr or ''}")
            command_results.append(
                {
                    "command": cmd,
                    "exit_code": completed.returncode,
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
            if completed.returncode != 0:
                break
        except subprocess.TimeoutExpired:
            execution_error = f"timeout_after_seconds: {timeout_seconds}"
            command_results.append(
                {
                    "command": cmd,
                    "exit_code": -1,
                    "error": execution_error,
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
            break
        except Exception as exc:  # pragma: no cover
            execution_error = f"command_error_{type(exc).__name__}: {exc}"
            command_results.append(
                {
                    "command": cmd,
                    "exit_code": -1,
                    "error": execution_error,
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
            break

    stdout_text = "\n".join(all_stdout)
    stderr_text = "\n".join(all_stderr)
    combined = f"{stdout_text}\n{stderr_text}".strip()
    detected = detect_java_build_tool(repo_path)
    tool = str(detected.get("tool") or "")
    report_paths = _collect_junit_xml_paths(
        repo_path=repo_path,
        tool=tool or None,
        modified_after_epoch=started_epoch - 2.0,
    )
    xml_failures = _parse_junit_xml_reports(report_paths)
    console_failures = _parse_console_failures(combined)
    failing_tests = _dedupe_failures([*xml_failures, *console_failures])
    last_exit = command_results[-1]["exit_code"] if command_results else -1

    if execution_error is not None:
        status = "error"
        success = False
    else:
        success = bool(last_exit == 0 and not failing_tests)
        status = "pass" if success else "fail"
    return {
        "status": status,
        "success": success,
        "execution_error": execution_error,
        "commands": command_results,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "report_paths": [str(p) for p in report_paths],
        "failing_tests": failing_tests,
        "failing_test_count": len(failing_tests),
    }


def run_regenerative_sync(
    workdir: Path,
    case_input: str,
    config: dict[str, Any] | None = None,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = REPO_INDEX_REL,
    artifacts_root: Path = DEFAULT_REGEN_ROOT_REL,
    evosuite_jar: Path | None = None,
    download_evosuite: bool = False,
    evosuite_download_url: str | None = None,
    force_download: bool = False,
    java_bin: str = "java",
    seed: int = DEFAULT_SEED,
    budget_seconds: int = 120,
    timeout_seconds: int = 1800,
    use_mapper_scope: bool = False,
    max_minutes: int = 30,
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
        raise RuntimeError(f"Repository is dirty; aborting regenerative run: {repo_path}")

    root = _resolve_path(workdir, artifacts_root) / case.case_id
    if root.exists():
        shutil.rmtree(root)
    logs_dir = root / "logs"
    generated_dir = root / "generated_tests"
    filtered_dir = root / "filtered_tests"
    merged_dir = root / "merged_suite"
    logs_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)
    filtered_dir.mkdir(parents=True, exist_ok=True)
    merged_dir.mkdir(parents=True, exist_ok=True)

    jar_info = resolve_evosuite_jar_path(
        workdir=workdir,
        config=config,
        jar_path=evosuite_jar,
        download=download_evosuite,
        download_url=evosuite_download_url,
        force_download=force_download,
    )
    jar_path = Path(jar_info["jar_path"])
    runtime_info = verify_evosuite_runtime(java_bin=java_bin, jar_path=jar_path)

    original_sha = _run_git(repo_path, ["rev-parse", "HEAD"])
    started_at = _utc_now_iso()
    start_monotonic = time.monotonic()
    max_seconds = max(1, max_minutes * 60)
    created_repo_files: set[Path] = set()
    checked_out_modified_sha: str | None = None
    stop_reason = "unknown"

    summary: dict[str, Any] = {
        "case_id": case.case_id,
        "case_source": case_source,
        "repo_id": case.repo_id,
        "repo_path": str(repo_path),
        "seed": seed,
        "budget_seconds": budget_seconds,
        "max_minutes": max_minutes,
        "use_mapper_scope": use_mapper_scope,
        "evosuite_jar": str(jar_path),
        "evosuite_jar_source": jar_info["source"],
        "evosuite_jar_downloaded": jar_info["downloaded"],
        "evosuite_jar_cache_hit": jar_info["used_cache"],
        "java_runtime": {
            "java_bin": runtime_info["java_bin"],
            "version_text": runtime_info["java_version_text"],
            "major": runtime_info["java_major"],
        },
        "status": "error",
        "started_at_utc": started_at,
    }

    try:
        _run_git(repo_path, ["checkout", "--detach", case.modified_commit])
        checked_out_modified_sha = _run_git(repo_path, ["rev-parse", "HEAD"])
        if not checked_out_modified_sha.startswith(case.modified_commit):
            raise RuntimeError(
                f"Resolved modified commit '{checked_out_modified_sha}' does not match requested "
                f"'{case.modified_commit}'."
            )

        prepare = prepare_project_for_evosuite(
            repo_path=repo_path,
            timeout_seconds=timeout_seconds,
        )
        tool = prepare["tool"]
        runner = prepare["runner"]
        test_root = _infer_test_source_root(repo_path, case.test_file_path)
        test_root.mkdir(parents=True, exist_ok=True)

        (logs_dir / "prepare_build_stdout.log").write_text(prepare["stdout"], encoding="utf-8")
        (logs_dir / "prepare_build_stderr.log").write_text(prepare["stderr"], encoding="utf-8")
        if not prepare["ok"]:
            raise RuntimeError(
                "Project build preparation failed before EvoSuite generation.\n"
                f"Tool: {prepare['tool']}\n"
                f"Command: {' '.join(prepare['command'])}\n"
                f"Exit code: {prepare['exit_code']}\n"
                f"Error: {prepare['error']}\n"
                f"See logs: {logs_dir / 'prepare_build_stderr.log'}"
            )

        classpath_info = build_project_test_classpath(
            repo_path=repo_path,
            tool=tool,
            runner=runner,
            logs_dir=logs_dir,
            timeout_seconds=timeout_seconds,
        )
        classpath_entries = classpath_info["classpath_entries"]
        classpath = classpath_info["classpath"]
        focal_fqcn = focal_class_fqcn(repo_path=repo_path, focal_file_path=case.focal_file_path)

        evosuite = run_evosuite_generation(
            repo_path=repo_path,
            java_bin=java_bin,
            jar_path=jar_path,
            focal_fqcn=focal_fqcn,
            classpath=classpath,
            generated_dir=generated_dir,
            seed=seed,
            time_budget_seconds=budget_seconds,
            timeout_seconds=timeout_seconds,
        )
        (logs_dir / "evosuite_stdout.log").write_text(evosuite["stdout"], encoding="utf-8")
        (logs_dir / "evosuite_stderr.log").write_text(evosuite["stderr"], encoding="utf-8")
        if not evosuite["ok"]:
            raise RuntimeError(
                "EvoSuite generation failed.\n"
                f"Command: {' '.join(evosuite['command'])}\n"
                f"Exit code: {evosuite['exit_code']}\n"
                f"Error: {evosuite['error']}\n"
                f"See logs: {logs_dir / 'evosuite_stderr.log'}"
            )

        raw_candidates = _collect_generated_candidates(generated_root=generated_dir)
        candidates = _dedupe_candidates(raw_candidates)
        candidate_results: list[dict[str, Any]] = []
        kept_candidates: list[GeneratedCandidate] = []

        filter_logs = logs_dir / "filter"
        filter_logs.mkdir(parents=True, exist_ok=True)

        for idx, candidate in enumerate(candidates, start=1):
            if (time.monotonic() - start_monotonic) > max_seconds:
                stop_reason = "time_budget_exhausted"
                break
            if use_mapper_scope and not _candidate_matches_mapper_scope(
                candidate,
                case.mapped_focal_method,
            ):
                candidate_results.append(
                    {
                        "index": idx,
                        "fqcn": candidate.fqcn,
                        "class_name": candidate.class_name,
                        "content_hash": candidate.content_hash,
                        "merge": {
                            "accepted": False,
                            "reason": "scope_limiter_no_mapped_focal_method_reference",
                            "conflicts": [],
                            "already_same": [],
                            "created_files": [],
                        },
                        "accepted": False,
                        "reason": "scope_limiter_no_mapped_focal_method_reference",
                    }
                )
                continue

            merge_result = _copy_candidate_into_repo(candidate=candidate, test_root=test_root)
            created = [Path(path) for path in merge_result["created_files"]]
            for path in created:
                created_repo_files.add(path)

            result: dict[str, Any] = {
                "index": idx,
                "fqcn": candidate.fqcn,
                "class_name": candidate.class_name,
                "content_hash": candidate.content_hash,
                "merge": merge_result,
                "accepted": False,
                "reason": "",
            }

            if not merge_result["accepted"]:
                result["reason"] = str(merge_result["reason"])
                candidate_results.append(result)
                continue

            targeted = _run_targeted_candidate_test(
                repo_path=repo_path,
                tool=tool,
                runner=runner,
                fqcn=candidate.fqcn,
                timeout_seconds=timeout_seconds,
            )
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate.class_name)
            (filter_logs / f"{idx:03d}_{safe_name}_stdout.log").write_text(
                targeted["stdout"], encoding="utf-8"
            )
            (filter_logs / f"{idx:03d}_{safe_name}_stderr.log").write_text(
                targeted["stderr"], encoding="utf-8"
            )
            result["targeted_test"] = {
                "command": targeted["command"],
                "exit_code": targeted["exit_code"],
                "error": targeted["error"],
                "success": targeted["success"],
                "duration_seconds": targeted["duration_seconds"],
                "failing_test_count": targeted["failing_test_count"],
                "failing_tests": targeted["failing_tests"],
                "report_paths": targeted["report_paths"],
            }
            if targeted["success"]:
                result["accepted"] = True
                result["reason"] = "passes_targeted_execution"
                kept_candidates.append(candidate)
            else:
                result["accepted"] = False
                result["reason"] = "fails_targeted_execution"
                _remove_files_and_empty_parents(created, stop_at=test_root)
                for path in created:
                    created_repo_files.discard(path)
            candidate_results.append(result)

        kept_manifest: list[dict[str, Any]] = []
        for candidate in kept_candidates:
            package_rel = Path(candidate.package_name.replace(".", "/")) if candidate.package_name else Path(".")
            kept_paths: list[Path] = [test_root / package_rel / candidate.test_file.name]
            if candidate.scaffolding_file is not None:
                kept_paths.append(test_root / package_rel / candidate.scaffolding_file.name)
            for path in kept_paths:
                if not path.exists():
                    continue
                rel_to_root = path.resolve().relative_to(test_root.resolve())
                target = filtered_dir / rel_to_root
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            kept_manifest.append(
                {
                    "fqcn": candidate.fqcn,
                    "class_name": candidate.class_name,
                    "content_hash": candidate.content_hash,
                    "files": [str(p.resolve()) for p in kept_paths if p.exists()],
                }
            )

        final_suite = _run_suite_commands(
            repo_path=repo_path,
            commands=case.build_commands,
            timeout_seconds=timeout_seconds,
        )
        (logs_dir / "merged_suite_stdout.log").write_text(final_suite["stdout"], encoding="utf-8")
        (logs_dir / "merged_suite_stderr.log").write_text(final_suite["stderr"], encoding="utf-8")

        if test_root.exists():
            shutil.copytree(test_root, merged_dir / "test_root_snapshot", dirs_exist_ok=True)
        diff_paths = [str(path.resolve().relative_to(repo_path.resolve())).replace("\\", "/") for path in created_repo_files]
        if diff_paths:
            diff_text = _run_git(repo_path, ["diff", "--no-color", "--", *sorted(diff_paths)], check=False)
        else:
            diff_text = ""
        (merged_dir / "merge_patch.diff").write_text(diff_text, encoding="utf-8")

        (root / "candidate_results.json").write_text(
            json.dumps(candidate_results, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (root / "kept_generated.json").write_text(
            json.dumps(kept_manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        summary.update(
            {
                "status": "ok",
                "build_tool": tool,
                "runner": runner,
                "focal_class_fqcn": focal_fqcn,
                "test_root": str(test_root),
                "prepare_build": {
                    "command": prepare["command"],
                    "exit_code": prepare["exit_code"],
                    "error": prepare["error"],
                    "duration_seconds": prepare["duration_seconds"],
                },
                "dependency_classpath": {
                    "command": classpath_info["command"],
                    "classpath_path": classpath_info["classpath_path"],
                    "classpath_entries_count": len(classpath_entries),
                },
                "evosuite": {
                    "command": evosuite["command"],
                    "exit_code": evosuite["exit_code"],
                    "error": evosuite["error"],
                    "duration_seconds": evosuite["duration_seconds"],
                },
                "generated_candidate_count": len(raw_candidates),
                "deduped_candidate_count": len(candidates),
                "kept_candidate_count": len(kept_candidates),
                "candidate_results_path": str(root / "candidate_results.json"),
                "kept_generated_path": str(root / "kept_generated.json"),
                "final_suite": {
                    "status": final_suite["status"],
                    "success": final_suite["success"],
                    "execution_error": final_suite["execution_error"],
                    "failing_test_count": final_suite["failing_test_count"],
                    "failing_tests": final_suite["failing_tests"],
                    "report_paths": final_suite["report_paths"],
                },
                "merged_suite_path": str(merged_dir),
                "logs_path": str(logs_dir),
                "generated_tests_path": str(generated_dir),
                "filtered_tests_path": str(filtered_dir),
            }
        )
        if stop_reason == "time_budget_exhausted":
            summary["status"] = "partial"
        elif bool(final_suite["success"]):
            stop_reason = "success"
        elif len(kept_candidates) == 0:
            stop_reason = "no_progress_no_candidates"
        elif not diff_text.strip():
            stop_reason = "no_progress_no_patch"
        else:
            stop_reason = "suite_still_failing"
    finally:
        if created_repo_files:
            _remove_files_and_empty_parents(list(created_repo_files), stop_at=repo_path)
        _run_git(repo_path, ["checkout", "--detach", original_sha], check=False)

    summary["checked_out_modified_sha"] = checked_out_modified_sha
    summary["restored_original_sha"] = original_sha
    summary["duration_seconds"] = round(time.monotonic() - start_monotonic, 3)
    if (time.monotonic() - start_monotonic) > max_seconds and stop_reason == "unknown":
        stop_reason = "time_budget_exhausted"
    if stop_reason == "unknown":
        stop_reason = "completed"
    summary["stop_reason"] = stop_reason
    summary_path = root / "regen_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary
