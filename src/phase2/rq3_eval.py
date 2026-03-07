from __future__ import annotations

import csv
import json
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from phase2.cases import Case, list_cases
from phase2.repo import REPO_INDEX_REL, resolve_repo_path
from phase2.test_runner import detect_java_build_tool


DEFAULT_CASES_ROOT_REL = Path("cases")
DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_ARTIFACTS_HEALING_REL = Path("artifacts/healing")
DEFAULT_ARTIFACTS_REGEN_REL = Path("artifacts/regen")
DEFAULT_RQ3_CSV_REL = Path("artifacts/rq3_quality.csv")
DEFAULT_RQ3_CASES_REL = Path("artifacts/rq3_cases")
DEFAULT_RQ3_EVAL_ROOT_REL = Path("phase2/evaluation/rq3_test_quality")

JACOCO_MAVEN_PLUGIN = "org.jacoco:jacoco-maven-plugin:0.8.11"
PIT_MAVEN_PLUGIN = "org.pitest:pitest-maven:mutationCoverage"
PIT_KILLED_STATUSES = {
    "KILLED",
    "TIMED_OUT",
    "MEMORY_ERROR",
    "NON_VIABLE",
}


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _run_git(repo_path: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_path),
        text=True,
        capture_output=True,
        check=check,
    )


def _run_command(command: list[str], cwd: Path, timeout_seconds: int) -> dict[str, Any]:
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
    except Exception as exc:  # pragma: no cover - defensive path
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "error": f"command_error_{type(exc).__name__}: {exc}",
            "duration_seconds": round(time.monotonic() - started, 3),
            "command": command,
        }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        return payload
    return None


def _resolve_artifact_path(path_value: str | None, base_dir: Path) -> Path | None:
    if not path_value:
        return None
    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    if candidate.exists():
        return candidate
    return None


def _infer_test_root_rel(test_file_path: str) -> Path:
    normalized = test_file_path.replace("\\", "/")
    marker = "src/test/java"
    if marker in normalized:
        prefix = normalized.split(marker, 1)[0].strip("/")
        return (Path(prefix) / marker) if prefix else Path(marker)
    candidate = Path(normalized)
    return candidate.parent if candidate.parent != Path(".") else Path("src/test/java")


def _discover_reports(
    worktree_path: Path,
    patterns: list[str],
    modified_after_epoch: float | None = None,
) -> list[Path]:
    seen: set[Path] = set()
    results: list[Path] = []
    for pattern in patterns:
        for path in worktree_path.glob(pattern):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if modified_after_epoch is not None:
                try:
                    if resolved.stat().st_mtime < modified_after_epoch:
                        continue
                except OSError:
                    continue
            if resolved in seen:
                continue
            seen.add(resolved)
            results.append(resolved)
    return sorted(results)


def _parse_jacoco_report(path: Path) -> dict[str, Any] | None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None

    counters: dict[str, tuple[int, int]] = {}
    for counter in root.findall("./counter"):
        ctype = str(counter.get("type") or "").upper()
        try:
            missed = int(counter.get("missed") or "0")
            covered = int(counter.get("covered") or "0")
        except ValueError:
            continue
        counters[ctype] = (missed, covered)

    if not counters:
        return None

    selected_type = "LINE" if "LINE" in counters else "INSTRUCTION" if "INSTRUCTION" in counters else None
    if selected_type is None:
        return None
    missed, covered = counters[selected_type]
    total = missed + covered
    return {
        "counter_type": selected_type.lower(),
        "covered": covered,
        "missed": missed,
        "total": total,
    }


def _aggregate_jacoco_metrics(paths: list[Path]) -> dict[str, Any]:
    line_covered = 0
    line_total = 0
    instr_covered = 0
    instr_total = 0
    parsed_paths: list[str] = []

    for path in paths:
        parsed = _parse_jacoco_report(path)
        if parsed is None:
            continue
        parsed_paths.append(str(path))
        if parsed["counter_type"] == "line":
            line_covered += int(parsed["covered"])
            line_total += int(parsed["total"])
        elif parsed["counter_type"] == "instruction":
            instr_covered += int(parsed["covered"])
            instr_total += int(parsed["total"])

    if line_total > 0:
        pct = round((line_covered / line_total) * 100.0, 4)
        return {
            "coverage_pct": pct,
            "counter_type": "line",
            "covered": line_covered,
            "total": line_total,
            "report_paths": parsed_paths,
        }
    if instr_total > 0:
        pct = round((instr_covered / instr_total) * 100.0, 4)
        return {
            "coverage_pct": pct,
            "counter_type": "instruction",
            "covered": instr_covered,
            "total": instr_total,
            "report_paths": parsed_paths,
        }
    return {
        "coverage_pct": None,
        "counter_type": None,
        "covered": 0,
        "total": 0,
        "report_paths": [],
    }


def _parse_pit_report(path: Path) -> dict[str, Any] | None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None

    total = 0
    killed = 0
    for mutation in root.findall(".//mutation"):
        total += 1
        status = str(mutation.get("status") or "").upper()
        detected = str(mutation.get("detected") or "").lower() == "true"
        if detected or status in PIT_KILLED_STATUSES:
            killed += 1

    return {"killed": killed, "total": total}


def _aggregate_pit_metrics(paths: list[Path]) -> dict[str, Any]:
    killed = 0
    total = 0
    parsed_paths: list[str] = []
    for path in paths:
        parsed = _parse_pit_report(path)
        if parsed is None:
            continue
        parsed_paths.append(str(path))
        killed += int(parsed["killed"])
        total += int(parsed["total"])

    score = round((killed / total) * 100.0, 4) if total > 0 else None
    return {
        "mutation_score_pct": score,
        "killed": killed,
        "total": total,
        "report_paths": parsed_paths,
    }


def _copy_reports(report_paths: list[Path], root: Path, target_dir: Path) -> list[str]:
    copied: list[str] = []
    for source in report_paths:
        try:
            relative = source.resolve().relative_to(root.resolve())
            target = target_dir / relative
        except Exception:
            target = target_dir / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(target))
    return copied


def _prepare_healing_suite(worktree_path: Path, case_id: str, artifacts_healing_root: Path) -> dict[str, Any]:
    summary_path = artifacts_healing_root / case_id / "healing_summary.json"
    summary = _read_json(summary_path)
    if summary is None:
        return {"ok": False, "error": f"missing_healing_summary: {summary_path}"}

    iterations = summary.get("iterations")
    if not isinstance(iterations, list):
        return {"ok": False, "error": f"invalid_healing_iterations: {summary_path}"}

    applied: list[str] = []
    for item in sorted(iterations, key=lambda row: int((row or {}).get("iteration", 0))):
        if not isinstance(item, dict):
            continue
        patch_path = _resolve_artifact_path(str(item.get("patch_diff_path") or ""), summary_path.parent)
        if patch_path is None or not patch_path.exists():
            continue
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        if not patch_text.strip():
            continue
        applied_result = _run_command(
            command=["git", "apply", "--whitespace=nowarn", str(patch_path)],
            cwd=worktree_path,
            timeout_seconds=120,
        )
        if not applied_result["ok"]:
            return {
                "ok": False,
                "error": (
                    f"failed_apply_healing_patch: {patch_path} "
                    f"(exit={applied_result['exit_code']}, error={applied_result['error']})"
                ),
            }
        applied.append(str(patch_path))
    return {"ok": True, "applied_patches": applied}


def _prepare_regen_suite(worktree_path: Path, case: Case, artifacts_regen_root: Path) -> dict[str, Any]:
    summary_path = artifacts_regen_root / case.case_id / "regen_summary.json"
    summary = _read_json(summary_path)
    if summary is None:
        return {"ok": False, "error": f"missing_regen_summary: {summary_path}"}

    merged_suite_path = _resolve_artifact_path(str(summary.get("merged_suite_path") or ""), summary_path.parent)
    if merged_suite_path is None:
        return {"ok": False, "error": f"missing_merged_suite_path: {summary_path}"}

    snapshot_dir = merged_suite_path / "test_root_snapshot"
    if snapshot_dir.exists():
        test_root_rel = _infer_test_root_rel(case.test_file_path)
        destination = (worktree_path / test_root_rel).resolve()
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(snapshot_dir, destination)
        return {
            "ok": True,
            "mode": "snapshot_copy",
            "snapshot_path": str(snapshot_dir),
            "destination": str(destination),
        }

    merge_patch = merged_suite_path / "merge_patch.diff"
    if merge_patch.exists():
        patch_text = merge_patch.read_text(encoding="utf-8", errors="replace")
        if patch_text.strip():
            applied_result = _run_command(
                command=["git", "apply", "--whitespace=nowarn", str(merge_patch)],
                cwd=worktree_path,
                timeout_seconds=120,
            )
            if not applied_result["ok"]:
                return {
                    "ok": False,
                    "error": (
                        f"failed_apply_regen_patch: {merge_patch} "
                        f"(exit={applied_result['exit_code']}, error={applied_result['error']})"
                    ),
                }
            return {"ok": True, "mode": "merge_patch_apply", "patch_path": str(merge_patch)}

    return {"ok": False, "error": f"missing_regen_snapshot_and_patch: {merged_suite_path}"}


def _build_metric_commands(tool: str, runner: str) -> tuple[list[str], list[str]]:
    if tool == "maven":
        jacoco = [
            runner,
            "-B",
            f"{JACOCO_MAVEN_PLUGIN}:prepare-agent",
            "test",
            f"{JACOCO_MAVEN_PLUGIN}:report",
        ]
        pit = [
            runner,
            "-B",
            PIT_MAVEN_PLUGIN,
            "-DtimestampedReports=false",
        ]
        return jacoco, pit
    if tool == "gradle":
        jacoco = [runner, "test", "jacocoTestReport", "--console=plain"]
        pit = [runner, "pitest", "--console=plain"]
        return jacoco, pit
    raise ValueError(f"Unsupported build tool for RQ3 metrics: {tool}")


def _collect_metric_reports(worktree_path: Path, started_epoch: float) -> tuple[list[Path], list[Path]]:
    jacoco_patterns = [
        "**/target/site/jacoco/jacoco.xml",
        "**/build/reports/jacoco/test/jacocoTestReport.xml",
        "**/build/reports/jacoco/**/*.xml",
    ]
    pit_patterns = [
        "**/target/pit-reports/mutations.xml",
        "**/target/pit-reports/*/mutations.xml",
        "**/build/reports/pitest/mutations.xml",
        "**/build/reports/pitest/*/mutations.xml",
    ]
    jacoco_reports = _discover_reports(
        worktree_path=worktree_path,
        patterns=jacoco_patterns,
        modified_after_epoch=started_epoch - 2.0,
    )
    pit_reports = _discover_reports(
        worktree_path=worktree_path,
        patterns=pit_patterns,
        modified_after_epoch=started_epoch - 2.0,
    )
    return jacoco_reports, pit_reports


def _evaluate_metrics_for_worktree(
    worktree_path: Path,
    timeout_seconds: int,
    logs_dir: Path,
) -> dict[str, Any]:
    detected = detect_java_build_tool(worktree_path)
    tool = str(detected.get("tool") or "")
    command = detected.get("command")
    if not tool or not isinstance(command, list) or not command:
        return {
            "status": "error",
            "error": (
                f"build_tool_not_detected in {worktree_path}: "
                "expected wrapper or build files (pom.xml/build.gradle)"
            ),
            "coverage_pct": None,
            "coverage_counter": None,
            "mutation_score_pct": None,
            "jacoco_runtime_seconds": None,
            "pit_runtime_seconds": None,
            "jacoco_reports": [],
            "pit_reports": [],
        }

    runner = str(command[0])
    jacoco_cmd, pit_cmd = _build_metric_commands(tool=tool, runner=runner)

    started_epoch = time.time()
    jacoco_result = _run_command(jacoco_cmd, cwd=worktree_path, timeout_seconds=timeout_seconds)
    (logs_dir / "jacoco_stdout.log").write_text(jacoco_result["stdout"], encoding="utf-8")
    (logs_dir / "jacoco_stderr.log").write_text(jacoco_result["stderr"], encoding="utf-8")

    pit_result = _run_command(pit_cmd, cwd=worktree_path, timeout_seconds=timeout_seconds)
    (logs_dir / "pit_stdout.log").write_text(pit_result["stdout"], encoding="utf-8")
    (logs_dir / "pit_stderr.log").write_text(pit_result["stderr"], encoding="utf-8")

    jacoco_reports, pit_reports = _collect_metric_reports(worktree_path, started_epoch=started_epoch)
    jacoco_metrics = _aggregate_jacoco_metrics(jacoco_reports)
    pit_metrics = _aggregate_pit_metrics(pit_reports)

    status = "ok"
    issues: list[str] = []
    if not jacoco_result["ok"]:
        status = "partial"
        issues.append(
            f"jacoco_command_failed(exit={jacoco_result['exit_code']}, error={jacoco_result['error']})"
        )
    if not pit_result["ok"]:
        status = "partial"
        issues.append(f"pit_command_failed(exit={pit_result['exit_code']}, error={pit_result['error']})")
    if jacoco_metrics["coverage_pct"] is None and pit_metrics["mutation_score_pct"] is None:
        status = "error"
    elif jacoco_metrics["coverage_pct"] is None or pit_metrics["mutation_score_pct"] is None:
        if status == "ok":
            status = "partial"

    return {
        "status": status,
        "error": "; ".join(issues) if issues else None,
        "tool": tool,
        "runner": runner,
        "jacoco_command": jacoco_cmd,
        "pit_command": pit_cmd,
        "jacoco_command_result": jacoco_result,
        "pit_command_result": pit_result,
        "coverage_pct": jacoco_metrics["coverage_pct"],
        "coverage_counter": jacoco_metrics["counter_type"],
        "mutation_score_pct": pit_metrics["mutation_score_pct"],
        "jacoco_runtime_seconds": jacoco_result["duration_seconds"],
        "pit_runtime_seconds": pit_result["duration_seconds"],
        "jacoco_reports": jacoco_reports,
        "pit_reports": pit_reports,
    }


def _with_worktree(repo_path: Path, commit: str, scratch_root: Path) -> tuple[Path, str]:
    scratch_root.mkdir(parents=True, exist_ok=True)
    worktree_path = Path(tempfile.mkdtemp(prefix="rq3_", dir=str(scratch_root))).resolve()
    add = _run_git(repo_path, ["worktree", "add", "--detach", str(worktree_path), commit], check=False)
    if add.returncode != 0:
        shutil.rmtree(worktree_path, ignore_errors=True)
        raise RuntimeError(f"git worktree add failed for commit {commit}: {add.stderr.strip()}")
    sha = _run_git(worktree_path, ["rev-parse", "HEAD"]).stdout.strip()
    return worktree_path, sha


def _cleanup_worktree(repo_path: Path, worktree_path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=str(repo_path),
        text=True,
        capture_output=True,
        check=False,
    )
    shutil.rmtree(worktree_path, ignore_errors=True)


def evaluate_rq3_quality(
    workdir: Path,
    cases_root: Path = DEFAULT_CASES_ROOT_REL,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = REPO_INDEX_REL,
    artifacts_healing_root: Path = DEFAULT_ARTIFACTS_HEALING_REL,
    artifacts_regen_root: Path = DEFAULT_ARTIFACTS_REGEN_REL,
    output_csv: Path = DEFAULT_RQ3_CSV_REL,
    output_cases_root: Path = DEFAULT_RQ3_CASES_REL,
    eval_root: Path = DEFAULT_RQ3_EVAL_ROOT_REL,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    resolved_cases_root = _resolve_path(workdir, cases_root)
    resolved_repos_root = _resolve_path(workdir, repos_root)
    resolved_repo_index = _resolve_path(workdir, repo_index_path)
    resolved_healing_root = _resolve_path(workdir, artifacts_healing_root)
    resolved_regen_root = _resolve_path(workdir, artifacts_regen_root)
    resolved_output_csv = _resolve_path(workdir, output_csv)
    resolved_output_cases_root = _resolve_path(workdir, output_cases_root)
    resolved_eval_root = _resolve_path(workdir, eval_root)
    resolved_output_cases_root.mkdir(parents=True, exist_ok=True)
    resolved_eval_root.mkdir(parents=True, exist_ok=True)

    cases = list_cases(
        workdir=workdir,
        index_rel=Path(str(resolved_cases_root / "index" / "cases.jsonl")),
    )

    rows: list[dict[str, Any]] = []
    per_case_paths: list[str] = []
    approach_stats: dict[str, dict[str, int]] = {
        "human": {"evaluated": 0},
        "healing": {"evaluated": 0},
        "regen": {"evaluated": 0},
    }

    for case in cases:
        metadata = case.metadata if isinstance(case.metadata, dict) else {}
        human_commit = str(metadata.get("human_update_commit") or "").strip()
        human_available = bool(metadata.get("human_update_found")) and bool(human_commit)

        repo_path = resolve_repo_path(
            workdir=workdir,
            repo_id=case.repo_id,
            repos_root=resolved_repos_root,
            index_path=resolved_repo_index,
        )
        if not (repo_path / ".git").exists():
            continue

        case_summary: dict[str, Any] = {
            "case_id": case.case_id,
            "repo_id": case.repo_id,
            "base_commit": case.base_commit,
            "modified_commit": case.modified_commit,
            "human_update_commit": human_commit if human_available else None,
            "approaches": {},
        }

        approach_defs = [
            ("human", human_commit if human_available else None),
            ("healing", case.modified_commit),
            ("regen", case.modified_commit),
        ]

        for approach, approach_commit in approach_defs:
            row: dict[str, Any] = {
                "case_id": case.case_id,
                "repo_id": case.repo_id,
                "approach": approach,
                "available": True,
                "status": "ok",
                "commit_sha": None,
                "coverage_pct": None,
                "mutation_score_pct": None,
                "runtime_seconds": None,
                "jacoco_runtime_seconds": None,
                "pit_runtime_seconds": None,
                "coverage_counter": None,
                "jacoco_report_count": 0,
                "pit_report_count": 0,
                "jacoco_report_paths": "",
                "pit_report_paths": "",
                "error": None,
            }

            if approach == "human" and not human_available:
                row.update({"available": False, "status": "missing_human_update", "error": "no_human_update_commit"})
                rows.append(row)
                case_summary["approaches"][approach] = row
                continue
            if approach == "healing" and not (resolved_healing_root / case.case_id / "healing_summary.json").exists():
                row.update(
                    {
                        "available": False,
                        "status": "missing_healing_artifact",
                        "error": "missing_healing_summary",
                    }
                )
                rows.append(row)
                case_summary["approaches"][approach] = row
                continue
            if approach == "regen" and not (resolved_regen_root / case.case_id / "regen_summary.json").exists():
                row.update(
                    {
                        "available": False,
                        "status": "missing_regen_artifact",
                        "error": "missing_regen_summary",
                    }
                )
                rows.append(row)
                case_summary["approaches"][approach] = row
                continue

            approach_stats[approach]["evaluated"] += 1
            approach_start = time.monotonic()
            case_approach_logs = resolved_eval_root / "summaries" / case.case_id / approach
            case_approach_logs.mkdir(parents=True, exist_ok=True)
            scratch_root = resolved_eval_root / ".tmp_worktrees" / case.case_id
            worktree_path: Path | None = None

            try:
                worktree_path, commit_sha = _with_worktree(
                    repo_path=repo_path,
                    commit=str(approach_commit),
                    scratch_root=scratch_root,
                )
                row["commit_sha"] = commit_sha

                prep_info: dict[str, Any] = {"ok": True}
                if approach == "healing":
                    prep_info = _prepare_healing_suite(worktree_path, case.case_id, resolved_healing_root)
                elif approach == "regen":
                    prep_info = _prepare_regen_suite(worktree_path, case, resolved_regen_root)

                if not prep_info.get("ok"):
                    row["status"] = "error"
                    row["error"] = str(prep_info.get("error") or "approach_prepare_failed")
                    row["runtime_seconds"] = round(time.monotonic() - approach_start, 3)
                    case_summary["approaches"][approach] = {**row, "prepare": prep_info}
                    rows.append(row)
                    continue

                metrics = _evaluate_metrics_for_worktree(
                    worktree_path=worktree_path,
                    timeout_seconds=timeout_seconds,
                    logs_dir=case_approach_logs,
                )

                jacoco_target = resolved_eval_root / "jacoco" / case.case_id / approach
                pit_target = resolved_eval_root / "pit" / case.case_id / approach
                jacoco_target.mkdir(parents=True, exist_ok=True)
                pit_target.mkdir(parents=True, exist_ok=True)
                jacoco_copied = _copy_reports(metrics["jacoco_reports"], worktree_path, jacoco_target)
                pit_copied = _copy_reports(metrics["pit_reports"], worktree_path, pit_target)

                row.update(
                    {
                        "status": str(metrics["status"]),
                        "coverage_pct": metrics["coverage_pct"],
                        "mutation_score_pct": metrics["mutation_score_pct"],
                        "jacoco_runtime_seconds": metrics["jacoco_runtime_seconds"],
                        "pit_runtime_seconds": metrics["pit_runtime_seconds"],
                        "coverage_counter": metrics["coverage_counter"],
                        "jacoco_report_count": len(jacoco_copied),
                        "pit_report_count": len(pit_copied),
                        "jacoco_report_paths": ";".join(jacoco_copied),
                        "pit_report_paths": ";".join(pit_copied),
                        "error": metrics["error"],
                    }
                )
                row["runtime_seconds"] = round(time.monotonic() - approach_start, 3)

                case_summary["approaches"][approach] = {
                    **row,
                    "prepare": prep_info,
                    "commands": {
                        "jacoco": metrics.get("jacoco_command"),
                        "pit": metrics.get("pit_command"),
                    },
                    "command_results": {
                        "jacoco": {
                            "ok": metrics["jacoco_command_result"]["ok"],
                            "exit_code": metrics["jacoco_command_result"]["exit_code"],
                            "error": metrics["jacoco_command_result"]["error"],
                            "duration_seconds": metrics["jacoco_command_result"]["duration_seconds"],
                        },
                        "pit": {
                            "ok": metrics["pit_command_result"]["ok"],
                            "exit_code": metrics["pit_command_result"]["exit_code"],
                            "error": metrics["pit_command_result"]["error"],
                            "duration_seconds": metrics["pit_command_result"]["duration_seconds"],
                        },
                    },
                }
                rows.append(row)
            except Exception as exc:
                row.update(
                    {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "runtime_seconds": round(time.monotonic() - approach_start, 3),
                    }
                )
                rows.append(row)
                case_summary["approaches"][approach] = row
            finally:
                if worktree_path is not None:
                    _cleanup_worktree(repo_path=repo_path, worktree_path=worktree_path)

        case_summary_path = resolved_output_cases_root / f"{case.case_id}.json"
        case_summary_path.write_text(json.dumps(case_summary, indent=2, sort_keys=True), encoding="utf-8")
        per_case_paths.append(str(case_summary_path))

    resolved_output_csv.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "case_id",
        "repo_id",
        "approach",
        "available",
        "status",
        "commit_sha",
        "coverage_pct",
        "mutation_score_pct",
        "runtime_seconds",
        "jacoco_runtime_seconds",
        "pit_runtime_seconds",
        "coverage_counter",
        "jacoco_report_count",
        "pit_report_count",
        "jacoco_report_paths",
        "pit_report_paths",
        "error",
    ]
    with resolved_output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return {
        "status": "ok",
        "cases_total": len(cases),
        "rows_written": len(rows),
        "approach_counts": approach_stats,
        "output_csv": str(resolved_output_csv),
        "per_case_dir": str(resolved_output_cases_root),
        "per_case_paths": per_case_paths,
        "evaluation_root": str(resolved_eval_root),
    }

