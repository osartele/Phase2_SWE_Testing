from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from phase2.case_runner import _repo_is_clean, _resolve_path, _run_git
from phase2.cases import load_case_by_id
from phase2.repo import REPO_INDEX_REL, resolve_repo_path
from phase2.test_runner import detect_java_build_tool


DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_ARTIFACTS_ROOT_REL = Path("artifacts/regen")
DEFAULT_TOOLS_DIR_REL = Path("phase2/tools/evosuite")
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 120
DEFAULT_RUNTIME_TIMEOUT_SECONDS = 120

JAVA_VERSION_RE = re.compile(r'version "([^"]+)"')
JAVA_PLAIN_RE = re.compile(r"\b(?:openjdk|java)\s+(\d+)(?:\.\d+)?")
PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)


@dataclass(slots=True)
class JavaRuntimeInfo:
    java_bin: str
    version_text: str
    major: int | None
    raw_output: str


def _run_command(
    command: list[str],
    cwd: Path | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
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
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "error": f"runner_not_found: {exc}",
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


def _dedupe_paths(entries: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for entry in entries:
        value = entry.strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _download_evosuite_jar(
    target_path: Path,
    download_url: str,
    timeout_seconds: int,
    force_download: bool,
) -> tuple[Path, bool]:
    if target_path.exists() and not force_download:
        return target_path, True

    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with requests.get(download_url, stream=True, timeout=timeout_seconds) as response:
        response.raise_for_status()
        with tmp_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)
    tmp_path.replace(target_path)
    return target_path, False


def resolve_evosuite_jar_path(
    workdir: Path,
    config: dict[str, Any] | None = None,
    jar_path: Path | None = None,
    download: bool = False,
    download_url: str | None = None,
    tools_dir: Path = DEFAULT_TOOLS_DIR_REL,
    force_download: bool = False,
    timeout_seconds: int = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    cfg = config or {}
    tool_cfg = cfg.get("tools", {}).get("evosuite", {}) if isinstance(cfg.get("tools"), dict) else {}

    source = "argument"
    resolved_path: Path | None = None
    if jar_path is not None:
        resolved_path = _resolve_path(workdir, jar_path)
        source = "argument"
    elif os.environ.get("EVOSUITE_JAR", "").strip():
        resolved_path = Path(os.environ["EVOSUITE_JAR"]).expanduser().resolve()
        source = "env"
    else:
        configured_path = tool_cfg.get("jar_path") if isinstance(tool_cfg, dict) else None
        if isinstance(configured_path, str) and configured_path.strip():
            resolved_path = _resolve_path(workdir, Path(configured_path))
            source = "config"

    resolved_download_url = download_url
    if not resolved_download_url:
        cfg_url = tool_cfg.get("download_url") if isinstance(tool_cfg, dict) else None
        if isinstance(cfg_url, str) and cfg_url.strip():
            resolved_download_url = cfg_url.strip()

    if resolved_path is not None and resolved_path.exists():
        return {
            "jar_path": resolved_path,
            "source": source,
            "downloaded": False,
            "used_cache": False,
            "download_url": None,
        }
    if resolved_path is not None and not download:
        raise FileNotFoundError(
            f"EvoSuite jar path does not exist: {resolved_path}. "
            "Provide a valid --evosuite-jar path, set EVOSUITE_JAR, or enable download with --download."
        )

    should_download = download or resolved_download_url is not None
    if not should_download:
        raise FileNotFoundError(
            "EvoSuite jar is not configured. Set --evosuite-jar, EVOSUITE_JAR, "
            "or configure tools.evosuite.jar_path / tools.evosuite.download_url."
        )
    if not resolved_download_url:
        raise ValueError(
            "EvoSuite download requested but no URL provided. Set --download-url or tools.evosuite.download_url."
        )

    parsed = urlparse(resolved_download_url)
    file_name = Path(parsed.path).name or "evosuite.jar"
    target = _resolve_path(workdir, tools_dir) / file_name
    try:
        path, used_cache = _download_evosuite_jar(
            target_path=target,
            download_url=resolved_download_url,
            timeout_seconds=timeout_seconds,
            force_download=force_download,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Failed to download EvoSuite jar from {resolved_download_url}: {exc}"
        ) from exc
    return {
        "jar_path": path,
        "source": "download",
        "downloaded": not used_cache,
        "used_cache": used_cache,
        "download_url": resolved_download_url,
    }


def detect_java_runtime(java_bin: str = "java", timeout_seconds: int = 30) -> JavaRuntimeInfo:
    result = _run_command([java_bin, "-version"], cwd=None, timeout_seconds=timeout_seconds)
    combined = (result["stdout"] + "\n" + result["stderr"]).strip()
    if result["error"] is not None and "runner_not_found" in str(result["error"]):
        raise RuntimeError(
            f"Java executable '{java_bin}' was not found. Install a JDK and/or pass --java-bin."
        )
    if result["error"] is not None:
        raise RuntimeError(
            f"Failed to execute '{java_bin} -version': {result['error']}\nOutput:\n{combined}"
        )

    version_text = ""
    major: int | None = None
    match = JAVA_VERSION_RE.search(combined)
    if match:
        version_text = match.group(1)
        if version_text.startswith("1."):
            parts = version_text.split(".")
            if len(parts) > 1 and parts[1].isdigit():
                major = int(parts[1])
        else:
            first = version_text.split(".", 1)[0]
            if first.isdigit():
                major = int(first)
    else:
        plain = JAVA_PLAIN_RE.search(combined)
        if plain:
            version_text = plain.group(1)
            major = int(plain.group(1))

    return JavaRuntimeInfo(
        java_bin=java_bin,
        version_text=version_text or "unknown",
        major=major,
        raw_output=combined,
    )


def verify_evosuite_runtime(
    java_bin: str,
    jar_path: Path,
    timeout_seconds: int = DEFAULT_RUNTIME_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    runtime = detect_java_runtime(java_bin=java_bin)
    command = [java_bin, "-jar", str(jar_path), "-help"]
    result = _run_command(command=command, cwd=None, timeout_seconds=timeout_seconds)
    combined = (result["stdout"] + "\n" + result["stderr"]).strip()

    if result["error"] is not None:
        raise RuntimeError(
            f"Unable to execute EvoSuite with '{java_bin}' and jar '{jar_path}': {result['error']}"
        )
    if result["exit_code"] != 0:
        hint = ""
        if "UnsupportedClassVersionError" in combined or "class file version" in combined:
            hint = (
                " The EvoSuite jar appears incompatible with this Java runtime. "
                "Use a different JDK via --java-bin or choose another EvoSuite jar build."
            )
        raise RuntimeError(
            "EvoSuite runtime check failed.\n"
            f"Command: {' '.join(command)}\n"
            f"Java version: {runtime.version_text} (major={runtime.major})\n"
            f"Exit code: {result['exit_code']}\n"
            f"Output:\n{combined}{hint}"
        )
    return {
        "java_bin": java_bin,
        "java_version_text": runtime.version_text,
        "java_major": runtime.major,
        "command": command,
        "exit_code": result["exit_code"],
        "duration_seconds": result["duration_seconds"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


def _tool_runner(tool_info: dict[str, Any]) -> str:
    command = tool_info.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError(f"Build tool command not detected: {tool_info}")
    runner = str(command[0]).strip()
    if not runner:
        raise ValueError(f"Build tool runner is empty: {tool_info}")
    return runner


def prepare_project_for_evosuite(
    repo_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    tool_info = detect_java_build_tool(repo_path)
    tool = str(tool_info.get("tool") or "")
    runner = _tool_runner(tool_info)

    if tool == "maven":
        command = [runner, "-B", "-DskipTests", "test-compile"]
    elif tool == "gradle":
        command = [runner, "testClasses", "-x", "test", "--console=plain"]
    else:
        raise RuntimeError(
            f"Build tool not detected for repo {repo_path}. Expected Maven/Gradle project files or wrappers."
        )

    run = _run_command(command=command, cwd=repo_path, timeout_seconds=timeout_seconds)
    return {
        "tool": tool,
        "runner": runner,
        "command": command,
        "ok": run["ok"],
        "exit_code": run["exit_code"],
        "error": run["error"],
        "duration_seconds": run["duration_seconds"],
        "stdout": run["stdout"],
        "stderr": run["stderr"],
    }


def _write_gradle_classpath_init_script(path: Path) -> None:
    script = """
gradle.rootProject {
  tasks.register("phase2PrintTestRuntimeClasspath") {
    doLast {
      allprojects.each { p ->
        def sourceSets = p.extensions.findByName("sourceSets")
        if (sourceSets == null) {
          return
        }
        def testSet = sourceSets.findByName("test")
        if (testSet == null) {
          return
        }
        def cp = testSet.runtimeClasspath
        if (cp != null) {
          println("PHASE2_TEST_CP::" + p.path + "::" + cp.asPath)
        }
      }
    }
  }
}
"""
    path.write_text(script.strip() + "\n", encoding="utf-8")


def _build_maven_test_classpath(
    repo_path: Path,
    runner: str,
    logs_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    cp_file = logs_dir / "maven_test_classpath.txt"
    command = [
        runner,
        "-B",
        "-q",
        "-DskipTests",
        "-DincludeScope=test",
        "dependency:build-classpath",
        f"-Dmdep.outputFile={cp_file}",
    ]
    run = _run_command(command=command, cwd=repo_path, timeout_seconds=timeout_seconds)
    (logs_dir / "maven_classpath_stdout.log").write_text(run["stdout"], encoding="utf-8")
    (logs_dir / "maven_classpath_stderr.log").write_text(run["stderr"], encoding="utf-8")
    if not run["ok"]:
        raise RuntimeError(
            "Failed to build Maven test classpath via dependency:build-classpath.\n"
            f"Command: {' '.join(command)}\n"
            f"Exit code: {run['exit_code']}\n"
            f"Error: {run['error']}\n"
            f"See logs: {logs_dir / 'maven_classpath_stderr.log'}"
        )

    entries: list[str] = []
    for path in sorted(repo_path.glob("**/target/classes")):
        entries.append(str(path.resolve()))
    for path in sorted(repo_path.glob("**/target/test-classes")):
        entries.append(str(path.resolve()))
    if cp_file.exists():
        raw = cp_file.read_text(encoding="utf-8", errors="replace").strip()
        for part in raw.split(os.pathsep):
            if part.strip():
                entries.append(part.strip())

    deduped = _dedupe_paths(entries)
    if not deduped:
        raise RuntimeError(
            "Maven classpath resolution produced no entries. "
            "Ensure the project builds and dependency:build-classpath is available."
        )
    return {
        "tool": "maven",
        "command": command,
        "classpath_entries": deduped,
    }


def _build_gradle_test_classpath(
    repo_path: Path,
    runner: str,
    logs_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    init_script = logs_dir / "phase2_gradle_classpath.init.gradle"
    _write_gradle_classpath_init_script(init_script)
    command = [runner, "-q", "-I", str(init_script), "phase2PrintTestRuntimeClasspath"]
    run = _run_command(command=command, cwd=repo_path, timeout_seconds=timeout_seconds)
    (logs_dir / "gradle_classpath_stdout.log").write_text(run["stdout"], encoding="utf-8")
    (logs_dir / "gradle_classpath_stderr.log").write_text(run["stderr"], encoding="utf-8")
    if not run["ok"]:
        raise RuntimeError(
            "Failed to build Gradle test runtime classpath.\n"
            f"Command: {' '.join(command)}\n"
            f"Exit code: {run['exit_code']}\n"
            f"Error: {run['error']}\n"
            f"See logs: {logs_dir / 'gradle_classpath_stderr.log'}"
        )

    entries: list[str] = []
    for line in run["stdout"].splitlines():
        line = line.strip()
        if not line.startswith("PHASE2_TEST_CP::"):
            continue
        parts = line.split("::", 2)
        if len(parts) != 3:
            continue
        cp = parts[2].strip()
        if not cp:
            continue
        for piece in cp.split(os.pathsep):
            if piece.strip():
                entries.append(piece.strip())

    if not entries:
        for pattern in [
            "**/build/classes/java/main",
            "**/build/classes/java/test",
            "**/build/resources/main",
            "**/build/resources/test",
            "**/build/libs/*.jar",
        ]:
            for path in sorted(repo_path.glob(pattern)):
                entries.append(str(path.resolve()))

    deduped = _dedupe_paths(entries)
    if not deduped:
        raise RuntimeError(
            "Gradle classpath resolution produced no entries. "
            "Ensure the project has Java test source sets and builds successfully."
        )
    return {
        "tool": "gradle",
        "command": command,
        "classpath_entries": deduped,
    }


def build_project_test_classpath(
    repo_path: Path,
    tool: str,
    runner: str,
    logs_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    if tool == "maven":
        data = _build_maven_test_classpath(
            repo_path=repo_path,
            runner=runner,
            logs_dir=logs_dir,
            timeout_seconds=timeout_seconds,
        )
    elif tool == "gradle":
        data = _build_gradle_test_classpath(
            repo_path=repo_path,
            runner=runner,
            logs_dir=logs_dir,
            timeout_seconds=timeout_seconds,
        )
    else:
        raise RuntimeError(f"Unsupported build tool for classpath generation: {tool}")

    classpath_entries = data["classpath_entries"]
    classpath = os.pathsep.join(classpath_entries)
    (logs_dir / "project_test_classpath.txt").write_text(classpath, encoding="utf-8")
    data["classpath"] = classpath
    data["classpath_path"] = str(logs_dir / "project_test_classpath.txt")
    return data


def focal_class_fqcn(repo_path: Path, focal_file_path: str) -> str:
    target = repo_path / focal_file_path
    if not target.exists():
        raise FileNotFoundError(f"Focal class file not found at modified commit: {target}")
    source = target.read_text(encoding="utf-8", errors="replace")
    package_match = PACKAGE_RE.search(source)
    package_name = package_match.group(1).strip() if package_match else ""
    class_name = target.stem
    if package_name:
        return f"{package_name}.{class_name}"
    return class_name


def run_evosuite_generation(
    repo_path: Path,
    java_bin: str,
    jar_path: Path,
    focal_fqcn: str,
    classpath: str,
    generated_dir: Path,
    seed: int,
    time_budget_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    generated_dir.mkdir(parents=True, exist_ok=True)
    command = [
        java_bin,
        "-jar",
        str(jar_path),
        "-class",
        focal_fqcn,
        "-projectCP",
        classpath,
        "-seed",
        str(seed),
        f"-Dsearch_budget={int(time_budget_seconds)}",
        "-Dtest_dir",
        str(generated_dir),
    ]
    return _run_command(
        command=command,
        cwd=repo_path,
        timeout_seconds=max(timeout_seconds, int(time_budget_seconds) + 120),
    )


def run_evosuite_for_case(
    workdir: Path,
    case_id: str,
    config: dict[str, Any] | None = None,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = REPO_INDEX_REL,
    artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT_REL,
    evosuite_jar: Path | None = None,
    download: bool = False,
    download_url: str | None = None,
    force_download: bool = False,
    java_bin: str = "java",
    seed: int = 1337,
    time_budget_seconds: int = 120,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    case = load_case_by_id(case_id, workdir=workdir)
    repo_path = resolve_repo_path(
        workdir=workdir,
        repo_id=case.repo_id,
        repos_root=repos_root,
        index_path=repo_index_path,
    )
    if not (repo_path / ".git").exists():
        raise FileNotFoundError(f"Repository not found for case repo_id={case.repo_id}: {repo_path}")
    if not _repo_is_clean(repo_path):
        raise RuntimeError(f"Repository is dirty; aborting evosuite run: {repo_path}")

    root = _resolve_path(workdir, artifacts_root) / case.case_id / "evosuite"
    if root.exists():
        shutil.rmtree(root)
    logs_dir = root / "logs"
    generated_dir = root / "generated_tests"
    logs_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)

    jar_info = resolve_evosuite_jar_path(
        workdir=workdir,
        config=config,
        jar_path=evosuite_jar,
        download=download,
        download_url=download_url,
        force_download=force_download,
    )
    jar_path = Path(jar_info["jar_path"])
    runtime_info = verify_evosuite_runtime(java_bin=java_bin, jar_path=jar_path)

    original_sha = _run_git(repo_path, ["rev-parse", "HEAD"])
    checked_out_modified_sha: str | None = None
    started_at = time.monotonic()
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
        (logs_dir / "prepare_stdout.log").write_text(prepare["stdout"], encoding="utf-8")
        (logs_dir / "prepare_stderr.log").write_text(prepare["stderr"], encoding="utf-8")
        if not prepare["ok"]:
            raise RuntimeError(
                "Project build preparation failed before EvoSuite generation.\n"
                f"Tool: {prepare['tool']}\n"
                f"Command: {' '.join(prepare['command'])}\n"
                f"Exit code: {prepare['exit_code']}\n"
                f"Error: {prepare['error']}\n"
                f"See logs: {logs_dir / 'prepare_stderr.log'}"
            )

        classpath_info = build_project_test_classpath(
            repo_path=repo_path,
            tool=prepare["tool"],
            runner=prepare["runner"],
            logs_dir=logs_dir,
            timeout_seconds=timeout_seconds,
        )
        focal_fqcn = focal_class_fqcn(repo_path=repo_path, focal_file_path=case.focal_file_path)
        evo = run_evosuite_generation(
            repo_path=repo_path,
            java_bin=java_bin,
            jar_path=jar_path,
            focal_fqcn=focal_fqcn,
            classpath=classpath_info["classpath"],
            generated_dir=generated_dir,
            seed=seed,
            time_budget_seconds=time_budget_seconds,
            timeout_seconds=timeout_seconds,
        )
        (logs_dir / "evosuite_stdout.log").write_text(evo["stdout"], encoding="utf-8")
        (logs_dir / "evosuite_stderr.log").write_text(evo["stderr"], encoding="utf-8")
        if not evo["ok"]:
            raise RuntimeError(
                "EvoSuite generation failed.\n"
                f"Command: {' '.join(evo['command'])}\n"
                f"Exit code: {evo['exit_code']}\n"
                f"Error: {evo['error']}\n"
                f"See logs: {logs_dir / 'evosuite_stderr.log'}"
            )

        generated_tests = sorted(str(path) for path in generated_dir.rglob("*_ESTest.java"))
        summary = {
            "status": "ok",
            "case_id": case.case_id,
            "repo_id": case.repo_id,
            "repo_path": str(repo_path),
            "modified_commit": case.modified_commit,
            "checked_out_modified_sha": checked_out_modified_sha,
            "restored_original_sha": original_sha,
            "evosuite_jar": str(jar_path),
            "jar_source": jar_info["source"],
            "jar_downloaded": jar_info["downloaded"],
            "jar_cache_hit": jar_info["used_cache"],
            "java_runtime": {
                "java_bin": runtime_info["java_bin"],
                "version_text": runtime_info["java_version_text"],
                "major": runtime_info["java_major"],
            },
            "build_tool": prepare["tool"],
            "build_runner": prepare["runner"],
            "classpath_entries_count": len(classpath_info["classpath_entries"]),
            "classpath_path": classpath_info["classpath_path"],
            "focal_class_fqcn": focal_fqcn,
            "seed": seed,
            "time_budget_seconds": time_budget_seconds,
            "generated_tests_count": len(generated_tests),
            "generated_tests": generated_tests,
            "generated_tests_dir": str(generated_dir),
            "logs_dir": str(logs_dir),
            "duration_seconds": round(time.monotonic() - started_at, 3),
        }
    finally:
        _run_git(repo_path, ["checkout", "--detach", original_sha], check=False)

    summary_path = root / "evosuite_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary
