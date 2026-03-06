from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CASES_ROOT_REL = Path("cases")
DEFAULT_SCHEMA_REL = DEFAULT_CASES_ROOT_REL / "schema" / "case.schema.json"
DEFAULT_RECORDS_REL = DEFAULT_CASES_ROOT_REL / "records"
DEFAULT_INDEX_REL = DEFAULT_CASES_ROOT_REL / "index" / "cases.jsonl"


REQUIRED_STRING_FIELDS: tuple[str, ...] = (
    "repo_id",
    "base_commit",
    "modified_commit",
    "focal_file_path",
    "test_file_path",
    "mapped_focal_method",
    "mapped_test_method",
)


@dataclass(slots=True)
class Case:
    case_id: str
    repo_id: str
    base_commit: str
    modified_commit: str
    focal_file_path: str
    test_file_path: str
    mapped_focal_method: str
    mapped_test_method: str
    build_commands: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "repo_id": self.repo_id,
            "base_commit": self.base_commit,
            "modified_commit": self.modified_commit,
            "focal_file_path": self.focal_file_path,
            "test_file_path": self.test_file_path,
            "mapped_focal_method": self.mapped_focal_method,
            "mapped_test_method": self.mapped_test_method,
            "build_commands": list(self.build_commands),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Case":
        validated = validate_case_payload(payload)
        return cls(
            case_id=validated["case_id"],
            repo_id=validated["repo_id"],
            base_commit=validated["base_commit"],
            modified_commit=validated["modified_commit"],
            focal_file_path=validated["focal_file_path"],
            test_file_path=validated["test_file_path"],
            mapped_focal_method=validated["mapped_focal_method"],
            mapped_test_method=validated["mapped_test_method"],
            build_commands=validated["build_commands"],
            metadata=validated.get("metadata", {}),
        )


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _normalize_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Field '{field_name}' must be a string.")
    text = value.strip()
    if not text:
        raise ValueError(f"Field '{field_name}' cannot be empty.")
    return text


def _normalize_build_commands(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Field 'build_commands' must be an array of command strings.")
    commands: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"Field 'build_commands[{index}]' must be a string.")
        command = item.strip()
        if not command:
            raise ValueError(f"Field 'build_commands[{index}]' cannot be empty.")
        commands.append(command)
    if not commands:
        raise ValueError("Field 'build_commands' must contain at least one command.")
    return commands


def _deterministic_case_id(payload: dict[str, Any]) -> str:
    seed_fields = {
        "repo_id": payload["repo_id"],
        "base_commit": payload["base_commit"],
        "modified_commit": payload["modified_commit"],
        "focal_file_path": payload["focal_file_path"],
        "test_file_path": payload["test_file_path"],
        "mapped_focal_method": payload["mapped_focal_method"],
        "mapped_test_method": payload["mapped_test_method"],
        "build_commands": payload["build_commands"],
    }
    seed = json.dumps(seed_fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"case_{hashlib.sha256(seed).hexdigest()[:16]}"


def validate_case_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Case payload must be an object.")

    normalized: dict[str, Any] = {}
    for field_name in REQUIRED_STRING_FIELDS:
        if field_name not in payload:
            raise ValueError(f"Missing required field '{field_name}'.")
        normalized[field_name] = _normalize_string(payload[field_name], field_name)

    if "build_commands" not in payload:
        raise ValueError("Missing required field 'build_commands'.")
    normalized["build_commands"] = _normalize_build_commands(payload["build_commands"])

    case_id = payload.get("case_id")
    if case_id is None:
        normalized["case_id"] = _deterministic_case_id(normalized)
    else:
        normalized["case_id"] = _normalize_string(case_id, "case_id")

    metadata = payload.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("Field 'metadata' must be an object when provided.")
    normalized["metadata"] = metadata
    return normalized


def ensure_case_schema_file(workdir: Path, schema_rel: Path = DEFAULT_SCHEMA_REL) -> Path:
    schema_path = _resolve_path(workdir, schema_rel)
    if schema_path.exists():
        return schema_path
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://phase2.local/schemas/case.schema.json",
        "title": "Phase2ChangeCase",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "case_id",
            "repo_id",
            "base_commit",
            "modified_commit",
            "focal_file_path",
            "test_file_path",
            "mapped_focal_method",
            "mapped_test_method",
            "build_commands",
        ],
        "properties": {
            "case_id": {"type": "string", "minLength": 1},
            "repo_id": {"type": "string", "minLength": 1},
            "base_commit": {"type": "string", "minLength": 1},
            "modified_commit": {"type": "string", "minLength": 1},
            "focal_file_path": {"type": "string", "minLength": 1},
            "test_file_path": {"type": "string", "minLength": 1},
            "mapped_focal_method": {"type": "string", "minLength": 1},
            "mapped_test_method": {"type": "string", "minLength": 1},
            "build_commands": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "metadata": {"type": "object"},
        },
    }
    schema_path.write_text(json.dumps(schema, indent=2, sort_keys=True), encoding="utf-8")
    return schema_path


def _read_index(index_path: Path) -> dict[str, dict[str, Any]]:
    if not index_path.exists():
        return {}
    items: dict[str, dict[str, Any]] = {}
    for raw_line in index_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            continue
        case_id = str(payload.get("case_id", "")).strip()
        if case_id:
            items[case_id] = payload
    return items


def _write_index(index_path: Path, rows: dict[str, dict[str, Any]]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("w", encoding="utf-8") as handle:
        for case_id in sorted(rows.keys()):
            handle.write(json.dumps(rows[case_id], sort_keys=True))
            handle.write("\n")


def save_case(
    case: Case | dict[str, Any],
    workdir: Path,
    cases_root: Path = DEFAULT_CASES_ROOT_REL,
    index_rel: Path = DEFAULT_INDEX_REL,
) -> tuple[Case, Path]:
    payload = case.to_dict() if isinstance(case, Case) else dict(case)
    normalized = validate_case_payload(payload)
    case_obj = Case.from_dict(normalized)

    root = _resolve_path(workdir, cases_root)
    case_dir = root / case_obj.case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    case_path = case_dir / "case.json"
    case_path.write_text(json.dumps(case_obj.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    index_path = _resolve_path(workdir, index_rel)
    index_rows = _read_index(index_path)
    index_rows[case_obj.case_id] = {
        "case_id": case_obj.case_id,
        "repo_id": case_obj.repo_id,
        "path": str(case_path),
        "base_commit": case_obj.base_commit,
        "modified_commit": case_obj.modified_commit,
        "mapped_focal_method": case_obj.mapped_focal_method,
        "mapped_test_method": case_obj.mapped_test_method,
    }
    _write_index(index_path, index_rows)
    return case_obj, case_path


def load_case(path: Path | str) -> Case:
    case_path = Path(path).expanduser().resolve()
    payload = json.loads(case_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Case file does not contain an object: {case_path}")
    return Case.from_dict(payload)


def load_case_by_id(
    case_id: str,
    workdir: Path,
    index_rel: Path = DEFAULT_INDEX_REL,
) -> Case:
    clean_id = _normalize_string(case_id, "case_id")
    index_path = _resolve_path(workdir, index_rel)
    rows = _read_index(index_path)
    row = rows.get(clean_id)
    if row is None:
        raise FileNotFoundError(f"Case not found in index: {clean_id}")
    case_path = row.get("path")
    if not isinstance(case_path, str) or not case_path.strip():
        raise ValueError(f"Case index entry missing path for case_id={clean_id}")
    return load_case(case_path)


def list_cases(workdir: Path, index_rel: Path = DEFAULT_INDEX_REL) -> list[Case]:
    index_path = _resolve_path(workdir, index_rel)
    rows = _read_index(index_path)
    cases: list[Case] = []
    for case_id in sorted(rows.keys()):
        path = rows[case_id].get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        try:
            cases.append(load_case(path))
        except FileNotFoundError:
            continue
    return cases
