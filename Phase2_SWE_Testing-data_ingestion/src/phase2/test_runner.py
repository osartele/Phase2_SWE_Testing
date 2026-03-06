import json
import logging
import os
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)

DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_REPO_INDEX_REL = Path("phase2/data/processed/repo_index.json")


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _load_repo_index(index_path: Path) -> dict[str, Any]:
    if not index_path.exists():
        return {"repos": {}}
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid repo index file: {index_path}")
    repos = payload.get("repos")
    if repos is None:
        payload["repos"] = {}
    elif not isinstance(repos, dict):
        raise ValueError(f"Invalid repo index file: {index_path}")
    return payload


def _repo_path_for_id(workdir: Path, repo_id: str, repos_root: Path, repo_index_path: Path) -> Path:
    resolved_repos_root = _resolve_path(workdir, repos_root)
    resolved_repo_index = _resolve_path(workdir, repo_index_path)

    index = _load_repo_index(resolved_repo_index)
    entry = index.get("repos", {}).get(repo_id)
    if isinstance(entry, dict):
        path_value = entry.get("path")
        if isinstance(path_value, str) and path_value.strip():
            path = Path(path_value).expanduser()
            if not path.is_absolute():
                path = (workdir / path).resolve()
            return path
    return resolved_repos_root / repo_id


def _is_windows() -> bool:
    return os.name == "nt"


def detect_java_build_tool(repo_path: Path) -> dict[str, Any]:
    mvnw = repo_path / ("mvnw.cmd" if _is_windows() else "mvnw")
    gradlew = repo_path / ("gradlew.bat" if _is_windows() else "gradlew")
    has_maven_project = (repo_path / "pom.xml").exists()
    has_gradle_project = (repo_path / "build.gradle").exists() or (repo_path / "build.gradle.kts").exists()

    if mvnw.exists():
        return {"tool": "maven", "runner": "wrapper", "command": [str(mvnw), "-B", "test"]}
    if gradlew.exists():
        return {"tool": "gradle", "runner": "wrapper", "command": [str(gradlew), "test", "--console=plain"]}
    if has_maven_project:
        return {"tool": "maven", "runner": "system", "command": ["mvn", "-B", "test"]}
    if has_gradle_project:
        return {"tool": "gradle", "runner": "system", "command": ["gradle", "test", "--console=plain"]}
    return {"tool": None, "runner": None, "command": []}


def _collect_junit_xml_paths(
    repo_path: Path,
    tool: str | None,
    modified_after_epoch: float | None = None,
) -> list[Path]:
    candidate_dirs: list[Path] = []
    if tool == "maven":
        candidate_dirs.extend(
            [
                repo_path / "target" / "surefire-reports",
                repo_path / "target" / "failsafe-reports",
            ]
        )
    elif tool == "gradle":
        candidate_dirs.extend(
            [
                repo_path / "build" / "test-results",
                repo_path / "build" / "test-results" / "test",
            ]
        )

    # Submodule support and fallback.
    candidate_dirs.extend(repo_path.glob("**/target/surefire-reports"))
    candidate_dirs.extend(repo_path.glob("**/target/failsafe-reports"))
    candidate_dirs.extend(repo_path.glob("**/build/test-results"))
    candidate_dirs.extend(repo_path.glob("**/build/test-results/test"))

    xml_paths: list[Path] = []
    seen: set[Path] = set()
    for directory in candidate_dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        for path in directory.rglob("*.xml"):
            if modified_after_epoch is not None:
                try:
                    if path.stat().st_mtime < modified_after_epoch:
                        continue
                except OSError:
                    continue
            if path not in seen:
                seen.add(path)
                xml_paths.append(path)
    return sorted(xml_paths)


def _extract_stack_trace(case_elem: ET.Element) -> tuple[str | None, str | None]:
    for tag in ("failure", "error"):
        node = case_elem.find(tag)
        if node is None:
            continue
        message = node.get("message")
        body = (node.text or "").strip() or None
        return message, body
    return None, None


def _parse_junit_xml_reports(report_paths: list[Path]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for path in report_paths:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            LOGGER.warning("Skipping unreadable XML report: %s", path)
            continue

        for case in root.findall(".//testcase"):
            failure = case.find("failure")
            error = case.find("error")
            if failure is None and error is None:
                continue

            class_name = case.get("classname") or ""
            method_name = case.get("name") or ""
            message, stack = _extract_stack_trace(case)
            failures.append(
                {
                    "test_class": class_name,
                    "test_method": method_name,
                    "message": message,
                    "stack_trace": stack,
                    "source": "junit_xml",
                    "report_path": str(path),
                }
            )
    return failures


def _parse_console_failures(console_text: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    # Gradle plain output examples:
    # "MyTest > testThing FAILED"
    gradle_pattern = re.compile(r"^\s*([A-Za-z0-9_.$]+)\s*>\s*([^\s]+)\s+FAILED\s*$", re.MULTILINE)
    for class_name, method_name in gradle_pattern.findall(console_text):
        key = (class_name, method_name)
        if key in seen:
            continue
        seen.add(key)
        failures.append(
            {
                "test_class": class_name,
                "test_method": method_name,
                "message": None,
                "stack_trace": None,
                "source": "console",
                "report_path": None,
            }
        )

    # Maven surefire examples:
    # "[ERROR]   FooTest.testBar:42 expected..."
    maven_pattern = re.compile(
        r"^\s*\[ERROR\]\s+([A-Za-z0-9_.$]+)\.([A-Za-z0-9_$<>.\[\]-]+):\d+",
        re.MULTILINE,
    )
    for class_name, method_name in maven_pattern.findall(console_text):
        key = (class_name, method_name)
        if key in seen:
            continue
        seen.add(key)
        failures.append(
            {
                "test_class": class_name,
                "test_method": method_name,
                "message": None,
                "stack_trace": None,
                "source": "console",
                "report_path": None,
            }
        )

    return failures


def _dedupe_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for failure in failures:
        key = (failure.get("test_class") or "", failure.get("test_method") or "")
        existing = merged.get(key)
        if existing is None:
            merged[key] = failure
            continue
        # Prefer richer stack trace/message data (typically from JUnit XML).
        if not existing.get("stack_trace") and failure.get("stack_trace"):
            existing["stack_trace"] = failure["stack_trace"]
        if not existing.get("message") and failure.get("message"):
            existing["message"] = failure["message"]
        if existing.get("source") != "junit_xml" and failure.get("source") == "junit_xml":
            existing["source"] = "junit_xml"
            existing["report_path"] = failure.get("report_path")
    return sorted(merged.values(), key=lambda x: (x.get("test_class", ""), x.get("test_method", "")))


def _ensure_log_dir(workdir: Path) -> Path:
    target = workdir / "phase2" / "artifacts" / "logs" / "test_runs"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _result_output_path(workdir: Path, repo_id: str) -> Path:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _ensure_log_dir(workdir) / f"{timestamp}_{repo_id}_test_run.json"


def _console_output_path(workdir: Path, repo_id: str) -> Path:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _ensure_log_dir(workdir) / f"{timestamp}_{repo_id}_console.log"


def run_repo_tests(
    workdir: Path,
    repo_id: str,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = DEFAULT_REPO_INDEX_REL,
    output_path: Path | None = None,
    extra_args: list[str] | None = None,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    started_at = _utc_now_iso()
    start_clock = time.monotonic()
    start_epoch = time.time()

    repo_path = _repo_path_for_id(
        workdir=workdir,
        repo_id=repo_id,
        repos_root=repos_root,
        repo_index_path=repo_index_path,
    )
    console_path = _console_output_path(workdir, repo_id)
    target_path = _resolve_path(workdir, output_path) if output_path else _result_output_path(workdir, repo_id)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    tool: str | None = None
    runner: str | None = None
    command: list[str] = []
    execution_error: str | None = None
    exit_code = -1
    failing_tests: list[dict[str, Any]] = []
    report_paths: list[Path] = []
    stdout_text = ""
    stderr_text = ""
    process_started = False

    if not (repo_path / ".git").exists():
        execution_error = f"repo_not_found: {repo_path}"
    else:
        detected = detect_java_build_tool(repo_path)
        tool = detected["tool"]
        runner = detected["runner"]
        command = list(detected["command"])
        if not tool or not command:
            execution_error = (
                f"build_tool_not_detected in {repo_path}: "
                "expected wrapper or build files (pom.xml/build.gradle)"
            )
        else:
            if extra_args:
                command.extend(extra_args)
            LOGGER.info("Running tests for repo_id=%s using %s (%s)", repo_id, tool, runner)
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(repo_path),
                    text=True,
                    capture_output=True,
                    timeout=timeout_seconds,
                    check=False,
                )
                process_started = True
                exit_code = completed.returncode
                stdout_text = completed.stdout or ""
                stderr_text = completed.stderr or ""
            except FileNotFoundError as exc:
                execution_error = f"runner_not_found: {exc}"
            except subprocess.TimeoutExpired as exc:
                process_started = True
                execution_error = f"timeout_after_seconds: {timeout_seconds}"
                stdout_text = exc.stdout or ""
                stderr_text = exc.stderr or ""

    combined_output = f"{stdout_text}\n{stderr_text}".strip()
    console_path.write_text(combined_output, encoding="utf-8")

    if (repo_path / ".git").exists() and process_started:
        report_paths = _collect_junit_xml_paths(
            repo_path,
            tool=tool,
            modified_after_epoch=start_epoch - 2.0,
        )
        xml_failures = _parse_junit_xml_reports(report_paths)
        console_failures = _parse_console_failures(combined_output)
        failing_tests = _dedupe_failures([*xml_failures, *console_failures])

    if execution_error is not None:
        success = False
        status = "error"
    else:
        success = exit_code == 0 and not failing_tests
        status = "pass" if success else "fail"

    duration_seconds = round(time.monotonic() - start_clock, 3)
    result = {
        "repo_id": repo_id,
        "repo_path": str(repo_path),
        "started_at_utc": started_at,
        "duration_seconds": duration_seconds,
        "build_tool": {
            "tool": tool,
            "runner": runner,
            "detected_command": command,
            "detected_command_shell": " ".join(shlex.quote(part) for part in command),
        },
        "status": status,
        "success": success,
        "exit_code": exit_code,
        "execution_error": execution_error,
        "console_log_path": str(console_path),
        "report_paths": [str(path) for path in report_paths],
        "failing_tests": failing_tests,
        "failing_test_count": len(failing_tests),
    }

    target_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    result["result_path"] = str(target_path)
    return result
