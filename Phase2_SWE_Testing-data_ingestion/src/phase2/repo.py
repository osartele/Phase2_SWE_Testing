import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


LOGGER = logging.getLogger(__name__)

DEFAULT_MANIFEST_REL = Path("phase2/data/processed/manifest.jsonl")
DEFAULT_REPOS_ROOT_REL = Path("repos")
REPO_INDEX_REL = Path("phase2/data/processed/repo_index.json")


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    command = ["git", *args]
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
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


def _repo_id_from_url(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) >= 2:
        owner = parts[-2]
        name = parts[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return f"{owner}_{name}"
    if parts:
        return parts[-1].replace(".git", "")
    return "repo"


def _repo_id_from_record(record: dict[str, Any]) -> str:
    repo_id = record.get("repository_repo_id")
    if repo_id is not None:
        repo_id_text = str(repo_id).strip()
        if repo_id_text:
            return repo_id_text

    repo_url = record.get("repository_url")
    if not isinstance(repo_url, str) or not repo_url.strip():
        raise ValueError("Manifest record missing repository_url")
    return _repo_id_from_url(repo_url)


def resolve_repo_id_from_manifest_record(record: dict[str, Any]) -> str:
    return _repo_id_from_record(record)


def resolve_repo_path(
    workdir: Path,
    repo_id: str,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    index_path: Path = REPO_INDEX_REL,
) -> Path:
    resolved_repos_root = _resolve_path(workdir, repos_root)
    resolved_index_path = _resolve_path(workdir, index_path)
    index = _load_repo_index(resolved_index_path)
    entry = index.get("repos", {}).get(repo_id)
    if isinstance(entry, dict):
        path_value = entry.get("path")
        if isinstance(path_value, str) and path_value.strip():
            candidate = Path(path_value).expanduser()
            if not candidate.is_absolute():
                candidate = (workdir / candidate).resolve()
            return candidate
    return resolved_repos_root / repo_id


def _repo_targets(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    targets: dict[str, str] = {}
    for record in records:
        repo_id = _repo_id_from_record(record)
        repo_url = record.get("repository_url")
        if not isinstance(repo_url, str) or not repo_url.strip():
            raise ValueError(f"Manifest record missing repository_url for repo_id={repo_id}")
        existing_url = targets.get(repo_id)
        if existing_url is not None and existing_url != repo_url:
            raise ValueError(
                f"Conflicting repository URLs for repo_id={repo_id}: {existing_url} vs {repo_url}"
            )
        targets[repo_id] = repo_url
    return sorted(targets.items(), key=lambda item: item[0])


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


def _write_repo_index(index_path: Path, index: dict[str, Any]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")


def _update_repo_index(
    workdir: Path,
    index_path: Path,
    manifest_path: Path,
    repos_root: Path,
    states: list[dict[str, Any]],
) -> Path:
    index = _load_repo_index(index_path)
    repos_index = index["repos"]
    for state in states:
        repo_id = state["repo_id"]
        repos_index[repo_id] = {
            "repo_url": state["repo_url"],
            "path": state["path"],
            "current_sha": state["current_sha"],
            "last_updated_utc": _utc_now_iso(),
        }

    index["manifest_path"] = str(manifest_path)
    index["repos_root"] = str(repos_root)
    index["workdir"] = str(workdir)
    index["updated_at_utc"] = _utc_now_iso()
    _write_repo_index(index_path, index)
    return index_path


def _ensure_clone(repo_url: str, repo_path: Path) -> bool:
    git_dir = repo_path / ".git"
    if git_dir.exists():
        LOGGER.info("Reusing existing clone at %s", repo_path)
        return True
    if repo_path.exists() and any(repo_path.iterdir()):
        raise ValueError(f"Repo path exists but is not a git clone: {repo_path}")

    repo_path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Cloning %s into %s", repo_url, repo_path)
    _run_git(["clone", repo_url, str(repo_path)])
    return False


def _fetch_all_refs(repo_path: Path) -> None:
    _run_git(["fetch", "--all", "--tags", "--prune"], cwd=repo_path)


def _current_sha(repo_path: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], cwd=repo_path)


def clone_from_manifest(
    workdir: Path,
    manifest_path: Path = DEFAULT_MANIFEST_REL,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    index_path: Path = REPO_INDEX_REL,
) -> tuple[Path, list[dict[str, Any]]]:
    resolved_manifest = _resolve_path(workdir, manifest_path)
    resolved_repos_root = _resolve_path(workdir, repos_root)
    resolved_index_path = _resolve_path(workdir, index_path)

    records = _load_manifest_records(resolved_manifest)
    targets = _repo_targets(records)

    states: list[dict[str, Any]] = []
    for repo_id, repo_url in targets:
        repo_path = resolved_repos_root / repo_id
        
        # --- Fault-Tolerant Cloning Block ---
        try:
            reused = _ensure_clone(repo_url=repo_url, repo_path=repo_path)
            _fetch_all_refs(repo_path=repo_path)
            sha = _current_sha(repo_path=repo_path)
            states.append(
                {
                    "repo_id": repo_id,
                    "repo_url": repo_url,
                    "path": str(repo_path),
                    "current_sha": sha,
                    "reused_existing_clone": reused,
                }
            )
        except subprocess.CalledProcessError as e:
            LOGGER.warning("Skipping dead repository %s. Git error code: %s", repo_url, e.returncode)
            continue
        # ------------------------------------

    updated_index = _update_repo_index(
        workdir=workdir,
        index_path=resolved_index_path,
        manifest_path=resolved_manifest,
        repos_root=resolved_repos_root,
        states=states,
    )
    return updated_index, states


def checkout_from_manifest(
    workdir: Path,
    commit: str = "", # Optional, we will pull it from the manifest record
    manifest_path: Path = DEFAULT_MANIFEST_REL,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    index_path: Path = REPO_INDEX_REL,
) -> tuple[Path, list[dict[str, Any]]]:
    resolved_manifest = _resolve_path(workdir, manifest_path)
    resolved_repos_root = _resolve_path(workdir, repos_root)
    resolved_index_path = _resolve_path(workdir, index_path)

    records = _load_manifest_records(resolved_manifest)
    
    # We map repo_id to its specific base_commit from the manifest
    target_commits = {}
    for record in records:
        repo_id = _repo_id_from_record(record)
        base_commit = record.get("base_commit", "")
        if base_commit:
            target_commits[repo_id] = base_commit

    targets = _repo_targets(records)

    states: list[dict[str, Any]] = []
    for repo_id, repo_url in targets:
        repo_path = resolved_repos_root / repo_id
        
        # Determine the target commit for this specific repository
        target_commit = target_commits.get(repo_id, commit)
        
        if not target_commit:
             LOGGER.warning("Skipping checkout for %s. No commit provided.", repo_url)
             continue

        if not repo_path.exists() or not (repo_path / ".git").exists():
            LOGGER.warning("Skipping checkout for %s. Repository clone not found.", repo_url)
            continue
            
        try:
            # We don't try to clone again here, just checkout the state
            _fetch_all_refs(repo_path=repo_path)
            _run_git(["checkout", "--detach", target_commit], cwd=repo_path)
            sha = _current_sha(repo_path=repo_path)
            states.append(
                {
                    "repo_id": repo_id,
                    "repo_url": repo_url,
                    "path": str(repo_path),
                    "current_sha": sha,
                    "checked_out": target_commit,
                    "reused_existing_clone": True,
                }
            )
        except subprocess.CalledProcessError as e:
            LOGGER.warning("Skipping broken checkout for %s at %s. Git error code: %s", repo_url, target_commit, e.returncode)
            continue

    updated_index = _update_repo_index(
        workdir=workdir,
        index_path=resolved_index_path,
        manifest_path=resolved_manifest,
        repos_root=resolved_repos_root,
        states=states,
    )
    return updated_index, states
