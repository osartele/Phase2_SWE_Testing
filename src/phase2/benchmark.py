from __future__ import annotations

import csv
import json
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phase2.cases import Case, list_cases
from phase2.healing import run_iterative_healing
from phase2.regen import run_regenerative_sync
from phase2.repo import REPO_INDEX_REL, resolve_repo_path
from phase2.rq1_eval import evaluate_rq1_cases
from phase2.rq2_eval import evaluate_rq2_scale
from phase2.rq3_eval import evaluate_rq3_quality


DEFAULT_CASES_ROOT_REL = Path("cases")
DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_BENCHMARK_ROOT_REL = Path("artifacts/benchmark")
DEFAULT_SUMMARY_REL = Path("artifacts/summary.md")
VALID_APPROACHES = {"healing", "regen", "human"}


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def parse_approaches(value: str) -> list[str]:
    parts = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not parts:
        raise ValueError("Approaches cannot be empty.")
    deduped: list[str] = []
    for item in parts:
        if item not in VALID_APPROACHES:
            raise ValueError(f"Unsupported approach '{item}'. Valid values: healing, regen, human")
        if item not in deduped:
            deduped.append(item)
    return deduped


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _safe_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / float(len(values)), 6)


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _write_simple_bar_svg(path: Path, title: str, series: list[tuple[str, float | None]], max_value: float = 100.0) -> Path:
    width = 920
    row_h = 44
    top = 70
    left = 220
    right = 40
    bar_w = width - left - right
    height = top + row_h * max(1, len(series)) + 40
    max_value = max(1.0, max_value)

    lines: list[str] = []
    lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    lines.append('<rect width="100%" height="100%" fill="#ffffff"/>')
    lines.append(f'<text x="20" y="36" font-family="Arial, sans-serif" font-size="24" fill="#111111">{title}</text>')
    lines.append(f'<line x1="{left}" y1="{top - 16}" x2="{left}" y2="{height - 20}" stroke="#333333" stroke-width="1"/>')

    for idx, (label, value) in enumerate(series):
        y = top + idx * row_h
        lines.append(f'<text x="20" y="{y + 20}" font-family="Arial, sans-serif" font-size="14" fill="#222222">{label}</text>')
        lines.append(f'<rect x="{left}" y="{y + 6}" width="{bar_w}" height="18" fill="#f0f0f0"/>')
        if value is None:
            lines.append(f'<text x="{left + 8}" y="{y + 20}" font-family="Arial, sans-serif" font-size="12" fill="#666666">n/a</text>')
            continue
        clipped = max(0.0, min(value, max_value))
        w = round((clipped / max_value) * bar_w, 2)
        lines.append(f'<rect x="{left}" y="{y + 6}" width="{w}" height="18" fill="#2b6cb0"/>')
        lines.append(
            f'<text x="{left + bar_w + 8}" y="{y + 20}" font-family="Arial, sans-serif" font-size="12" fill="#222222">{value:.2f}</text>'
        )

    lines.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _markdown_table(columns: list[str], rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No rows._"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
    return "\n".join([header, sep, *body])


def _select_cases(
    workdir: Path,
    cases_root: Path,
    n_cases: int,
) -> list[Case]:
    resolved_cases_root = _resolve_path(workdir, cases_root)
    all_cases = list_cases(
        workdir=workdir,
        index_rel=Path(str(resolved_cases_root / "index" / "cases.jsonl")),
    )
    all_cases = sorted(all_cases, key=lambda c: c.case_id)
    if n_cases <= 0:
        return all_cases
    return all_cases[: min(n_cases, len(all_cases))]


def _write_case_subset(workdir: Path, cases: list[Case], subset_root: Path) -> Path:
    root = _resolve_path(workdir, subset_root)
    index_path = root / "index" / "cases.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for case in cases:
        case_dir = root / case.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        case_path = case_dir / "case.json"
        case_path.write_text(json.dumps(case.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        rows.append(
            {
                "case_id": case.case_id,
                "repo_id": case.repo_id,
                "path": str(case_path),
                "base_commit": case.base_commit,
                "modified_commit": case.modified_commit,
                "mapped_focal_method": case.mapped_focal_method,
                "mapped_test_method": case.mapped_test_method,
            }
        )
    with index_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")
    return root


def _run_human_reference_once(
    workdir: Path,
    case: Case,
    repos_root: Path,
    repo_index_path: Path,
    artifacts_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    metadata = case.metadata if isinstance(case.metadata, dict) else {}
    human_commit = str(metadata.get("human_update_commit") or "").strip()
    if not human_commit:
        return {
            "case_id": case.case_id,
            "approach": "human",
            "status": "missing_human_update_commit",
            "success": None,
            "duration_seconds": None,
            "stop_reason": "missing_human_update_commit",
            "error": "missing_human_update_commit",
            "summary_path": None,
        }

    repo_path = resolve_repo_path(
        workdir=workdir,
        repo_id=case.repo_id,
        repos_root=repos_root,
        index_path=repo_index_path,
    )
    if not (repo_path / ".git").exists():
        return {
            "case_id": case.case_id,
            "approach": "human",
            "status": "error",
            "success": None,
            "duration_seconds": None,
            "stop_reason": "repo_not_found",
            "error": f"repo_not_found: {repo_path}",
            "summary_path": None,
        }

    case_dir = _resolve_path(workdir, artifacts_root) / case.case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = case_dir / "stdout.log"
    stderr_path = case_dir / "stderr.log"
    summary_path = case_dir / "human_run.json"

    original_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    started = time.monotonic()
    command_runs: list[dict[str, Any]] = []
    status = "ok"
    success = True
    error: str | None = None
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    try:
        checkout = subprocess.run(
            ["git", "checkout", "--detach", human_commit],
            cwd=str(repo_path),
            text=True,
            capture_output=True,
            check=False,
        )
        if checkout.returncode != 0:
            status = "error"
            success = False
            error = f"checkout_failed: {checkout.stderr.strip()}"
        else:
            for cmd in case.build_commands:
                cmd_start = time.monotonic()
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
                    duration = round(time.monotonic() - cmd_start, 3)
                    command_runs.append(
                        {"command": cmd, "exit_code": completed.returncode, "duration_seconds": duration}
                    )
                    stdout_chunks.append(f"$ {cmd}\n{completed.stdout or ''}")
                    stderr_chunks.append(f"$ {cmd}\n{completed.stderr or ''}")
                    if completed.returncode != 0:
                        success = False
                        status = "fail"
                        break
                except subprocess.TimeoutExpired:
                    duration = round(time.monotonic() - cmd_start, 3)
                    command_runs.append(
                        {
                            "command": cmd,
                            "exit_code": -1,
                            "duration_seconds": duration,
                            "error": f"timeout_after_seconds: {timeout_seconds}",
                        }
                    )
                    success = False
                    status = "error"
                    error = f"timeout_after_seconds: {timeout_seconds}"
                    break
    finally:
        if original_sha:
            subprocess.run(
                ["git", "checkout", "--detach", original_sha],
                cwd=str(repo_path),
                text=True,
                capture_output=True,
                check=False,
            )

    stdout_path.write_text("\n".join(stdout_chunks), encoding="utf-8")
    stderr_path.write_text("\n".join(stderr_chunks), encoding="utf-8")

    result = {
        "case_id": case.case_id,
        "repo_id": case.repo_id,
        "approach": "human",
        "human_commit": human_commit,
        "status": status,
        "success": success,
        "error": error,
        "duration_seconds": round(time.monotonic() - started, 3),
        "commands": command_runs,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    summary_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    compat_summary = {
        "case_id": case.case_id,
        "repo_id": case.repo_id,
        "status": "ok",
        "modified": {
            "requested_commit": human_commit,
            "success": success,
            "status": "pass" if success else status,
            "commands": command_runs,
        },
    }
    (case_dir / "case_run_summary.json").write_text(
        json.dumps(compat_summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result["summary_path"] = str(summary_path)
    result["stop_reason"] = "success" if success else ("error" if status == "error" else "still_failing")
    return result


def _aggregate_outcome_rows(rows: list[dict[str, Any]], approaches: list[str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("approach") or "")].append(row)

    table: list[dict[str, Any]] = []
    for approach in approaches:
        bucket = grouped.get(approach, [])
        successes = sum(1 for row in bucket if _to_bool(row.get("success")) is True)
        durations = [
            float(v)
            for v in (_to_float(row.get("duration_seconds")) for row in bucket)
            if v is not None
        ]
        pass_rate = round(successes / float(len(bucket)), 6) if bucket else None
        table.append(
            {
                "approach": approach,
                "runs": len(bucket),
                "successes": successes,
                "pass_rate": pass_rate,
                "avg_runtime_seconds": _safe_mean(durations),
            }
        )
    return table


def _aggregate_rq1_table(rows: list[dict[str, str]], approaches: list[str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("approach") or "")].append(row)

    table: list[dict[str, Any]] = []
    for approach in approaches:
        if approach == "human":
            continue
        bucket = grouped.get(approach, [])
        available = [row for row in bucket if _to_bool(row.get("available")) is True]
        passes = sum(1 for row in available if _to_bool(row.get("pass")) is True)
        times = [x for x in (_to_float(row.get("time_seconds")) for row in available) if x is not None]
        line_sims = [x for x in (_to_float(row.get("patch_line_similarity")) for row in available) if x is not None]
        table.append(
            {
                "approach": approach,
                "rows": len(bucket),
                "available_runs": len(available),
                "pass_rate": round(passes / float(len(available)), 6) if available else None,
                "avg_time_seconds": _safe_mean(times),
                "avg_patch_line_similarity": _safe_mean(line_sims),
            }
        )
    return table


def _aggregate_rq3_table(rows: list[dict[str, str]], approaches: list[str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("approach") or "")].append(row)

    table: list[dict[str, Any]] = []
    for approach in approaches:
        bucket = grouped.get(approach, [])
        available = [row for row in bucket if _to_bool(row.get("available")) is True]
        coverage = [x for x in (_to_float(row.get("coverage_pct")) for row in available) if x is not None]
        mutation = [x for x in (_to_float(row.get("mutation_score_pct")) for row in available) if x is not None]
        runtime = [x for x in (_to_float(row.get("runtime_seconds")) for row in available) if x is not None]
        table.append(
            {
                "approach": approach,
                "rows": len(bucket),
                "available_runs": len(available),
                "avg_coverage_pct": _safe_mean(coverage),
                "avg_mutation_score_pct": _safe_mean(mutation),
                "avg_runtime_seconds": _safe_mean(runtime),
            }
        )
    return table


def _parse_rq2_failure_modes(report_path: Path) -> list[dict[str, Any]]:
    if not report_path.exists():
        return []
    modes: list[dict[str, Any]] = []
    for line in report_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("### "):
            continue
        payload = line[4:].strip()
        if payload.endswith(")") and "(" in payload:
            name, count_txt = payload.rsplit("(", 1)
            count_txt = count_txt[:-1].strip()
            try:
                count = int(count_txt)
            except ValueError:
                continue
            modes.append({"mode": name.strip(), "count": count})
    return modes


def _write_summary_md(
    summary_path: Path,
    config: dict[str, Any],
    selected_case_ids: list[str],
    approaches: list[str],
    with_mapper: bool,
    outcome_table: list[dict[str, Any]],
    rq1_table: list[dict[str, Any]],
    rq3_table: list[dict[str, Any]],
    failure_modes: dict[str, Any],
    artifacts: dict[str, Any],
) -> Path:
    lines: list[str] = []
    lines.append("# Benchmark Summary")
    lines.append("")
    lines.append(f"Generated at: `{_utc_now_iso()}`")
    lines.append("")
    lines.append("## Run Configuration")
    lines.append("")
    lines.append(f"- Cases evaluated: `{len(selected_case_ids)}`")
    lines.append(f"- Case IDs: `{', '.join(selected_case_ids)}`")
    lines.append(f"- Approaches: `{', '.join(approaches)}`")
    lines.append(f"- Mapper scope enabled: `{with_mapper}`")
    lines.append(f"- Max iterations: `{config['max_iterations']}`")
    lines.append(f"- Max minutes: `{config['max_minutes']}`")
    lines.append(f"- Timeout seconds: `{config['timeout_seconds']}`")
    lines.append(f"- EvoSuite seed: `{config['seed']}`")
    lines.append(f"- EvoSuite budget seconds: `{config['budget_seconds']}`")
    lines.append("")
    lines.append("## Outcome Table")
    lines.append("")
    lines.append(
        _markdown_table(
            ["approach", "runs", "successes", "pass_rate", "avg_runtime_seconds"],
            outcome_table,
        )
    )
    lines.append("")
    lines.append("## RQ1 Table")
    lines.append("")
    lines.append(
        _markdown_table(
            ["approach", "rows", "available_runs", "pass_rate", "avg_time_seconds", "avg_patch_line_similarity"],
            rq1_table,
        )
    )
    lines.append("")
    lines.append("## RQ3 Table")
    lines.append("")
    lines.append(
        _markdown_table(
            ["approach", "rows", "available_runs", "avg_coverage_pct", "avg_mutation_score_pct", "avg_runtime_seconds"],
            rq3_table,
        )
    )
    lines.append("")
    lines.append("## Key Failure Modes")
    lines.append("")

    stop_counts: dict[str, int] = failure_modes.get("stop_reasons", {})
    if stop_counts:
        lines.append("### Stop Reasons")
        lines.append("")
        for key, count in sorted(stop_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{key}`: `{count}`")
        lines.append("")

    error_counts: dict[str, int] = failure_modes.get("execution_errors", {})
    if error_counts:
        lines.append("### Execution Errors")
        lines.append("")
        for key, count in sorted(error_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{key}`: `{count}`")
        lines.append("")

    rq2_modes: list[dict[str, Any]] = failure_modes.get("rq2_modes", [])
    if rq2_modes:
        lines.append("### RQ2 Mapper Failure Modes")
        lines.append("")
        for mode in rq2_modes:
            lines.append(f"- `{mode['mode']}`: `{mode['count']}`")
        lines.append("")

    rq3_statuses: dict[str, int] = failure_modes.get("rq3_statuses", {})
    if rq3_statuses:
        lines.append("### RQ3 Status Distribution")
        lines.append("")
        for key, count in sorted(rq3_statuses.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{key}`: `{count}`")
        lines.append("")

    lines.append("## Artifact Index")
    lines.append("")
    for label, path in artifacts.items():
        lines.append(f"- `{label}`: `{path}`")

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def run_benchmark(
    workdir: Path,
    n_cases: int,
    approaches_csv: str,
    with_mapper: bool,
    cases_root: Path = DEFAULT_CASES_ROOT_REL,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = REPO_INDEX_REL,
    benchmark_root: Path = DEFAULT_BENCHMARK_ROOT_REL,
    summary_path: Path = DEFAULT_SUMMARY_REL,
    max_iterations: int = 5,
    max_minutes: int = 30,
    timeout_seconds: int = 1800,
    seed: int = 1337,
    budget_seconds: int = 120,
    config: dict[str, Any] | None = None,
    evosuite_jar: Path | None = None,
    download_evosuite: bool = False,
    evosuite_download_url: str | None = None,
    force_download: bool = False,
    java_bin: str = "java",
    reassert_command: str | None = None,
    enable_direct_reassert: bool = True,
) -> dict[str, Any]:
    approaches = parse_approaches(approaches_csv)
    selected_cases = _select_cases(workdir=workdir, cases_root=cases_root, n_cases=n_cases)
    if not selected_cases:
        raise ValueError("No cases found to benchmark.")

    resolved_benchmark_root = _resolve_path(workdir, benchmark_root)
    resolved_summary_path = _resolve_path(workdir, summary_path)
    resolved_benchmark_root.mkdir(parents=True, exist_ok=True)

    subset_root = _write_case_subset(
        workdir=workdir,
        cases=selected_cases,
        subset_root=resolved_benchmark_root / "cases_subset",
    )
    selected_case_ids = [case.case_id for case in selected_cases]

    run_rows: list[dict[str, Any]] = []
    stop_reason_counts: Counter[str] = Counter()
    execution_error_counts: Counter[str] = Counter()

    if "human" in approaches:
        human_root = resolved_benchmark_root / "human"
        for case in selected_cases:
            human = _run_human_reference_once(
                workdir=workdir,
                case=case,
                repos_root=repos_root,
                repo_index_path=repo_index_path,
                artifacts_root=human_root,
                timeout_seconds=timeout_seconds,
            )
            run_rows.append(
                {
                    "case_id": case.case_id,
                    "repo_id": case.repo_id,
                    "approach": "human",
                    "status": human.get("status"),
                    "success": human.get("success"),
                    "duration_seconds": human.get("duration_seconds"),
                    "stop_reason": human.get("stop_reason"),
                    "error": human.get("error"),
                    "summary_path": human.get("summary_path"),
                }
            )
            if human.get("stop_reason"):
                stop_reason_counts[str(human["stop_reason"])] += 1
            if human.get("error"):
                execution_error_counts[str(human["error"])] += 1

    healing_root = resolved_benchmark_root / "healing"
    if "healing" in approaches:
        for case in selected_cases:
            try:
                healing = run_iterative_healing(
                    workdir=workdir,
                    case_input=case.case_id,
                    repos_root=repos_root,
                    repo_index_path=repo_index_path,
                    artifacts_root=healing_root,
                    max_iterations=max_iterations,
                    max_minutes=max_minutes,
                    timeout_seconds=timeout_seconds,
                    config=config,
                    reassert_command=reassert_command,
                    enable_direct_reassert=enable_direct_reassert,
                    use_mapper_scope=with_mapper,
                )
                success = any(bool(item.get("success")) for item in healing.get("iterations", []))
                stop_reason = str(healing.get("stop_reason") or "unknown")
                row = {
                    "case_id": case.case_id,
                    "repo_id": case.repo_id,
                    "approach": "healing",
                    "status": healing.get("status"),
                    "success": success,
                    "duration_seconds": healing.get("duration_seconds"),
                    "stop_reason": stop_reason,
                    "error": None,
                    "summary_path": healing.get("summary_path"),
                }
                run_rows.append(row)
                stop_reason_counts[stop_reason] += 1
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                run_rows.append(
                    {
                        "case_id": case.case_id,
                        "repo_id": case.repo_id,
                        "approach": "healing",
                        "status": "error",
                        "success": None,
                        "duration_seconds": None,
                        "stop_reason": "error",
                        "error": err,
                        "summary_path": None,
                    }
                )
                stop_reason_counts["error"] += 1
                execution_error_counts[err] += 1

    regen_root = resolved_benchmark_root / "regen"
    if "regen" in approaches:
        for case in selected_cases:
            try:
                regen = run_regenerative_sync(
                    workdir=workdir,
                    case_input=case.case_id,
                    config=config,
                    repos_root=repos_root,
                    repo_index_path=repo_index_path,
                    artifacts_root=regen_root,
                    evosuite_jar=evosuite_jar,
                    download_evosuite=download_evosuite,
                    evosuite_download_url=evosuite_download_url,
                    force_download=force_download,
                    java_bin=java_bin,
                    seed=seed,
                    budget_seconds=budget_seconds,
                    timeout_seconds=timeout_seconds,
                    use_mapper_scope=with_mapper,
                    max_minutes=max_minutes,
                )
                final_suite = regen.get("final_suite") if isinstance(regen.get("final_suite"), dict) else {}
                success = bool(final_suite.get("success"))
                stop_reason = str(regen.get("stop_reason") or "unknown")
                row = {
                    "case_id": case.case_id,
                    "repo_id": case.repo_id,
                    "approach": "regen",
                    "status": regen.get("status"),
                    "success": success,
                    "duration_seconds": regen.get("duration_seconds"),
                    "stop_reason": stop_reason,
                    "error": None,
                    "summary_path": regen.get("summary_path"),
                }
                run_rows.append(row)
                stop_reason_counts[stop_reason] += 1
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                run_rows.append(
                    {
                        "case_id": case.case_id,
                        "repo_id": case.repo_id,
                        "approach": "regen",
                        "status": "error",
                        "success": None,
                        "duration_seconds": None,
                        "stop_reason": "error",
                        "error": err,
                        "summary_path": None,
                    }
                )
                stop_reason_counts["error"] += 1
                execution_error_counts[err] += 1

    outcome_rows = _aggregate_outcome_rows(run_rows, approaches=approaches)
    outcome_csv = _write_csv(
        resolved_benchmark_root / "benchmark_outcomes.csv",
        [
            "case_id",
            "repo_id",
            "approach",
            "status",
            "success",
            "duration_seconds",
            "stop_reason",
            "error",
            "summary_path",
        ],
        run_rows,
    )
    outcome_table_csv = _write_csv(
        workdir / "artifacts" / "tables" / "benchmark_outcomes_table.csv",
        ["approach", "runs", "successes", "pass_rate", "avg_runtime_seconds"],
        outcome_rows,
    )

    rq1_result: dict[str, Any] | None = None
    rq1_table: list[dict[str, Any]] = []
    rq1_csv_path: Path | None = None
    if "healing" in approaches or "regen" in approaches:
        rq1_result = evaluate_rq1_cases(
            workdir=workdir,
            cases_root=subset_root,
            repos_root=repos_root,
            repo_index_path=repo_index_path,
            artifacts_cases_root=resolved_benchmark_root / "human",
            artifacts_healing_root=healing_root,
            artifacts_regen_root=regen_root,
            output_csv=resolved_benchmark_root / "rq1_summary.csv",
            output_cases_root=resolved_benchmark_root / "rq1_cases",
            with_ast_similarity=True,
        )
        rq1_csv_path = Path(str(rq1_result["output_csv"]))
        rq1_rows = _load_csv_rows(rq1_csv_path)
        rq1_table = _aggregate_rq1_table(rq1_rows, approaches=approaches)
        _write_csv(
            workdir / "artifacts" / "tables" / "benchmark_rq1_table.csv",
            ["approach", "rows", "available_runs", "pass_rate", "avg_time_seconds", "avg_patch_line_similarity"],
            rq1_table,
        )

    rq3_result = evaluate_rq3_quality(
        workdir=workdir,
        cases_root=subset_root,
        repos_root=repos_root,
        repo_index_path=repo_index_path,
        artifacts_healing_root=healing_root,
        artifacts_regen_root=regen_root,
        output_csv=resolved_benchmark_root / "rq3_quality.csv",
        output_cases_root=resolved_benchmark_root / "rq3_cases",
        eval_root=resolved_benchmark_root / "rq3_eval",
        timeout_seconds=timeout_seconds,
    )
    rq3_csv_path = Path(str(rq3_result["output_csv"]))
    rq3_rows = _load_csv_rows(rq3_csv_path)
    rq3_table = _aggregate_rq3_table(rq3_rows, approaches=approaches)
    _write_csv(
        workdir / "artifacts" / "tables" / "benchmark_rq3_table.csv",
        ["approach", "rows", "available_runs", "avg_coverage_pct", "avg_mutation_score_pct", "avg_runtime_seconds"],
        rq3_table,
    )

    rq3_statuses: Counter[str] = Counter()
    for row in rq3_rows:
        approach = str(row.get("approach") or "")
        if approach not in approaches:
            continue
        status = str(row.get("status") or "unknown")
        rq3_statuses[status] += 1
        error_value = str(row.get("error") or "").strip()
        if error_value:
            execution_error_counts[error_value] += 1

    rq2_result: dict[str, Any] | None = None
    rq2_modes: list[dict[str, Any]] = []
    try:
        rq2_result = evaluate_rq2_scale(
            workdir=workdir,
            top_k=int((config or {}).get("rq2", {}).get("top_k", 3)),
            output_csv=resolved_benchmark_root / "rq2_metrics.csv",
            output_report=resolved_benchmark_root / "rq2_error_report.md",
        )
        rq2_modes = _parse_rq2_failure_modes(Path(str(rq2_result["error_report"])))
    except Exception:
        rq2_result = None
        rq2_modes = []

    plots_root = workdir / "artifacts" / "plots"
    pass_plot = _write_simple_bar_svg(
        plots_root / "benchmark_pass_rate.svg",
        "Benchmark Pass Rate (%)",
        [(row["approach"], None if row["pass_rate"] is None else float(row["pass_rate"]) * 100.0) for row in outcome_rows],
        max_value=100.0,
    )
    coverage_plot = _write_simple_bar_svg(
        plots_root / "benchmark_coverage.svg",
        "Benchmark Coverage (%)",
        [(row["approach"], _to_float(row.get("avg_coverage_pct"))) for row in rq3_table],
        max_value=100.0,
    )
    mutation_plot = _write_simple_bar_svg(
        plots_root / "benchmark_mutation.svg",
        "Benchmark Mutation Score (%)",
        [(row["approach"], _to_float(row.get("avg_mutation_score_pct"))) for row in rq3_table],
        max_value=100.0,
    )

    artifacts_index = {
        "benchmark_root": str(resolved_benchmark_root),
        "benchmark_outcomes_csv": str(outcome_csv),
        "benchmark_outcomes_table_csv": str(outcome_table_csv),
        "rq1_csv": str(rq1_csv_path) if rq1_csv_path else "",
        "rq3_csv": str(rq3_csv_path),
        "rq2_report": str(rq2_result["error_report"]) if rq2_result else "",
        "pass_plot": str(pass_plot),
        "coverage_plot": str(coverage_plot),
        "mutation_plot": str(mutation_plot),
    }

    summary = _write_summary_md(
        summary_path=resolved_summary_path,
        config={
            "max_iterations": max_iterations,
            "max_minutes": max_minutes,
            "timeout_seconds": timeout_seconds,
            "seed": seed,
            "budget_seconds": budget_seconds,
        },
        selected_case_ids=selected_case_ids,
        approaches=approaches,
        with_mapper=with_mapper,
        outcome_table=outcome_rows,
        rq1_table=rq1_table,
        rq3_table=rq3_table,
        failure_modes={
            "stop_reasons": dict(stop_reason_counts),
            "execution_errors": dict(execution_error_counts),
            "rq2_modes": rq2_modes,
            "rq3_statuses": dict(rq3_statuses),
        },
        artifacts=artifacts_index,
    )

    result = {
        "status": "ok",
        "generated_at_utc": _utc_now_iso(),
        "cases_requested": n_cases,
        "cases_executed": len(selected_cases),
        "case_ids": selected_case_ids,
        "approaches": approaches,
        "with_mapper": with_mapper,
        "benchmark_root": str(resolved_benchmark_root),
        "outcomes_csv": str(outcome_csv),
        "rq1_result": rq1_result,
        "rq3_result": rq3_result,
        "rq2_result": rq2_result,
        "summary_path": str(summary),
    }
    (resolved_benchmark_root / "run_result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result
