from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any
import logging

LOGGER = logging.getLogger(__name__)

from phase2.cases import (
    DEFAULT_INDEX_REL,
    Case,
    save_case,
)
from phase2.java_parser import parse_java_source
from phase2.repo import REPO_INDEX_REL, resolve_repo_path
from phase2.test_runner import run_repo_tests


DEFAULT_MANIFEST_REL = Path("phase2/data/processed/manifest.jsonl")
DEFAULT_PINS_REL = Path("phase2/data/processed/pins.jsonl")
DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_RANKED_OUTPUT_REL = Path("artifacts/change_cases/ranked_candidates.jsonl")


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _run_git(repo_path: Path, args: list[str], check: bool = True):
    # Ensure any previous Java/Maven locks are released (The "Hammer")
    # This is a bit slow, but saves the run on OneDrive/Windows
    import subprocess
    
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True, # This ensures e.stderr is a string, not bytes
        encoding='utf-8',
        errors='replace',
        check=check
    )
    return result.stdout.strip()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line:
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            items.append(payload)
    return items


def _manifest_by_id(manifest_path: Path) -> dict[str, dict[str, Any]]:
    records = _load_jsonl(manifest_path)
    by_id: dict[str, dict[str, Any]] = {}
    for idx, record in enumerate(records):
        key = str(record.get("dataset_id") or f"index_{idx}")
        by_id[key] = record
    return by_id


def _usable_pins(pins_path: Path) -> list[dict[str, Any]]:
    return [row for row in _load_jsonl(pins_path) if bool(row.get("usable"))]


def _commit_list_touching_file(
    repo_path: Path,
    base_commit: str,
    file_path: str,
    max_commits: int,
) -> list[str]:
    args = [
        "rev-list",
        "--ancestry-path",
        "--reverse",
        f"{base_commit}..HEAD",
        "--",
        file_path,
    ]
    output = _run_git(repo_path, args)
    commits = [line.strip() for line in output.splitlines() if line.strip()]
    if commits:
        return commits[: max_commits] if max_commits > 0 else commits

    # Fallback: if no descendants from base touch the file, search nearby ancestor history.
    fallback_output = _run_git(
        repo_path,
        [
            "rev-list",
            "--first-parent",
            "--max-count",
            str(max(max_commits, 1)),
            base_commit,
            "--",
            file_path,
        ],
    )
    fallback = [line.strip() for line in fallback_output.splitlines() if line.strip()]
    fallback = [sha for sha in fallback if sha != base_commit]
    return fallback[: max_commits] if max_commits > 0 else fallback


def _line_range_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def _method_range_in_source(source: str, method_name: str) -> tuple[int, int] | None:
    parsed = parse_java_source(source, path="<git-show>")
    matches = [m for m in parsed.get("methods", []) if str(m.get("name") or "") == method_name]
    if not matches:
        return None
    target = matches[0]
    return int(target["line"]), int(target["end_line"])


def _commit_touches_file(repo_path: Path, commit: str, file_path: str) -> bool:
    output = _run_git(repo_path, ["diff-tree", "--no-commit-id", "--name-only", "-r", commit, "--", file_path])
    return bool(output.strip())


def _commit_touches_method_region(
    repo_path: Path,
    commit: str,
    focal_file_path: str,
    mapped_focal_method: str,
) -> bool:
    try:
        parent = _run_git(repo_path, ["rev-parse", f"{commit}^"])
    except subprocess.CalledProcessError:
        return False

    try:
        parent_source = _run_git(repo_path, ["show", f"{parent}:{focal_file_path}"])
    except subprocess.CalledProcessError:
        return False

    line_range = _method_range_in_source(parent_source, mapped_focal_method)
    if line_range is None:
        return False
    method_start, method_end = line_range

    diff = _run_git(repo_path, ["diff", "--unified=0", parent, commit, "--", focal_file_path], check=False)
    hunk_pattern = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    for m in hunk_pattern.finditer(diff):
        old_start = int(m.group(1))
        old_count = int(m.group(2) or "1")
        old_end = old_start + max(old_count, 1) - 1
        if old_count == 0:
            old_end = old_start
        if _line_range_overlap(method_start, method_end, old_start, old_end):
            return True
    return False


def _nearby_test_update(
    repo_path: Path,
    candidate_commit: str,
    test_file_path: str,
    nearby_window: int,
) -> tuple[str | None, int | None]:
    if _commit_touches_file(repo_path, candidate_commit, test_file_path):
        return candidate_commit, 0

    commits_after = _run_git(
        repo_path,
        [
            "rev-list",
            "--ancestry-path",
            "--reverse",
            "--max-count",
            str(max(nearby_window, 0)),
            f"{candidate_commit}..HEAD",
        ],
        check=False,
    )
    commits = [line.strip() for line in commits_after.splitlines() if line.strip()]
    for distance, sha in enumerate(commits, start=1):
        if _commit_touches_file(repo_path, sha, test_file_path):
            return sha, distance
    return None, None


def _detect_build_commands(repo_path: Path) -> list[str]:
    mvnw = repo_path / "mvnw"
    mvnw_cmd = repo_path / "mvnw.cmd"
    gradlew = repo_path / "gradlew"
    gradlew_bat = repo_path / "gradlew.bat"
    if mvnw_cmd.exists():
        return [str(mvnw_cmd), "-B", "test"]
    if mvnw.exists():
        return [str(mvnw), "-B", "test"]
    if gradlew_bat.exists():
        return [str(gradlew_bat), "test", "--console=plain"]
    if gradlew.exists():
        return [str(gradlew), "test", "--console=plain"]
    if (repo_path / "pom.xml").exists():
        return ["mvn", "-B", "test"]
    if (repo_path / "build.gradle").exists() or (repo_path / "build.gradle.kts").exists():
        return ["gradle", "test", "--console=plain"]
    return ["<build-tool-not-detected>"]


def _failures_appear(base_result: dict[str, Any], candidate_result: dict[str, Any]) -> bool:
    base_status = str(base_result.get("status") or "")
    candidate_status = str(candidate_result.get("status") or "")
    base_fail_count = int(base_result.get("failing_test_count") or 0)
    candidate_fail_count = int(candidate_result.get("failing_test_count") or 0)
    if base_status == "pass" and candidate_status in {"fail", "error"}:
        return True
    if candidate_fail_count > base_fail_count:
        return True
    return False


def _candidate_score(
    touches_method_region: bool,
    failures_appear: bool,
    candidate_test_status: str,
    human_update_distance: int | None,
    nearby_window: int,
) -> float:
    score = 0.0
    if touches_method_region:
        score += 2.0
    if failures_appear:
        score += 3.5
    if candidate_test_status == "fail":
        score += 1.0
    if human_update_distance is not None:
        score += 2.0
        if nearby_window > 0:
            score += max(0.0, (nearby_window - human_update_distance) / nearby_window)
    return round(score, 6)


def _write_ranked_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")
    return path


def mine_change_cases(
    workdir: Path,
    manifest_path: Path = DEFAULT_MANIFEST_REL,
    pins_path: Path = DEFAULT_PINS_REL,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = REPO_INDEX_REL,
    ranked_output_path: Path = DEFAULT_RANKED_OUTPUT_REL,
    max_commits_per_record: int = 50,
    nearby_window: int = 20,
    selected_per_record: int = 1,
    run_tests: bool = True,
    test_timeout_seconds: int = 1800,
) -> dict[str, Any]:
    resolved_manifest = _resolve_path(workdir, manifest_path)
    resolved_pins = _resolve_path(workdir, pins_path)
    resolved_ranked_output = _resolve_path(workdir, ranked_output_path)

    manifest = _manifest_by_id(resolved_manifest)
    pins = _usable_pins(resolved_pins)

    ranked_rows: list[dict[str, Any]] = []
    selected_case_paths: list[str] = []
    selected_case_ids: list[str] = []

    for pin in pins:
        record_id = str(pin.get("dataset_id") or "")
        if not record_id:
            continue
        record = manifest.get(record_id)
        if record is None:
            continue

        repo_id = str(record.get("repository_repo_id") or pin.get("repo_id") or "")
        if not repo_id:
            repo_id = str(pin.get("repo_id") or "")
        if not repo_id:
            continue

        repo_path = resolve_repo_path(
            workdir=workdir,
            repo_id=repo_id,
            repos_root=repos_root,
            index_path=repo_index_path,
        )
        if not (repo_path / ".git").exists():
            continue

        base_commit = str(pin.get("base_commit") or "")
        if not base_commit:
            continue

        focal_file_path = str(record.get("focal_file_path") or pin.get("focal_file_path") or "")
        test_file_path = str(record.get("test_file_path") or pin.get("test_file_path") or "")
        mapped_focal_method = str(record.get("labeled_focal_method") or pin.get("labeled_focal_method") or "")
        mapped_test_method = str(record.get("labeled_test_method") or pin.get("labeled_test_method") or "")
        if not focal_file_path or not test_file_path or not mapped_focal_method or not mapped_test_method:
            continue

        original_sha = _run_git(repo_path, ["rev-parse", "HEAD"])
        baseline_test_result: dict[str, Any] | None = None
        build_command_tokens = _detect_build_commands(repo_path)
        build_command_text = " ".join(shlex.quote(token) for token in build_command_tokens)

        try:
            # --- New: Global Git Protection ---
            def robust_git_prep(p):
                try:
                    _run_git(p, ["reset", "--hard", "HEAD"])
                    _run_git(p, ["clean", "-fd"])
                except subprocess.CalledProcessError as e:
                    LOGGER.warning(f"Initial cleanup failed for {repo_id}, trying to proceed anyway...")

            robust_git_prep(repo_path)
            
            try:
                _run_git(repo_path, ["checkout", "--detach", base_commit])
            except subprocess.CalledProcessError as e:
                error_msg = e.stderr.decode(errors='replace') if isinstance(e.stderr, bytes) else str(e)
                LOGGER.error(f"Abandoning repo {repo_id}: {error_msg}")
                continue

            if run_tests:
                baseline_test_result = run_repo_tests(
                    workdir=workdir,
                    repo_id=repo_id,
                    repos_root=repos_root,
                    repo_index_path=repo_index_path,
                    timeout_seconds=test_timeout_seconds,
                )

            commits = _commit_list_touching_file(
                repo_path=repo_path,
                base_commit=base_commit,
                file_path=focal_file_path,
                max_commits=max_commits_per_record,
            )
            per_record: list[dict[str, Any]] = []

            for modified_commit in commits:
                # Wrap EVERYTHING inside the commit loop
                try:
                    robust_git_prep(repo_path)
                    _run_git(repo_path, ["checkout", "--detach", modified_commit])

                    # --- ARTIFICIAL BREAKAGE STEP ---
                    # Keep new source logic, but revert the test to the old version.
                    # This ensures the test fails, giving the AI a repair task.
                    _run_git(repo_path, ["checkout", base_commit, "--", test_file_path])
                    # --------------------------------
                except subprocess.CalledProcessError:
                    continue 

                touches_method_region = _commit_touches_method_region(
                    repo_path=repo_path,
                    commit=modified_commit,
                    focal_file_path=focal_file_path,
                    mapped_focal_method=mapped_focal_method,
                )

                human_update_commit, human_update_distance = _nearby_test_update(
                    repo_path=repo_path,
                    candidate_commit=modified_commit,
                    test_file_path=test_file_path,
                    nearby_window=nearby_window,
                )

                candidate_test_result: dict[str, Any] | None = None
                failures_appear = False
                candidate_status = ""
                if run_tests:
                    candidate_test_result = run_repo_tests(
                        workdir=workdir,
                        repo_id=repo_id,
                        repos_root=repos_root,
                        repo_index_path=repo_index_path,
                        timeout_seconds=test_timeout_seconds,
                    )
                    candidate_status = str(candidate_test_result.get("status") or "")
                    if baseline_test_result is not None:
                        failures_appear = _failures_appear(baseline_test_result, candidate_test_result)

                score = _candidate_score(
                    touches_method_region=touches_method_region,
                    failures_appear=failures_appear,
                    candidate_test_status=candidate_status,
                    human_update_distance=human_update_distance,
                    nearby_window=nearby_window,
                )
                row = {
                    "record_id": record_id,
                    "repo_id": repo_id,
                    "base_commit": base_commit,
                    "modified_commit": modified_commit,
                    "focal_file_path": focal_file_path,
                    "test_file_path": test_file_path,
                    "mapped_focal_method": mapped_focal_method,
                    "mapped_test_method": mapped_test_method,
                    "touches_method_region": touches_method_region,
                    "failures_appear": failures_appear,
                    "candidate_test_status": candidate_status,
                    "baseline_test_status": (
                        str((baseline_test_result or {}).get("status") or "") if run_tests else ""
                    ),
                    "human_update_found": human_update_commit is not None,
                    "human_update_commit": human_update_commit,
                    "human_update_distance": human_update_distance,
                    "score": score,
                    "build_commands": [build_command_text],
                    "candidate_test_result_path": (
                        str((candidate_test_result or {}).get("result_path") or "") if run_tests else ""
                    ),
                    "baseline_test_result_path": (
                        str((baseline_test_result or {}).get("result_path") or "") if run_tests else ""
                    ),
                }
                per_record.append(row)

            per_record.sort(
                key=lambda r: (
                    -float(r.get("score") or 0.0),
                    bool(r.get("touches_method_region")),
                    bool(r.get("failures_appear")),
                    str(r.get("modified_commit") or ""),
                ),
                reverse=False,
            )
            per_record = sorted(
                per_record,
                key=lambda r: (
                    -float(r.get("score") or 0.0),
                    -int(bool(r.get("touches_method_region"))),
                    -int(bool(r.get("failures_appear"))),
                    str(r.get("modified_commit") or ""),
                ),
            )

            for rank, row in enumerate(per_record, start=1):
                row["rank"] = rank
                ranked_rows.append(row)

            for row in per_record[: max(1, selected_per_record)]:
                case_payload = {
                    "repo_id": repo_id,
                    "base_commit": base_commit,
                    "modified_commit": row["modified_commit"],
                    "focal_file_path": focal_file_path,
                    "test_file_path": test_file_path,
                    "mapped_focal_method": mapped_focal_method,
                    "mapped_test_method": mapped_test_method,
                    "build_commands": row["build_commands"],
                    "metadata": {
                        "record_id": record_id,
                        "score": row["score"],
                        "rank": row["rank"],
                        "touches_method_region": row["touches_method_region"],
                        "failures_appear": row["failures_appear"],
                        "candidate_test_status": row["candidate_test_status"],
                        "baseline_test_status": row["baseline_test_status"],
                        "human_update_found": row["human_update_found"],
                        "human_update_commit": row["human_update_commit"],
                        "human_update_distance": row["human_update_distance"],
                        "candidate_test_result_path": row["candidate_test_result_path"],
                        "baseline_test_result_path": row["baseline_test_result_path"],
                    },
                }
                case_obj, case_path = save_case(case_payload, workdir=workdir, index_rel=DEFAULT_INDEX_REL)
                selected_case_ids.append(case_obj.case_id)
                selected_case_paths.append(str(case_path))
        
        finally:
            _run_git(repo_path, ["checkout", "--detach", original_sha], check=False)

    _write_ranked_jsonl(resolved_ranked_output, ranked_rows)
    return {
        "status": "ok",
        "records_considered": len(pins),
        "ranked_candidates": len(ranked_rows),
        "ranked_output_path": str(resolved_ranked_output),
        "selected_case_count": len(selected_case_ids),
        "selected_case_ids": selected_case_ids,
        "selected_case_paths": selected_case_paths,
    }

