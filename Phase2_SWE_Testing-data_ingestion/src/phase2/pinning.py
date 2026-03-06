import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from phase2.repo import resolve_repo_id_from_manifest_record


LOGGER = logging.getLogger(__name__)

DEFAULT_MANIFEST_REL = Path("phase2/data/processed/manifest.jsonl")
DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_REPO_INDEX_REL = Path("phase2/data/processed/repo_index.json")
DEFAULT_PINS_REL = Path("phase2/data/processed/pins.jsonl")


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _run_git(args: list[str], cwd: Path) -> str:
    command = ["git", *args]
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _load_manifest_records(manifest_path: Path) -> list[dict[str, Any]]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

    records: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(
        manifest_path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{manifest_path}:{line_no}: expected JSON object per line")
        records.append(payload)
    if not records:
        raise ValueError(f"Manifest is empty: {manifest_path}")
    return records


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


def _repo_start_sha(repo_path: Path, repo_id: str, repo_index: dict[str, Any]) -> str:
    repos = repo_index.get("repos", {})
    entry = repos.get(repo_id)
    if isinstance(entry, dict):
        sha = entry.get("current_sha")
        if isinstance(sha, str) and sha.strip():
            return sha.strip()
    return _run_git(["rev-parse", "HEAD"], cwd=repo_path)


def _workspace_file_content(repo_path: Path, rel_path: str) -> str | None:
    target = repo_path / Path(rel_path)
    if not target.exists() or not target.is_file():
        return None
    return target.read_text(encoding="utf-8", errors="replace")


def _git_file_content(repo_path: Path, commit: str, rel_path: str) -> str | None:
    git_rel_path = rel_path.replace("\\", "/")
    try:
        return _run_git(["show", f"{commit}:{git_rel_path}"], cwd=repo_path)
    except subprocess.CalledProcessError:
        return None


def _method_exists_in_source(source: str | None, identifier: str) -> bool:
    if source is None:
        return False
    method_name = identifier.strip()
    if not method_name:
        return False

    # Approximate Java method declaration detection with optional annotations/modifiers.
    pattern_text = (
        r"(?ms)^[ \t]*(?:@\w+(?:\([^)]*\))?[ \t]*\n[ \t]*)*"
        r"(?:(?:public|protected|private|static|final|native|synchronized|abstract|strictfp|default)\s+)*"
        r"(?:<[^>]+>\s*)?"
        r"(?:[\w\[\]<>.,?]+\s+)?"
        + re.escape(method_name)
        + r"\s*\([^;{)]*\)\s*(?:throws\s+[^{]+)?\s*\{"
    )
    pattern = re.compile(pattern_text)
    return pattern.search(source) is not None


def _status_from_sources(
    test_source: str | None,
    focal_source: str | None,
    test_method: str,
    focal_method: str,
) -> dict[str, bool]:
    test_file_exists = test_source is not None
    focal_file_exists = focal_source is not None
    test_method_exists = _method_exists_in_source(test_source, test_method)
    focal_method_exists = _method_exists_in_source(focal_source, focal_method)
    return {
        "test_file_exists": test_file_exists,
        "focal_file_exists": focal_file_exists,
        "test_method_exists": test_method_exists,
        "focal_method_exists": focal_method_exists,
        "all_present": test_file_exists
        and focal_file_exists
        and test_method_exists
        and focal_method_exists,
    }


def _reason_from_status(status: dict[str, bool]) -> str:
    if not status["test_file_exists"] and not status["focal_file_exists"]:
        return "missing_test_and_focal_files"
    if not status["test_file_exists"]:
        return "missing_test_file"
    if not status["focal_file_exists"]:
        return "missing_focal_file"
    if not status["test_method_exists"] and not status["focal_method_exists"]:
        return "missing_test_and_focal_methods"
    if not status["test_method_exists"]:
        return "missing_test_method"
    if not status["focal_method_exists"]:
        return "missing_focal_method"
    return "unknown"


def _candidate_commits(repo_path: Path, start_sha: str, window: int) -> list[str]:
    if window < 1:
        raise ValueError("Search window must be >= 1.")
    output = _run_git(["rev-list", "--max-count", str(window), start_sha], cwd=repo_path)
    commits = [line.strip() for line in output.splitlines() if line.strip()]
    if not commits:
        return [start_sha]
    return commits


def _record_sort_key(indexed_record: tuple[int, dict[str, Any]]) -> tuple[str, int]:
    idx, record = indexed_record
    dataset_id = str(record.get("dataset_id") or "")
    return (dataset_id, idx)


def verify_base_commit_pins(
    workdir: Path,
    manifest_path: Path = DEFAULT_MANIFEST_REL,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = DEFAULT_REPO_INDEX_REL,
    output_path: Path = DEFAULT_PINS_REL,
    search_window: int = 200,
) -> tuple[Path, list[dict[str, Any]]]:
    resolved_manifest = _resolve_path(workdir, manifest_path)
    resolved_repos_root = _resolve_path(workdir, repos_root)
    resolved_repo_index = _resolve_path(workdir, repo_index_path)
    resolved_output = _resolve_path(workdir, output_path)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)

    repo_index = _load_repo_index(resolved_repo_index)
    records = _load_manifest_records(resolved_manifest)

    results: list[dict[str, Any]] = []
    for _, record in sorted(enumerate(records), key=_record_sort_key):
        dataset_id = str(record.get("dataset_id", ""))
        repo_id = resolve_repo_id_from_manifest_record(record)
        repo_url = str(record.get("repository_url", ""))
        test_file_path = str(record.get("test_file_path", ""))
        focal_file_path = str(record.get("focal_file_path", ""))
        test_method = str(record.get("labeled_test_method", ""))
        focal_method = str(record.get("labeled_focal_method", ""))

        result: dict[str, Any] = {
            "dataset_id": dataset_id,
            "repo_id": repo_id,
            "repository_url": repo_url,
            "repo_path": str(resolved_repos_root / repo_id),
            "test_file_path": test_file_path,
            "focal_file_path": focal_file_path,
            "labeled_test_method": test_method,
            "labeled_focal_method": focal_method,
            "search_window": search_window,
            "usable": False,
            "reason": "",
            "base_commit": None,
            "start_commit": None,
            "commits_checked": 0,
        }

        repo_path = resolved_repos_root / repo_id
        if not (repo_path / ".git").exists():
            result["reason"] = "repo_not_cloned"
            results.append(result)
            continue

        try:
            start_sha = _repo_start_sha(repo_path, repo_id, repo_index)
            result["start_commit"] = start_sha

            worktree_test = _workspace_file_content(repo_path, test_file_path)
            worktree_focal = _workspace_file_content(repo_path, focal_file_path)
            checked_out_status = _status_from_sources(
                test_source=worktree_test,
                focal_source=worktree_focal,
                test_method=test_method,
                focal_method=focal_method,
            )
            if checked_out_status["all_present"]:
                result["usable"] = True
                result["base_commit"] = start_sha
                result["reason"] = "verified_at_checked_out_commit"
                result["commits_checked"] = 1
                results.append(result)
                continue

            last_status = checked_out_status
            checked = 0
            for candidate_sha in _candidate_commits(repo_path, start_sha, search_window):
                checked += 1
                test_source = _git_file_content(repo_path, candidate_sha, test_file_path)
                focal_source = _git_file_content(repo_path, candidate_sha, focal_file_path)
                candidate_status = _status_from_sources(
                    test_source=test_source,
                    focal_source=focal_source,
                    test_method=test_method,
                    focal_method=focal_method,
                )
                if candidate_status["all_present"]:
                    result["usable"] = True
                    result["base_commit"] = candidate_sha
                    result["reason"] = (
                        "verified_at_checked_out_commit"
                        if candidate_sha == start_sha
                        else "verified_in_nearby_history"
                    )
                    result["commits_checked"] = checked
                    break
                last_status = candidate_status

            if not result["usable"]:
                result["reason"] = f"unusable_{_reason_from_status(last_status)}_within_window"
                result["commits_checked"] = checked
        except subprocess.CalledProcessError as exc:
            LOGGER.exception("Git command failed for repo_id=%s", repo_id)
            result["reason"] = f"git_error_{exc.returncode}"
        except Exception as exc:  # pragma: no cover - defensive error capture for batch runs
            LOGGER.exception("Pin verification failed for repo_id=%s dataset_id=%s", repo_id, dataset_id)
            result["reason"] = f"error_{type(exc).__name__}"

        results.append(result)

    with resolved_output.open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")

    return resolved_output, results
