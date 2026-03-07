import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


LOGGER = logging.getLogger(__name__)

RAW_CACHE_REL = Path("phase2/data/raw/classes2test")
PROCESSED_REL = Path("phase2/data/processed")
MANIFEST_REL = PROCESSED_REL / "manifest.jsonl"
INDEX_REL = RAW_CACHE_REL / "cache_index.json"

REQUIRED_PATHS: tuple[tuple[str, ...], ...] = (
    ("repository", "url"),
    ("test_class", "file"),
    ("focal_class", "file"),
    ("test_case", "identifier"),
    ("focal_method", "identifier"),
    ("test_case", "body"),
    ("focal_method", "body"),
)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _url_basename(url: str) -> str:
    path = urlparse(url).path
    basename = Path(path).name or "dataset.json"
    if "." not in basename:
        basename += ".json"
    return basename


def _cache_name_for_url(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"{digest}_{_url_basename(url)}"


def _read_index(index_path: Path) -> dict[str, Any]:
    if not index_path.exists():
        return {}
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Cache index is invalid: {index_path}")
    return payload


def _write_index(index_path: Path, payload: dict[str, Any]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _cache_target(workdir: Path, url: str) -> Path:
    return workdir / RAW_CACHE_REL / _cache_name_for_url(url)


def _content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def resolve_dataset_urls(config: dict[str, Any], cli_urls: list[str] | None = None) -> list[str]:
    cleaned_cli = [u.strip() for u in (cli_urls or []) if u and u.strip()]
    if cleaned_cli:
        return sorted(set(cleaned_cli))

    dataset_cfg = config.get("dataset", {})
    urls_value = dataset_cfg.get("urls")
    if isinstance(urls_value, list):
        urls = [str(u).strip() for u in urls_value if str(u).strip()]
        if urls:
            return sorted(set(urls))

    single_url = dataset_cfg.get("url")
    if isinstance(single_url, str) and single_url.strip():
        return [single_url.strip()]
    return []


def read_urls_file(urls_file: Path) -> list[str]:
    urls: list[str] = []
    for raw_line in urls_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def list_cached_urls(workdir: Path) -> list[str]:
    index_path = workdir / INDEX_REL
    index = _read_index(index_path)
    urls = [url for url in index.keys() if isinstance(url, str) and url.strip()]
    return sorted(set(urls))


def fetch_dataset_urls(
    workdir: Path,
    urls: list[str],
    timeout_seconds: int = 30,
    force: bool = False,
) -> list[dict[str, Any]]:
    if not urls:
        raise ValueError("No dataset URLs provided.")

    raw_dir = workdir / RAW_CACHE_REL
    raw_dir.mkdir(parents=True, exist_ok=True)

    index_path = workdir / INDEX_REL
    index = _read_index(index_path)

    results: list[dict[str, Any]] = []
    for url in sorted(set(urls)):
        target = _cache_target(workdir, url)
        used_cache = target.exists() and not force
        if used_cache:
            content = target.read_bytes()
            content_sha = _content_sha256(content)
            LOGGER.info("Using cached dataset for %s", url)
        else:
            headers = {"User-Agent": "phase2-cli/0.1"}
            response = requests.get(url, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()
            content = response.content
            target.write_bytes(content)
            content_sha = _content_sha256(content)
            LOGGER.info("Downloaded dataset %s -> %s", url, target)

        # Parse and validate shape/schema at fetch-time so bad inputs fail early.
        parsed = json.loads(content.decode("utf-8"))
        record = _coerce_record(parsed, source=url)
        _normalize_payload(record, dataset_url=url, source=url)

        relative_target = str(target.relative_to(workdir))
        index[url] = {
            "cache_path": relative_target,
            "sha256": content_sha,
            "last_fetch_utc": _utc_now_iso(),
        }
        results.append(
            {
                "url": url,
                "cache_path": str(target),
                "cache_path_relative": relative_target,
                "used_cache": used_cache,
                "sha256": content_sha,
                "bytes": len(content),
            }
        )

    _write_index(index_path, index)
    return results


def _coerce_record(raw: Any, source: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        if len(raw) != 1:
            raise ValueError(f"{source}: expected one record, found {len(raw)}")
        item = raw[0]
        if not isinstance(item, dict):
            raise ValueError(f"{source}: list record must be an object")
        return item
    raise ValueError(f"{source}: expected JSON object or single-item list")


def _walk_get(mapping: dict[str, Any], path: tuple[str, ...], source: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict) or key not in current:
            dotted = ".".join(path)
            raise ValueError(f"{source}: missing required field '{dotted}'")
        current = current[key]
    return current


def _required_str(mapping: dict[str, Any], path: tuple[str, ...], source: str) -> str:
    value = _walk_get(mapping, path, source)
    if not isinstance(value, str):
        dotted = ".".join(path)
        raise ValueError(f"{source}: field '{dotted}' must be a string")
    return value


def _optional_str(mapping: dict[str, Any], path: tuple[str, ...]) -> str | None:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    if current is None:
        return None
    if isinstance(current, str):
        return current
    return str(current)


def _deterministic_record_id(normalized: dict[str, Any]) -> str:
    seed_fields = {
        "dataset_url": normalized["dataset_url"],
        "repository_url": normalized["repository_url"],
        "test_file_path": normalized["test_file_path"],
        "focal_file_path": normalized["focal_file_path"],
        "labeled_test_method": normalized["labeled_test_method"],
        "labeled_focal_method": normalized["labeled_focal_method"],
    }
    seed = json.dumps(seed_fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:20]
    return f"c2t_{digest}"


def _normalize_payload(payload: dict[str, Any], dataset_url: str, source: str) -> dict[str, Any]:
    for path in REQUIRED_PATHS:
        _required_str(payload, path, source=source)

    normalized = {
        "dataset_id": "",
        "dataset_url": dataset_url,
        "repository_url": _required_str(payload, ("repository", "url"), source),
        "repository_repo_id": _optional_str(payload, ("repository", "repo_id")),
        "test_file_path": _required_str(payload, ("test_class", "file"), source),
        "focal_file_path": _required_str(payload, ("focal_class", "file"), source),
        "labeled_test_method": _required_str(payload, ("test_case", "identifier"), source),
        "labeled_focal_method": _required_str(payload, ("focal_method", "identifier"), source),
        "test_method_signature": _optional_str(payload, ("test_case", "full_signature")),
        "focal_method_signature": _optional_str(payload, ("focal_method", "full_signature")),
        "test_class_identifier": _optional_str(payload, ("test_class", "identifier")),
        "focal_class_identifier": _optional_str(payload, ("focal_class", "identifier")),
        "test_method_body": _required_str(payload, ("test_case", "body"), source),
        "focal_method_body": _required_str(payload, ("focal_method", "body"), source),
    }
    normalized["dataset_id"] = _deterministic_record_id(normalized)
    return normalized


def load_cached_record_for_url(workdir: Path, url: str) -> tuple[Path, dict[str, Any]]:
    index_path = workdir / INDEX_REL
    index = _read_index(index_path)
    cache_entry = index.get(url)

    candidate_path: Path | None = None
    if isinstance(cache_entry, dict):
        cache_path = cache_entry.get("cache_path")
        if isinstance(cache_path, str) and cache_path.strip():
            candidate_path = (workdir / cache_path).resolve()

    if candidate_path is None:
        candidate_path = _cache_target(workdir, url).resolve()

    if not candidate_path.exists():
        raise FileNotFoundError(
            f"No cached dataset file for URL '{url}'. Run 'phase2 dataset fetch' first."
        )

    raw = json.loads(candidate_path.read_text(encoding="utf-8"))
    record = _coerce_record(raw, source=str(candidate_path))
    return candidate_path, record


def build_manifest(
    workdir: Path,
    urls: list[str],
    output_path: Path | None = None,
) -> tuple[Path, list[dict[str, Any]]]:
    if not urls:
        raise ValueError("No dataset URLs provided for manifest build.")

    manifest_target = output_path or (workdir / MANIFEST_REL)
    if not manifest_target.is_absolute():
        manifest_target = (workdir / manifest_target).resolve()
    manifest_target.parent.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    for url in sorted(set(urls)):
        cache_path, raw_record = load_cached_record_for_url(workdir, url)
        normalized = _normalize_payload(raw_record, dataset_url=url, source=str(cache_path))
        normalized["source_cache_path"] = str(cache_path)
        records.append(normalized)

    records.sort(key=lambda row: row["dataset_url"])
    with manifest_target.open("w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")

    return manifest_target, records
