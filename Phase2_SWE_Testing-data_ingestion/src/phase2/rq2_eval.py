from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phase2.java_parser import parse_java_file
from phase2.rq2_mapper import DEFAULT_MANIFEST_REL, map_manifest_record_one
from phase2.repo import REPO_INDEX_REL


DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_PINS_REL = Path("phase2/data/processed/pins.jsonl")
DEFAULT_METRICS_CSV_REL = Path("artifacts/rq2_metrics.csv")
DEFAULT_ERROR_REPORT_REL = Path("artifacts/rq2_error_report.md")


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_path(base: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line:
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _load_manifest_index(manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records = _load_jsonl(manifest_path)
    by_id: dict[str, dict[str, Any]] = {}
    for idx, record in enumerate(records):
        dataset_id = str(record.get("dataset_id") or f"index_{idx}")
        by_id[dataset_id] = record
    return records, by_id


def _signature_core(signature: str | None) -> str | None:
    if not signature:
        return None
    s = signature.strip()
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)
    s = s.split("{", 1)[0].strip()
    s = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", s)
    m = re.search(r"([A-Za-z_]\w*)\s*\((.*)\)", s)
    if not m:
        return re.sub(r"\s+", "", s).lower()
    name = m.group(1).strip().lower()
    params = re.sub(r"\s+", "", m.group(2)).lower()
    return f"{name}({params})"


def _candidate_matches_signature(candidate_signature: str | None, labeled_signature: str | None) -> bool:
    left = _signature_core(candidate_signature)
    right = _signature_core(labeled_signature)
    if left is None or right is None:
        return False
    return left == right


def _candidate_top_k(candidates: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    return candidates[: max(1, k)]


def _mock_signal(test_source: str) -> bool:
    markers = (
        "@Mock",
        "@InjectMocks",
        "Mockito.",
        "mock(",
        "when(",
        "verify(",
        "given(",
        "spy(",
    )
    lowered = test_source.lower()
    return any(marker.lower() in lowered for marker in markers)


def _failure_mode(
    mapping_result: dict[str, Any],
    manifest_record: dict[str, Any],
    parse_cache: dict[str, dict[str, Any]],
) -> str:
    candidates = mapping_result.get("candidates", [])
    labeled_name = str(manifest_record.get("labeled_focal_method") or "")
    labeled_sig = manifest_record.get("focal_method_signature")
    top1 = candidates[0] if candidates else None

    test_file_path = str(mapping_result.get("test_file_path") or "")
    focal_file_path = str(mapping_result.get("focal_file_path") or "")

    test_source = ""
    if test_file_path:
        try:
            test_source = Path(test_file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            test_source = ""

    if _mock_signal(test_source):
        return "mocks"

    any_direct = any(int(candidate.get("signals", {}).get("direct_call", 0)) == 1 for candidate in candidates)
    if not any_direct:
        return "indirect_calls"

    focal_parsed = parse_cache.get(focal_file_path)
    if focal_parsed is None and focal_file_path:
        try:
            focal_parsed = parse_java_file(focal_file_path)
            parse_cache[focal_file_path] = focal_parsed
        except Exception:
            focal_parsed = None

    overload_count = 0
    if focal_parsed is not None:
        overload_count = sum(
            1
            for method in focal_parsed.get("methods", [])
            if method.get("kind") == "method_declaration" and method.get("name") == labeled_name
        )

    if top1 is not None:
        top1_name = str(top1.get("name") or "")
        top1_sig = top1.get("signature")
        if top1_name == labeled_name and labeled_sig and not _candidate_matches_signature(top1_sig, labeled_sig):
            return "overloads"
        if overload_count > 1 and any(str(c.get("name") or "") == labeled_name for c in candidates):
            return "overloads"

        if focal_parsed is not None and top1_name and top1_name != labeled_name:
            wrapper_words = ("wrap", "delegate", "proxy", "forward", "invoke", "call")
            top_method = next(
                (
                    method
                    for method in focal_parsed.get("methods", [])
                    if method.get("kind") == "method_declaration" and method.get("name") == top1_name
                ),
                None,
            )
            invokes_labeled = False
            if top_method is not None:
                invokes_labeled = any(
                    str(inv.get("callee_name") or "") == labeled_name
                    for inv in top_method.get("invocations", [])
                )
            if invokes_labeled or any(word in top1_name.lower() for word in wrapper_words):
                return "wrappers"

    return "other"


def _select_record_ids(
    manifest_records: list[dict[str, Any]],
    pins_path: Path,
    use_all_records: bool,
) -> list[str]:
    if use_all_records:
        return [str(record.get("dataset_id") or f"index_{idx}") for idx, record in enumerate(manifest_records)]

    pins = _load_jsonl(pins_path)
    usable_ids = [
        str(row.get("dataset_id"))
        for row in pins
        if bool(row.get("usable")) and isinstance(row.get("dataset_id"), (str, int))
    ]
    if usable_ids:
        return sorted(set(usable_ids))
    return [str(record.get("dataset_id") or f"index_{idx}") for idx, record in enumerate(manifest_records)]


def _write_metrics_csv(output_path: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "kind",
        "record_id",
        "repo_id",
        "status",
        "usable",
        "labeled_focal_method",
        "labeled_focal_signature",
        "top1_pred_name",
        "top1_pred_signature",
        "top1_score",
        "top1_identifier_correct",
        "top3_identifier_correct",
        "top1_signature_correct",
        "top3_signature_correct",
        "top1_primary_correct",
        "top3_primary_correct",
        "ground_truth_rank_identifier",
        "failure_mode",
        "mapping_output_path",
        "n_records",
        "n_signature_records",
        "top1_identifier_accuracy",
        "top3_identifier_accuracy",
        "top1_signature_accuracy",
        "top3_signature_accuracy",
        "top1_primary_accuracy",
        "top3_primary_accuracy",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        writer.writerow({"kind": "summary", **summary})
    return output_path


def _write_error_report(
    output_path: Path,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    misses = [row for row in rows if not bool(row.get("top1_primary_correct"))]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in misses:
        grouped[str(row.get("failure_mode") or "other")].append(row)

    ordered_modes = ["overloads", "wrappers", "indirect_calls", "mocks", "other", "processing_error"]
    modes = ordered_modes + [mode for mode in grouped.keys() if mode not in ordered_modes]

    lines: list[str] = []
    lines.append("# RQ2 Error Report")
    lines.append("")
    lines.append(f"Generated at: `{_utc_now_iso()}`")
    lines.append("")
    lines.append("## Metrics Summary")
    lines.append("")
    lines.append(f"- Records evaluated: `{summary['n_records']}`")
    lines.append(f"- Signature records: `{summary['n_signature_records']}`")
    lines.append(f"- Top-1 accuracy (identifier): `{summary['top1_identifier_accuracy']:.4f}`")
    lines.append(f"- Top-3 accuracy (identifier): `{summary['top3_identifier_accuracy']:.4f}`")
    lines.append(f"- Top-1 accuracy (signature): `{summary['top1_signature_accuracy']:.4f}`")
    lines.append(f"- Top-3 accuracy (signature): `{summary['top3_signature_accuracy']:.4f}`")
    lines.append(f"- Top-1 accuracy (primary): `{summary['top1_primary_accuracy']:.4f}`")
    lines.append(f"- Top-3 accuracy (primary): `{summary['top3_primary_accuracy']:.4f}`")
    lines.append("")
    lines.append("## Failure Modes (Top-1 Primary Misses)")
    lines.append("")

    if not misses:
        lines.append("No Top-1 primary misses were found.")
        lines.append("")
    else:
        for mode in modes:
            bucket = grouped.get(mode, [])
            if not bucket:
                continue
            lines.append(f"### {mode} ({len(bucket)})")
            lines.append("")
            for row in bucket[:20]:
                lines.append(
                    "- "
                    f"`{row.get('record_id')}` repo `{row.get('repo_id')}` "
                    f"labeled `{row.get('labeled_focal_method')}` "
                    f"top1 `{row.get('top1_pred_name')}` "
                    f"(rank={row.get('ground_truth_rank_identifier')})"
                )
            if len(bucket) > 20:
                lines.append(f"- ... {len(bucket) - 20} more")
            lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def evaluate_rq2_scale(
    workdir: Path,
    top_k: int = 3,
    manifest_path: Path = DEFAULT_MANIFEST_REL,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = REPO_INDEX_REL,
    pins_path: Path = DEFAULT_PINS_REL,
    use_all_records: bool = False,
    output_csv: Path = DEFAULT_METRICS_CSV_REL,
    output_report: Path = DEFAULT_ERROR_REPORT_REL,
) -> dict[str, Any]:
    resolved_manifest = _resolve_path(workdir, manifest_path)
    resolved_pins = _resolve_path(workdir, pins_path)
    resolved_csv = _resolve_path(workdir, output_csv)
    resolved_report = _resolve_path(workdir, output_report)

    manifest_records, manifest_by_id = _load_manifest_index(resolved_manifest)
    record_ids = _select_record_ids(manifest_records, resolved_pins, use_all_records=use_all_records)

    rows: list[dict[str, Any]] = []
    focal_parse_cache: dict[str, dict[str, Any]] = {}

    n_records = 0
    n_signature = 0
    top1_id_hits = 0
    top3_id_hits = 0
    top1_sig_hits = 0
    top3_sig_hits = 0
    top1_primary_hits = 0
    top3_primary_hits = 0

    eval_top_k = max(3, int(top_k))

    for record_id in record_ids:
        manifest_record = manifest_by_id.get(record_id)
        if manifest_record is None:
            continue
        n_records += 1

        labeled_name = str(manifest_record.get("labeled_focal_method") or "")
        labeled_sig = manifest_record.get("focal_method_signature")

        base_row: dict[str, Any] = {
            "kind": "record",
            "record_id": record_id,
            "repo_id": str(manifest_record.get("repository_repo_id") or ""),
            "status": "ok",
            "usable": True,
            "labeled_focal_method": labeled_name,
            "labeled_focal_signature": labeled_sig or "",
            "top1_pred_name": "",
            "top1_pred_signature": "",
            "top1_score": "",
            "top1_identifier_correct": False,
            "top3_identifier_correct": False,
            "top1_signature_correct": "",
            "top3_signature_correct": "",
            "top1_primary_correct": False,
            "top3_primary_correct": False,
            "ground_truth_rank_identifier": "",
            "failure_mode": "",
            "mapping_output_path": "",
        }

        try:
            mapping = map_manifest_record_one(
                workdir=workdir,
                record_id=record_id,
                top_k=eval_top_k,
                manifest_path=manifest_path,
                repos_root=repos_root,
                repo_index_path=repo_index_path,
                output_path=None,
            )
            candidates = mapping.get("candidates", [])
            top1 = candidates[0] if candidates else None
            top3 = _candidate_top_k(candidates, 3)

            top1_id = bool(top1 and str(top1.get("name") or "") == labeled_name)
            top3_id = any(str(candidate.get("name") or "") == labeled_name for candidate in top3)

            has_signature = bool(labeled_sig)
            if has_signature:
                n_signature += 1
                top1_sig = bool(
                    top1
                    and str(top1.get("name") or "") == labeled_name
                    and _candidate_matches_signature(top1.get("signature"), labeled_sig)
                )
                top3_sig = any(
                    str(candidate.get("name") or "") == labeled_name
                    and _candidate_matches_signature(candidate.get("signature"), labeled_sig)
                    for candidate in top3
                )
                top1_primary = top1_sig
                top3_primary = top3_sig
            else:
                top1_sig = None
                top3_sig = None
                top1_primary = top1_id
                top3_primary = top3_id

            if top1_id:
                top1_id_hits += 1
            if top3_id:
                top3_id_hits += 1
            if has_signature and top1_sig:
                top1_sig_hits += 1
            if has_signature and top3_sig:
                top3_sig_hits += 1
            if top1_primary:
                top1_primary_hits += 1
            if top3_primary:
                top3_primary_hits += 1

            if not top1_primary:
                mode = _failure_mode(mapping, manifest_record, focal_parse_cache)
            else:
                mode = ""

            base_row.update(
                {
                    "repo_id": str(mapping.get("repo_id") or base_row["repo_id"]),
                    "top1_pred_name": str((top1 or {}).get("name") or ""),
                    "top1_pred_signature": str((top1 or {}).get("signature") or ""),
                    "top1_score": (top1 or {}).get("score", ""),
                    "top1_identifier_correct": top1_id,
                    "top3_identifier_correct": top3_id,
                    "top1_signature_correct": "" if top1_sig is None else top1_sig,
                    "top3_signature_correct": "" if top3_sig is None else top3_sig,
                    "top1_primary_correct": top1_primary,
                    "top3_primary_correct": top3_primary,
                    "ground_truth_rank_identifier": mapping.get("ground_truth", {}).get("rank", ""),
                    "failure_mode": mode,
                    "mapping_output_path": mapping.get("output_path", ""),
                }
            )
        except Exception as exc:
            base_row.update(
                {
                    "status": "error",
                    "top1_identifier_correct": False,
                    "top3_identifier_correct": False,
                    "top1_primary_correct": False,
                    "top3_primary_correct": False,
                    "failure_mode": "processing_error",
                    "mapping_output_path": "",
                    "top1_pred_name": "",
                    "top1_pred_signature": "",
                    "top1_score": "",
                }
            )
            if labeled_sig:
                n_signature += 1

        rows.append(base_row)

    def _safe_div(n: int, d: int) -> float:
        return float(n) / float(d) if d else 0.0

    summary = {
        "n_records": n_records,
        "n_signature_records": n_signature,
        "top1_identifier_accuracy": round(_safe_div(top1_id_hits, n_records), 6),
        "top3_identifier_accuracy": round(_safe_div(top3_id_hits, n_records), 6),
        "top1_signature_accuracy": round(_safe_div(top1_sig_hits, n_signature), 6),
        "top3_signature_accuracy": round(_safe_div(top3_sig_hits, n_signature), 6),
        "top1_primary_accuracy": round(_safe_div(top1_primary_hits, n_records), 6),
        "top3_primary_accuracy": round(_safe_div(top3_primary_hits, n_records), 6),
    }

    csv_path = _write_metrics_csv(resolved_csv, rows, summary)
    report_path = _write_error_report(resolved_report, rows, summary)
    return {
        "status": "ok",
        "n_records": n_records,
        "n_signature_records": n_signature,
        "metrics_csv": str(csv_path),
        "error_report": str(report_path),
        "summary": summary,
    }
