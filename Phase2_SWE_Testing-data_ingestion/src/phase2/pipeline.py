import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phase2.context import RunContext
from phase2.cases import ensure_case_schema_file
from phase2.dataset import build_manifest, fetch_dataset_urls, resolve_dataset_urls
from phase2.layout import ensure_phase2_layout
from phase2.rq1_eval import evaluate_rq1_cases
from phase2.rq3_eval import evaluate_rq3_quality


LOGGER = logging.getLogger(__name__)


def _run_receipt_dir(workdir: Path) -> Path:
    target = workdir / "phase2" / "artifacts" / "logs" / "runs"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_step_receipt(ctx: RunContext, step: str, payload: dict[str, Any] | None = None) -> Path:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{timestamp}_{step}.json"
    output_path = _run_receipt_dir(ctx.workdir) / filename
    body = {
        "step": step,
        "timestamp_utc": timestamp,
        "workdir": str(ctx.workdir),
        "config_path": str(ctx.config_path) if ctx.config_path else None,
        "payload": payload or {},
    }
    output_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return output_path


def init_layout(ctx: RunContext) -> Path:
    created = ensure_phase2_layout(ctx.workdir)
    receipt = _write_step_receipt(ctx, "init_layout", {"created_count": len(created)})
    LOGGER.info("Layout ready. Created %s directories.", len(created))
    return receipt


def prepare_data(ctx: RunContext) -> Path:
    ensure_phase2_layout(ctx.workdir)

    dataset_cfg = ctx.config.get("dataset", {})
    timeout_seconds = int(dataset_cfg.get("timeout_seconds", 30))
    urls = resolve_dataset_urls(config=ctx.config)
    payload: dict[str, Any] = {
        "fetched": 0,
        "used_cache": 0,
        "manifest_records": 0,
    }

    if not urls:
        LOGGER.info("No dataset URLs configured. Skipping data preparation.")
        return _write_step_receipt(ctx, "prepare_data", payload)

    fetch_results = fetch_dataset_urls(
        workdir=ctx.workdir,
        urls=urls,
        timeout_seconds=timeout_seconds,
    )
    manifest_path, records = build_manifest(workdir=ctx.workdir, urls=urls)
    payload = {
        "fetched": len(fetch_results),
        "used_cache": sum(1 for r in fetch_results if r["used_cache"]),
        "manifest_path": str(manifest_path),
        "manifest_records": len(records),
        "urls": urls,
    }
    return _write_step_receipt(ctx, "prepare_data", payload)


def run_rq2_mapper(ctx: RunContext) -> Path:
    ensure_phase2_layout(ctx.workdir)
    payload = {
        "top_k": ctx.config.get("rq2", {}).get("top_k", 3),
        "status": "placeholder",
    }
    LOGGER.info("RQ2 mapper placeholder step executed.")
    return _write_step_receipt(ctx, "run_rq2_mapper", payload)


def build_change_cases(ctx: RunContext) -> Path:
    ensure_phase2_layout(ctx.workdir)
    schema_path = ensure_case_schema_file(ctx.workdir)
    payload = {"status": "placeholder", "case_schema_path": str(schema_path)}
    LOGGER.info("Change-case builder placeholder step executed.")
    return _write_step_receipt(ctx, "build_change_cases", payload)


def sync_regenerative(ctx: RunContext) -> Path:
    ensure_phase2_layout(ctx.workdir)
    payload = {"status": "placeholder", "approach": "regenerative_evosuite"}
    LOGGER.info("Regenerative sync placeholder step executed.")
    return _write_step_receipt(ctx, "sync_regenerative", payload)


def sync_healing(ctx: RunContext) -> Path:
    ensure_phase2_layout(ctx.workdir)
    payload = {"status": "placeholder", "approach": "iterative_healing_reassert"}
    LOGGER.info("Iterative healing placeholder step executed.")
    return _write_step_receipt(ctx, "sync_healing", payload)


def evaluate_rq1(ctx: RunContext) -> Path:
    ensure_phase2_layout(ctx.workdir)
    rq1_cfg = ctx.config.get("rq1", {}) if isinstance(ctx.config.get("rq1"), dict) else {}
    with_ast_similarity = bool(rq1_cfg.get("with_ast_similarity", False))
    result = evaluate_rq1_cases(
        workdir=ctx.workdir,
        with_ast_similarity=with_ast_similarity,
    )
    payload = {
        "status": result.get("status"),
        "output_csv": result.get("output_csv"),
        "per_case_dir": result.get("per_case_dir"),
        "rows_written": result.get("rows_written"),
        "approach_pass_rates": result.get("approach_pass_rates"),
        "with_ast_similarity": with_ast_similarity,
    }
    LOGGER.info("RQ1 evaluation completed: %s", payload)
    return _write_step_receipt(ctx, "evaluate_rq1", payload)


def evaluate_rq3(ctx: RunContext) -> Path:
    ensure_phase2_layout(ctx.workdir)
    rq3_cfg = ctx.config.get("rq3", {}) if isinstance(ctx.config.get("rq3"), dict) else {}
    timeout_seconds = int(rq3_cfg.get("timeout_seconds", 3600))
    result = evaluate_rq3_quality(
        workdir=ctx.workdir,
        timeout_seconds=timeout_seconds,
    )
    payload = {
        "status": result.get("status"),
        "output_csv": result.get("output_csv"),
        "per_case_dir": result.get("per_case_dir"),
        "rows_written": result.get("rows_written"),
        "approach_counts": result.get("approach_counts"),
        "timeout_seconds": timeout_seconds,
    }
    LOGGER.info("RQ3 evaluation completed: %s", payload)
    return _write_step_receipt(ctx, "evaluate_rq3", payload)


def run_all(ctx: RunContext) -> list[Path]:
    receipts = [
        init_layout(ctx),
        prepare_data(ctx),
        run_rq2_mapper(ctx),
        build_change_cases(ctx),
        sync_regenerative(ctx),
        sync_healing(ctx),
        evaluate_rq1(ctx),
        evaluate_rq3(ctx),
    ]
    LOGGER.info("Full pipeline placeholder run completed.")
    return receipts
