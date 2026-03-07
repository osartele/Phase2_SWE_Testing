from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from phase2.cases import Case, list_cases, load_case, load_case_by_id
from phase2.healing import run_iterative_healing
from phase2.regen import run_regenerative_sync


DEFAULT_CASES_ROOT_REL = Path("cases")
DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_REPO_INDEX_REL = Path("phase2/data/processed/repo_index.json")
DEFAULT_ABLATIONS_ROOT_REL = Path("artifacts/ablations")
DEFAULT_ABLATIONS_CSV_REL = Path("artifacts/ablations/ablation_summary.csv")


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _resolve_case_input(case_input: str, workdir: Path) -> Case:
    candidate = Path(case_input).expanduser()
    if candidate.exists():
        return load_case(candidate if candidate.is_absolute() else workdir / candidate)
    return load_case_by_id(case_input, workdir=workdir)


def _variant_matrix() -> list[dict[str, Any]]:
    return [
        {"variant_id": "healing_mapper_on", "approach": "healing", "use_mapper_scope": True},
        {"variant_id": "healing_mapper_off", "approach": "healing", "use_mapper_scope": False},
        {"variant_id": "regen_mapper_on", "approach": "regen", "use_mapper_scope": True},
        {"variant_id": "regen_mapper_off", "approach": "regen", "use_mapper_scope": False},
    ]


def _standardized_stop_reason(approach: str, raw_reason: str, success: bool) -> str:
    if success:
        return "success"
    clean = (raw_reason or "").strip().lower()
    if "time_budget" in clean or "iteration_budget" in clean:
        return "budget_exhausted"
    if clean.startswith("no_progress"):
        return "no_progress"
    if clean.startswith("suite_still_failing"):
        return "still_failing"
    if clean:
        return clean
    return "unknown"


def _collect_case_targets(
    workdir: Path,
    case_input: str | None,
    run_all_cases: bool,
    cases_root: Path,
) -> list[Case]:
    if run_all_cases:
        resolved_cases_root = _resolve_path(workdir, cases_root)
        return list_cases(
            workdir=workdir,
            index_rel=Path(str(resolved_cases_root / "index" / "cases.jsonl")),
        )
    if not case_input:
        raise ValueError("Provide --case or use --all-cases.")
    return [_resolve_case_input(case_input, workdir=workdir)]


def run_ablation_variants(
    workdir: Path,
    case_input: str | None = None,
    run_all_cases: bool = False,
    cases_root: Path = DEFAULT_CASES_ROOT_REL,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = DEFAULT_REPO_INDEX_REL,
    artifacts_root: Path = DEFAULT_ABLATIONS_ROOT_REL,
    output_csv: Path = DEFAULT_ABLATIONS_CSV_REL,
    max_iterations: int = 5,
    max_minutes: int = 30,
    timeout_seconds: int = 1800,
    shared_budget_seconds: int | None = None,
    seed: int = 1337,
    config: dict[str, Any] | None = None,
    evosuite_jar: Path | None = None,
    download_evosuite: bool = False,
    evosuite_download_url: str | None = None,
    force_download: bool = False,
    java_bin: str = "java",
    reassert_command: str | None = None,
    enable_direct_reassert: bool = True,
) -> dict[str, Any]:
    resolved_artifacts_root = _resolve_path(workdir, artifacts_root)
    resolved_output_csv = _resolve_path(workdir, output_csv)
    resolved_artifacts_root.mkdir(parents=True, exist_ok=True)
    resolved_output_csv.parent.mkdir(parents=True, exist_ok=True)

    budget_seconds = shared_budget_seconds if shared_budget_seconds is not None else max(1, max_minutes * 60)
    targets = _collect_case_targets(
        workdir=workdir,
        case_input=case_input,
        run_all_cases=run_all_cases,
        cases_root=cases_root,
    )
    variants = _variant_matrix()

    rows: list[dict[str, Any]] = []
    per_case_paths: list[str] = []
    completed = 0

    for case in targets:
        case_dir = resolved_artifacts_root / case.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        case_summary: dict[str, Any] = {
            "case_id": case.case_id,
            "repo_id": case.repo_id,
            "controls": {
                "max_iterations": max_iterations,
                "max_minutes": max_minutes,
                "shared_budget_seconds": budget_seconds,
                "timeout_seconds": timeout_seconds,
                "seed": seed,
                "consistent_stopping_criteria": [
                    "success",
                    "no_progress",
                    "budget_exhausted",
                    "still_failing",
                ],
            },
            "variants": {},
        }

        for variant in variants:
            variant_id = str(variant["variant_id"])
            approach = str(variant["approach"])
            use_mapper_scope = bool(variant["use_mapper_scope"])
            variant_root = case_dir / variant_id
            variant_root.mkdir(parents=True, exist_ok=True)

            row: dict[str, Any] = {
                "case_id": case.case_id,
                "repo_id": case.repo_id,
                "variant_id": variant_id,
                "approach": approach,
                "mapper_scope_enabled": use_mapper_scope,
                "max_iterations": max_iterations,
                "max_minutes": max_minutes,
                "shared_budget_seconds": budget_seconds,
                "timeout_seconds": timeout_seconds,
                "seed": seed,
                "status": "ok",
                "success": None,
                "raw_stop_reason": None,
                "standardized_stop_reason": None,
                "duration_seconds": None,
                "summary_path": None,
                "error": None,
            }

            try:
                if approach == "healing":
                    run = run_iterative_healing(
                        workdir=workdir,
                        case_input=case.case_id,
                        repos_root=repos_root,
                        repo_index_path=repo_index_path,
                        artifacts_root=variant_root / "healing",
                        max_iterations=max_iterations,
                        max_minutes=max_minutes,
                        timeout_seconds=timeout_seconds,
                        config=config,
                        reassert_command=reassert_command,
                        enable_direct_reassert=enable_direct_reassert,
                        use_mapper_scope=use_mapper_scope,
                    )
                    success = any(bool(item.get("success")) for item in run.get("iterations", []))
                    raw_stop_reason = str(run.get("stop_reason") or "")
                    row["duration_seconds"] = run.get("duration_seconds")
                    row["summary_path"] = run.get("summary_path")
                else:
                    run = run_regenerative_sync(
                        workdir=workdir,
                        case_input=case.case_id,
                        config=config,
                        repos_root=repos_root,
                        repo_index_path=repo_index_path,
                        artifacts_root=variant_root / "regen",
                        evosuite_jar=evosuite_jar,
                        download_evosuite=download_evosuite,
                        evosuite_download_url=evosuite_download_url,
                        force_download=force_download,
                        java_bin=java_bin,
                        seed=seed,
                        budget_seconds=budget_seconds,
                        timeout_seconds=timeout_seconds,
                        use_mapper_scope=use_mapper_scope,
                        max_minutes=max_minutes,
                    )
                    final_suite = run.get("final_suite") if isinstance(run.get("final_suite"), dict) else {}
                    success = bool(final_suite.get("success"))
                    raw_stop_reason = str(run.get("stop_reason") or "")
                    row["duration_seconds"] = run.get("duration_seconds")
                    row["summary_path"] = run.get("summary_path")

                row["success"] = success
                row["raw_stop_reason"] = raw_stop_reason
                row["standardized_stop_reason"] = _standardized_stop_reason(
                    approach=approach,
                    raw_reason=raw_stop_reason,
                    success=bool(success),
                )
                case_summary["variants"][variant_id] = {
                    "status": "ok",
                    "result": run,
                    "row": row,
                }
                completed += 1
            except Exception as exc:
                row["status"] = "error"
                row["error"] = f"{type(exc).__name__}: {exc}"
                row["standardized_stop_reason"] = "error"
                case_summary["variants"][variant_id] = {
                    "status": "error",
                    "error": row["error"],
                    "row": row,
                }
            rows.append(row)

        case_summary_path = case_dir / "ablation_summary.json"
        case_summary_path.write_text(json.dumps(case_summary, indent=2, sort_keys=True), encoding="utf-8")
        per_case_paths.append(str(case_summary_path))

    columns = [
        "case_id",
        "repo_id",
        "variant_id",
        "approach",
        "mapper_scope_enabled",
        "max_iterations",
        "max_minutes",
        "shared_budget_seconds",
        "timeout_seconds",
        "seed",
        "status",
        "success",
        "raw_stop_reason",
        "standardized_stop_reason",
        "duration_seconds",
        "summary_path",
        "error",
    ]
    with resolved_output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return {
        "status": "ok",
        "cases_total": len(targets),
        "variants_per_case": len(variants),
        "runs_completed": completed,
        "rows_written": len(rows),
        "output_csv": str(resolved_output_csv),
        "artifacts_root": str(resolved_artifacts_root),
        "per_case_paths": per_case_paths,
    }
