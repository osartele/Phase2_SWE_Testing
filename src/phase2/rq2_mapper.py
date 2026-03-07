from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from phase2.java_parser import parse_java_file
from phase2.repo import REPO_INDEX_REL, resolve_repo_id_from_manifest_record, resolve_repo_path


DEFAULT_MANIFEST_REL = Path("phase2/data/processed/manifest.jsonl")
DEFAULT_REPOS_ROOT_REL = Path("repos")


@dataclass(slots=True)
class CandidateSignals:
    direct_call: int
    qualifier_match: int
    static_import_match: int
    arg_count_match: int
    name_similarity: float
    frequency: int
    frequency_norm: float
    qualifier_hits: int


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


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
    return records


def _find_manifest_record(records: list[dict[str, Any]], record_id: str) -> dict[str, Any]:
    exact = [record for record in records if str(record.get("dataset_id", "")) == record_id]
    if exact:
        return exact[0]

    if record_id.isdigit():
        idx = int(record_id)
        if 0 <= idx < len(records):
            return records[idx]

    raise ValueError(f"Record not found for record_id='{record_id}'.")


def _candidate_focal_methods(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    return [method for method in parsed["methods"] if method.get("kind") == "method_declaration"]


def _select_labeled_test_method(parsed_test: dict[str, Any], labeled_name: str) -> dict[str, Any]:
    matches = [method for method in parsed_test["methods"] if method.get("name") == labeled_name]
    if not matches:
        raise ValueError(f"Labeled test method not found in parsed test file: {labeled_name}")
    matches.sort(key=lambda method: (not method.get("is_test", False), method.get("line", 10**9)))
    return matches[0]


def _static_imports(test_source: str) -> list[str]:
    return re.findall(r"^\s*import\s+static\s+([^;]+);\s*$", test_source, flags=re.MULTILINE)


def _focal_aliases(test_method_source: str, focal_class_name: str) -> set[str]:
    aliases = {focal_class_name}

    typed_pattern = re.compile(
        rf"\b{re.escape(focal_class_name)}(?:\s*<[^>]+>)?\s+([A-Za-z_]\w*)\b"
    )
    aliases.update(match.group(1) for match in typed_pattern.finditer(test_method_source))

    new_pattern = re.compile(rf"\b([A-Za-z_]\w*)\s*=\s*new\s+{re.escape(focal_class_name)}\s*\(")
    aliases.update(match.group(1) for match in new_pattern.finditer(test_method_source))

    return {alias for alias in aliases if alias}


def _normalize_test_method_name(name: str) -> str:
    lowered = name.strip().lower()
    if lowered.startswith("test") and len(lowered) > 4:
        lowered = lowered[4:]
    return lowered.replace("_", "")


def _name_similarity(test_method_name: str, candidate_name: str) -> float:
    test_norm = _normalize_test_method_name(test_method_name)
    candidate_norm = candidate_name.strip().lower().replace("_", "")
    if not test_norm or not candidate_norm:
        return 0.0
    return SequenceMatcher(a=test_norm, b=candidate_norm).ratio()


def _static_import_signal(static_imports: list[str], focal_class_name: str, method_name: str) -> int:
    for item in static_imports:
        cleaned = item.strip()
        if cleaned.endswith(f".{focal_class_name}.{method_name}") or cleaned.endswith(
            f".{focal_class_name}.*"
        ):
            return 1
    return 0


def _compute_candidate_signals(
    test_method: dict[str, Any],
    focal_method: dict[str, Any],
    focal_class_name: str,
    aliases: set[str],
    static_imports: list[str],
    max_frequency: int,
) -> CandidateSignals:
    method_name = str(focal_method.get("name", ""))
    invocations = [
        invocation for invocation in test_method.get("invocations", []) if invocation.get("callee_name") == method_name
    ]
    frequency = len(invocations)
    qualifier_hits = sum(
        1 for invocation in invocations if (invocation.get("qualifier") in aliases or invocation.get("qualifier") == focal_class_name)
    )
    param_count = len(focal_method.get("params", []))
    arg_count_match = int(any(invocation.get("arg_count") == param_count for invocation in invocations))
    direct_call = int(frequency > 0)
    qualifier_match = int(qualifier_hits > 0)
    static_import_match = _static_import_signal(static_imports, focal_class_name, method_name)
    similarity = _name_similarity(str(test_method.get("name", "")), method_name)
    frequency_norm = (frequency / max_frequency) if max_frequency > 0 else 0.0

    return CandidateSignals(
        direct_call=direct_call,
        qualifier_match=qualifier_match,
        static_import_match=static_import_match,
        arg_count_match=arg_count_match,
        name_similarity=similarity,
        frequency=frequency,
        frequency_norm=frequency_norm,
        qualifier_hits=qualifier_hits,
    )


def _score_candidate(signals: CandidateSignals) -> float:
    return (
        signals.direct_call * 5.0
        + signals.qualifier_match * 2.0
        + signals.static_import_match * 1.5
        + signals.arg_count_match * 1.25
        + signals.name_similarity * 2.0
        + signals.frequency_norm * 1.0
    )


def _candidate_explanation(signals: CandidateSignals) -> str:
    return (
        f"direct_call={signals.direct_call}; "
        f"qualifier_match={signals.qualifier_match} (hits={signals.qualifier_hits}); "
        f"static_import_match={signals.static_import_match}; "
        f"arg_count_match={signals.arg_count_match}; "
        f"name_similarity={signals.name_similarity:.3f}; "
        f"frequency={signals.frequency}; "
        f"frequency_norm={signals.frequency_norm:.3f}"
    )


def _default_output_path(workdir: Path, record_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", record_id)
    return workdir / "artifacts" / "mapping" / f"{safe_id}.json"


def map_manifest_record_one(
    workdir: Path,
    record_id: str,
    top_k: int = 3,
    manifest_path: Path = DEFAULT_MANIFEST_REL,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = REPO_INDEX_REL,
    output_path: Path | None = None,
) -> dict[str, Any]:
    resolved_manifest = _resolve_path(workdir, manifest_path)
    records = _load_manifest_records(resolved_manifest)
    record = _find_manifest_record(records, record_id)

    dataset_id = str(record.get("dataset_id", record_id))
    repo_id = resolve_repo_id_from_manifest_record(record)
    repo_path = resolve_repo_path(
        workdir=workdir,
        repo_id=repo_id,
        repos_root=repos_root,
        index_path=repo_index_path,
    )
    if not (repo_path / ".git").exists():
        raise FileNotFoundError(f"Repository clone not found for repo_id={repo_id}: {repo_path}")

    test_file_rel = str(record.get("test_file_path", ""))
    focal_file_rel = str(record.get("focal_file_path", ""))
    labeled_test_method = str(record.get("labeled_test_method", ""))
    labeled_focal_method = str(record.get("labeled_focal_method", ""))
    focal_class_name = str(record.get("focal_class_identifier") or Path(focal_file_rel).stem)

    test_file_path = repo_path / Path(test_file_rel)
    focal_file_path = repo_path / Path(focal_file_rel)
    if not test_file_path.exists():
        raise FileNotFoundError(f"Test file not found: {test_file_path}")
    if not focal_file_path.exists():
        raise FileNotFoundError(f"Focal file not found: {focal_file_path}")

    parsed_test = parse_java_file(test_file_path)
    parsed_focal = parse_java_file(focal_file_path)
    test_method = _select_labeled_test_method(parsed_test, labeled_test_method)
    focal_methods = _candidate_focal_methods(parsed_focal)
    if not focal_methods:
        raise ValueError(f"No focal methods found in focal file: {focal_file_path}")

    test_source = test_file_path.read_text(encoding="utf-8", errors="replace")
    static_imports = _static_imports(test_source)
    aliases = _focal_aliases(str(test_method.get("source_text", "")), focal_class_name)

    method_invocation_frequencies: dict[str, int] = {}
    for invocation in test_method.get("invocations", []):
        callee = str(invocation.get("callee_name") or "")
        if callee:
            method_invocation_frequencies[callee] = method_invocation_frequencies.get(callee, 0) + 1
    max_frequency = max(method_invocation_frequencies.values(), default=0)

    scored: list[dict[str, Any]] = []
    for method in focal_methods:
        signals = _compute_candidate_signals(
            test_method=test_method,
            focal_method=method,
            focal_class_name=focal_class_name,
            aliases=aliases,
            static_imports=static_imports,
            max_frequency=max_frequency,
        )
        score = _score_candidate(signals)
        scored.append(
            {
                "name": method.get("name"),
                "signature": method.get("signature"),
                "param_count": len(method.get("params", [])),
                "line": method.get("line"),
                "score": round(score, 6),
                "signals": {
                    "direct_call": signals.direct_call,
                    "qualifier_match": signals.qualifier_match,
                    "qualifier_hits": signals.qualifier_hits,
                    "static_import_match": signals.static_import_match,
                    "arg_count_match": signals.arg_count_match,
                    "name_similarity": round(signals.name_similarity, 6),
                    "frequency": signals.frequency,
                    "frequency_norm": round(signals.frequency_norm, 6),
                },
                "explanation": _candidate_explanation(signals),
            }
        )

    scored.sort(
        key=lambda row: (
            -row["score"],
            -row["signals"]["direct_call"],
            -row["signals"]["frequency"],
            -row["signals"]["name_similarity"],
            str(row["name"] or ""),
        )
    )

    ranked_candidates: list[dict[str, Any]] = []
    for rank, row in enumerate(scored[: max(top_k, 1)], start=1):
        ranked = dict(row)
        ranked["rank"] = rank
        ranked_candidates.append(ranked)

    ground_truth_rank = None
    for idx, row in enumerate(scored, start=1):
        if str(row.get("name", "")) == labeled_focal_method:
            ground_truth_rank = idx
            break

    result = {
        "record_id": record_id,
        "dataset_id": dataset_id,
        "status": "ok",
        "repo_id": repo_id,
        "repository_url": record.get("repository_url"),
        "repo_path": str(repo_path),
        "test_file_path": str(test_file_path),
        "focal_file_path": str(focal_file_path),
        "labeled_test_method": labeled_test_method,
        "labeled_focal_method": labeled_focal_method,
        "focal_class_name": focal_class_name,
        "top_k": max(top_k, 1),
        "located_test_method": {
            "name": test_method.get("name"),
            "line": test_method.get("line"),
            "is_test": test_method.get("is_test"),
            "annotation_names": [annotation.get("name") for annotation in test_method.get("annotations", [])],
            "invocation_count": len(test_method.get("invocations", [])),
        },
        "candidate_count": len(scored),
        "candidates": ranked_candidates,
        "ground_truth": {
            "labeled_focal_method": labeled_focal_method,
            "in_top_k": ground_truth_rank is not None and ground_truth_rank <= max(top_k, 1),
            "rank": ground_truth_rank,
        },
        "metadata": {
            "manifest_path": str(resolved_manifest),
            "static_imports": static_imports,
            "focal_aliases": sorted(aliases),
            "generated_at_utc": _utc_now_iso(),
        },
    }

    final_output_path = _resolve_path(workdir, output_path) if output_path else _default_output_path(workdir, dataset_id)
    final_output_path.parent.mkdir(parents=True, exist_ok=True)
    final_output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    result["output_path"] = str(final_output_path)
    return result
