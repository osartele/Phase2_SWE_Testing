import argparse
import json
import logging
from pathlib import Path

from phase2.config import load_config
from phase2.context import RunContext
from phase2.change_case_mining import mine_change_cases
from phase2.case_runner import run_case_deterministic
from phase2.ablation_runner import run_ablation_variants
from phase2.benchmark import run_benchmark
from phase2.dataset import (
    build_manifest,
    fetch_dataset_urls,
    list_cached_urls,
    read_urls_file,
    resolve_dataset_urls,
)
from phase2.layout import ensure_phase2_layout
from phase2.logging_utils import configure_logging
from phase2.evosuite import run_evosuite_for_case
from phase2.healing import run_iterative_healing
from phase2.regen import run_regenerative_sync
from phase2.pipeline import (
    build_change_cases,
    evaluate_rq1,
    evaluate_rq3,
    init_layout,
    prepare_data,
    run_all,
    run_rq2_mapper,
    sync_healing,
    sync_regenerative,
)
from phase2.pinning import verify_base_commit_pins
from phase2.rq1_eval import evaluate_rq1_cases
from phase2.rq3_eval import evaluate_rq3_quality
from phase2.rq2_eval import evaluate_rq2_scale
from phase2.rq2_mapper import map_manifest_record_one
from phase2.repo import checkout_from_manifest, clone_from_manifest
from phase2.test_runner import run_repo_tests


LOGGER = logging.getLogger(__name__)


def _build_context(args: argparse.Namespace) -> RunContext:
    workdir = args.workdir.expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    ensure_phase2_layout(workdir)

    log_path = configure_logging(workdir=workdir, level=args.log_level)
    config_path, config = load_config(config_path=args.config, workdir=workdir)
    LOGGER.info("Logs writing to %s", log_path)
    return RunContext(
        workdir=workdir,
        config_path=config_path,
        config=config,
        log_level=args.log_level.upper(),
    )


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path.cwd(),
        help="Workspace root where phase2/ data and artifacts are created (default: current directory).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to YAML/JSON/TOML config. Relative paths resolve from --workdir. "
            "If omitted, default config discovery is used."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO).",
    )


def _add_dataset_url_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        dest="urls",
        action="append",
        default=[],
        help="Dataset JSON URL. Provide multiple times for multiple datasets.",
    )
    parser.add_argument(
        "--urls-file",
        type=Path,
        default=None,
        help="Text file containing one dataset JSON URL per line.",
    )


def _collect_dataset_urls(args: argparse.Namespace, ctx: RunContext) -> list[str]:
    urls = list(args.urls or [])
    if args.urls_file is not None:
        candidate = args.urls_file.expanduser()
        if not candidate.is_absolute():
            candidate = ctx.workdir / candidate
        urls.extend(read_urls_file(candidate.resolve()))
    return resolve_dataset_urls(config=ctx.config, cli_urls=urls)


def _add_repo_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("phase2/data/processed/manifest.jsonl"),
        help=(
            "Manifest JSONL path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/manifest.jsonl)."
        ),
    )
    parser.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    parser.add_argument(
        "--index-path",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )


def _add_pinning_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("phase2/data/processed/manifest.jsonl"),
        help=(
            "Manifest JSONL path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/manifest.jsonl)."
        ),
    )
    parser.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    parser.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )


def _add_test_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    parser.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )


def _add_mapping_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("phase2/data/processed/manifest.jsonl"),
        help=(
            "Manifest JSONL path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/manifest.jsonl)."
        ),
    )
    parser.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    parser.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )


def _add_case_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("phase2/data/processed/manifest.jsonl"),
        help=(
            "Manifest JSONL path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/manifest.jsonl)."
        ),
    )
    parser.add_argument(
        "--pins",
        type=Path,
        default=Path("phase2/data/processed/pins.jsonl"),
        help=(
            "Pins JSONL path (usable records). Relative paths resolve from --workdir "
            "(default: phase2/data/processed/pins.jsonl)."
        ),
    )
    parser.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    parser.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phase2",
        description="Phase 2 reproducible pipeline CLI for RQ1/RQ2/RQ3.",
    )
    _add_common_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-layout", help="Create the canonical Phase 2 directory layout.")
    subparsers.add_parser(
        "prepare-data",
        help="Run Step 1 data prep (dataset fetch + manifest build) from config URLs.",
    )
    subparsers.add_parser("run-rq2", help="Run Step 2 AST traceability mapper workflow.")
    subparsers.add_parser("build-cases", help="Run Step 3 change-case construction workflow.")
    subparsers.add_parser("sync-regen", help="Run Step 4 regenerative synchronization workflow.")
    subparsers.add_parser("sync-heal", help="Run Step 4 iterative healing synchronization workflow.")
    subparsers.add_parser("evaluate-rq1", help="Run Step 5 agent-vs-human evaluation.")
    subparsers.add_parser("evaluate-rq3", help="Run Step 6 test-quality evaluation.")
    subparsers.add_parser("run-all", help="Run the full pipeline in order (Steps 1-6).")
    run_parser = subparsers.add_parser("run", help="High-level benchmark runner commands.")
    run_subparsers = run_parser.add_subparsers(dest="run_command", required=True)

    run_benchmark_parser = run_subparsers.add_parser(
        "benchmark",
        help=(
            "Execute end-to-end benchmark for selected cases/approaches, then generate "
            "final tables/plots and artifacts/summary.md."
        ),
    )
    run_benchmark_parser.add_argument(
        "--cases",
        type=int,
        default=1,
        help="Number of cases to benchmark from cases/index (default: 1; <=0 means all).",
    )
    run_benchmark_parser.add_argument(
        "--approaches",
        default="healing,regen,human",
        help="Comma-separated approaches subset: healing,regen,human (default: healing,regen,human).",
    )
    run_benchmark_parser.add_argument(
        "--with-mapper",
        action="store_true",
        help="Enable RQ2 mapper scope limiter during agent approaches.",
    )
    run_benchmark_parser.add_argument(
        "--cases-root",
        type=Path,
        default=Path("cases"),
        help="Cases root directory (default: cases).",
    )
    run_benchmark_parser.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    run_benchmark_parser.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )
    run_benchmark_parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=Path("artifacts/benchmark"),
        help="Benchmark artifacts root (default: artifacts/benchmark).",
    )
    run_benchmark_parser.add_argument(
        "--summary-path",
        type=Path,
        default=Path("artifacts/summary.md"),
        help="Final report markdown output path (default: artifacts/summary.md).",
    )
    run_benchmark_parser.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        help="Maximum healing iterations (default: 5).",
    )
    run_benchmark_parser.add_argument(
        "--max-minutes",
        type=int,
        default=30,
        help="Shared wall-clock budget in minutes (default: 30).",
    )
    run_benchmark_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Timeout per command invocation in seconds (default: 1800).",
    )
    run_benchmark_parser.add_argument(
        "--seed",
        type=int,
        default=1337,
        help="Fixed EvoSuite seed used in benchmark runs (default: 1337).",
    )
    run_benchmark_parser.add_argument(
        "--budget-seconds",
        type=int,
        default=120,
        help="EvoSuite search budget in seconds for regen runs (default: 120).",
    )
    run_benchmark_parser.add_argument(
        "--evosuite-jar",
        type=Path,
        default=None,
        help="Path to EvoSuite jar. If omitted, config/env/download path is used.",
    )
    run_benchmark_parser.add_argument(
        "--download-evosuite",
        action="store_true",
        help="Download EvoSuite jar when no local configured jar is available.",
    )
    run_benchmark_parser.add_argument(
        "--evosuite-download-url",
        default=None,
        help="Download URL for EvoSuite jar (used with --download-evosuite).",
    )
    run_benchmark_parser.add_argument(
        "--force-download",
        action="store_true",
        help="Force re-download of EvoSuite jar even if cached.",
    )
    run_benchmark_parser.add_argument(
        "--java-bin",
        default="java",
        help="Java executable used to run EvoSuite (default: java).",
    )
    run_benchmark_parser.add_argument(
        "--reassert-command",
        default=None,
        help=(
            "Optional direct ReAssert tool command. If omitted, auto-detection uses "
            "sync.healing.reassert_command, REASSERT_COMMAND, or 'reassert' on PATH."
        ),
    )
    run_benchmark_parser.add_argument(
        "--disable-direct-reassert",
        action="store_true",
        help="Disable direct ReAssert tool execution in healing runs.",
    )

    dataset_parser = subparsers.add_parser("dataset", help="Dataset ingestion commands.")
    dataset_subparsers = dataset_parser.add_subparsers(dest="dataset_command", required=True)

    dataset_fetch = dataset_subparsers.add_parser(
        "fetch",
        help="Download one or more classes2test dataset JSON URLs with caching.",
    )
    _add_dataset_url_args(dataset_fetch)
    dataset_fetch.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        help="HTTP timeout in seconds for each dataset URL (default: 30).",
    )
    dataset_fetch.add_argument(
        "--force",
        action="store_true",
        help="Re-download URLs even when cached files already exist.",
    )

    dataset_manifest = dataset_subparsers.add_parser(
        "manifest",
        help=(
            "Validate cached dataset records and write normalized output to "
            "phase2/data/processed/manifest.jsonl."
        ),
    )
    _add_dataset_url_args(dataset_manifest)
    dataset_manifest.add_argument(
        "--output",
        type=Path,
        default=Path("phase2/data/processed/manifest.jsonl"),
        help=(
            "Output JSONL path. Relative paths are resolved from --workdir "
            "(default: phase2/data/processed/manifest.jsonl)."
        ),
    )

    repo_parser = subparsers.add_parser("repo", help="Repository clone and checkout commands.")
    repo_subparsers = repo_parser.add_subparsers(dest="repo_command", required=True)

    repo_clone = repo_subparsers.add_parser(
        "clone",
        help="Clone repositories from manifest into repos/<repo_id>/ (or reuse existing clones).",
    )
    _add_repo_common_args(repo_clone)

    repo_checkout = repo_subparsers.add_parser(
        "checkout",
        help="Fetch refs and checkout a provided commit for each repository in the manifest.",
    )
    _add_repo_common_args(repo_checkout)
    repo_checkout.add_argument(
        "--commit",
        required=False,
        default="",
        help="Commit-ish to checkout for every repository (e.g., SHA, tag, branch).",
    )

    pin_parser = subparsers.add_parser("pin", help="Base commit pinning commands.")
    pin_subparsers = pin_parser.add_subparsers(dest="pin_command", required=True)

    pin_verify = pin_subparsers.add_parser(
        "verify",
        help="Verify labeled test/focal methods and pin a usable base commit per manifest record.",
    )
    _add_pinning_common_args(pin_verify)
    pin_verify.add_argument(
        "--output",
        type=Path,
        default=Path("phase2/data/processed/pins.jsonl"),
        help=(
            "Pins JSONL output path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/pins.jsonl)."
        ),
    )
    pin_verify.add_argument(
        "--window",
        type=int,
        default=200,
        help="Nearby commit search window size (default: 200).",
    )

    test_parser = subparsers.add_parser("test", help="Java test execution commands.")
    test_subparsers = test_parser.add_subparsers(dest="test_command", required=True)

    test_run = test_subparsers.add_parser(
        "run",
        help=(
            "Detect Maven/Gradle, run tests for a repo, and emit structured JSON "
            "with pass/fail and failing tests."
        ),
    )
    _add_test_common_args(test_run)
    test_run.add_argument(
        "--repo-id",
        required=True,
        help="Repository ID to run tests for (folder name under repos/ or key in repo_index.json).",
    )
    test_run.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON output path. Relative paths resolve from --workdir.",
    )
    test_run.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Test execution timeout in seconds (default: 1800).",
    )
    test_run.add_argument(
        "--extra-arg",
        dest="extra_args",
        action="append",
        default=[],
        help="Extra argument to pass through to Maven/Gradle command. Repeat for multiple values.",
    )

    map_parser = subparsers.add_parser("map", help="RQ2 AST traceability mapping commands.")
    map_subparsers = map_parser.add_subparsers(dest="map_command", required=True)

    map_one = map_subparsers.add_parser(
        "one",
        help="Run RQ2 mapper for one manifest record and write artifacts/mapping/<record-id>.json.",
    )
    _add_mapping_common_args(map_one)
    map_one.add_argument(
        "--record-id",
        required=True,
        help="Manifest record ID (dataset_id) or numeric manifest index.",
    )
    map_one.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Number of top candidates to output (default: config rq2.top_k or 3).",
    )
    map_one.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Optional mapping output path. Relative paths resolve from --workdir. "
            "Default: artifacts/mapping/<record-id>.json."
        ),
    )

    map_eval = map_subparsers.add_parser(
        "eval",
        help=(
            "Run RQ2 mapping for all usable records and write artifacts/rq2_metrics.csv "
            "and artifacts/rq2_error_report.md."
        ),
    )
    _add_mapping_common_args(map_eval)
    map_eval.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Top-k candidate list size used during evaluation (default: config rq2.top_k or 3).",
    )
    map_eval.add_argument(
        "--pins",
        type=Path,
        default=Path("phase2/data/processed/pins.jsonl"),
        help=(
            "Pins JSONL path used to select usable records. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/pins.jsonl)."
        ),
    )
    map_eval.add_argument(
        "--all-records",
        action="store_true",
        help="Ignore pins usable filter and evaluate all manifest records.",
    )
    map_eval.add_argument(
        "--output-csv",
        type=Path,
        default=Path("artifacts/rq2_metrics.csv"),
        help="Output CSV path (default: artifacts/rq2_metrics.csv).",
    )
    map_eval.add_argument(
        "--output-report",
        type=Path,
        default=Path("artifacts/rq2_error_report.md"),
        help="Output markdown report path (default: artifacts/rq2_error_report.md).",
    )

    case_parser = subparsers.add_parser("case", help="Change-case mining commands.")
    case_subparsers = case_parser.add_subparsers(dest="case_command", required=True)

    case_mine = case_subparsers.add_parser(
        "mine",
        help="Mine and rank real focal-file change commits, then write selected cases.",
    )
    _add_case_common_args(case_mine)
    case_mine.add_argument(
        "--max-commits-per-record",
        type=int,
        default=50,
        help="Max focal-file change commits to inspect per record (default: 50).",
    )
    case_mine.add_argument(
        "--nearby-window",
        type=int,
        default=20,
        help="Nearby commit window for human test updates (default: 20).",
    )
    case_mine.add_argument(
        "--selected-per-record",
        type=int,
        default=1,
        help="Number of top-ranked cases to save per record (default: 1).",
    )
    case_mine.add_argument(
        "--no-test-run",
        action="store_true",
        help="Skip test execution when mining (ranking uses non-test signals only).",
    )
    case_mine.add_argument(
        "--test-timeout-seconds",
        type=int,
        default=1800,
        help="Timeout per test execution during mining (default: 1800).",
    )
    case_mine.add_argument(
        "--ranked-output",
        type=Path,
        default=Path("artifacts/change_cases/ranked_candidates.jsonl"),
        help="Ranked candidates JSONL output path (default: artifacts/change_cases/ranked_candidates.jsonl).",
    )

    case_run = case_subparsers.add_parser(
        "run",
        help=(
            "Deterministically run one case: checkout base and modified commits, execute tests, "
            "and store artifacts under artifacts/cases/<case_id>/."
        ),
    )
    case_run.add_argument(
        "--case",
        required=True,
        help="Case JSON path or case_id present in cases/index/cases.jsonl.",
    )
    case_run.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    case_run.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )
    case_run.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Timeout per build/test command execution in seconds (default: 1800).",
    )
    case_run.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("artifacts/cases"),
        help="Case run artifacts root (default: artifacts/cases).",
    )

    heal_parser = subparsers.add_parser("heal", help="Iterative healing commands.")
    heal_subparsers = heal_parser.add_subparsers(dest="heal_command", required=True)

    heal_run = heal_subparsers.add_parser(
        "run",
        help=(
            "Run iterative healing (Agent + ReAssert baseline) from a case's modified commit and "
            "write iteration artifacts under artifacts/healing/<case_id>/iter_N/."
        ),
    )
    heal_run.add_argument(
        "--case",
        required=True,
        help="Case JSON path or case_id present in cases/index/cases.jsonl.",
    )
    heal_run.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    heal_run.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )
    heal_run.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("artifacts/healing"),
        help="Healing artifacts root (default: artifacts/healing).",
    )
    heal_run.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        help="Maximum healing iterations (default: 5).",
    )
    heal_run.add_argument(
        "--max-minutes",
        type=int,
        default=30,
        help="Total wall-clock budget for healing loop in minutes (default: 30).",
    )
    heal_run.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Timeout per build/test command execution in seconds (default: 1800).",
    )
    heal_run.add_argument(
        "--reassert-command",
        default=None,
        help=(
            "Optional direct ReAssert tool command. If omitted, auto-detection uses "
            "sync.healing.reassert_command, REASSERT_COMMAND, or 'reassert' on PATH."
        ),
    )
    heal_run.add_argument(
        "--disable-direct-reassert",
        action="store_true",
        help="Disable direct ReAssert tool execution and use only the ReAssert-style rule baseline.",
    )
    heal_run.add_argument(
        "--use-mapper-scope",
        action="store_true",
        help="Enable RQ2 mapper scope limiter (restrict healing focus to mapped test scope).",
    )

    regen_parser = subparsers.add_parser("regen", help="Regenerative synchronization commands.")
    regen_subparsers = regen_parser.add_subparsers(dest="regen_command", required=True)

    regen_run = regen_subparsers.add_parser(
        "run",
        help=(
            "Run regenerative synchronization (Agent + EvoSuite baseline) for a case's modified commit "
            "and write outputs under artifacts/regen/<case_id>/."
        ),
    )
    regen_run.add_argument(
        "--case",
        required=True,
        help="Case JSON path or case_id present in cases/index/cases.jsonl.",
    )
    regen_run.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    regen_run.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )
    regen_run.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("artifacts/regen"),
        help="Regeneration artifacts root (default: artifacts/regen).",
    )
    regen_run.add_argument(
        "--evosuite-jar",
        type=Path,
        default=None,
        help="Path to evosuite jar. If omitted, config/env/download path is used.",
    )
    regen_run.add_argument(
        "--download-evosuite",
        action="store_true",
        help="Download EvoSuite jar when no local configured jar is available.",
    )
    regen_run.add_argument(
        "--evosuite-download-url",
        default=None,
        help="Download URL for EvoSuite jar (used with --download-evosuite).",
    )
    regen_run.add_argument(
        "--force-download",
        action="store_true",
        help="Force re-download of EvoSuite jar even if cached.",
    )
    regen_run.add_argument(
        "--java-bin",
        default="java",
        help="Java executable used to run EvoSuite (default: java).",
    )
    regen_run.add_argument(
        "--seed",
        type=int,
        default=1337,
        help="Deterministic EvoSuite seed (default: 1337).",
    )
    regen_run.add_argument(
        "--budget-seconds",
        type=int,
        default=120,
        help="EvoSuite search budget in seconds (default: 120).",
    )
    regen_run.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Timeout per compile/test command in seconds (default: 1800).",
    )
    regen_run.add_argument(
        "--max-minutes",
        type=int,
        default=30,
        help="Total wall-clock budget for regenerative run in minutes (default: 30).",
    )
    regen_run.add_argument(
        "--use-mapper-scope",
        action="store_true",
        help="Enable RQ2 mapper scope limiter (prefer generated tests tied to mapped focal method).",
    )

    evosuite_parser = subparsers.add_parser("evosuite", help="EvoSuite tooling commands.")
    evosuite_subparsers = evosuite_parser.add_subparsers(dest="evosuite_command", required=True)

    evosuite_run = evosuite_subparsers.add_parser(
        "run",
        help=(
            "Run EvoSuite for a case's modified commit with robust classpath/tooling checks "
            "and write outputs under artifacts/regen/<case_id>/evosuite/."
        ),
    )
    evosuite_run.add_argument(
        "--case-id",
        required=True,
        help="Case ID present in cases/index/cases.jsonl.",
    )
    evosuite_run.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    evosuite_run.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )
    evosuite_run.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("artifacts/regen"),
        help="Artifacts root for EvoSuite outputs (default: artifacts/regen).",
    )
    evosuite_run.add_argument(
        "--evosuite-jar",
        type=Path,
        default=None,
        help="Path to EvoSuite jar. If omitted, config/env/download path is used.",
    )
    evosuite_run.add_argument(
        "--download",
        action="store_true",
        help="Download EvoSuite jar if no configured local jar exists.",
    )
    evosuite_run.add_argument(
        "--download-url",
        default=None,
        help="Download URL for EvoSuite jar.",
    )
    evosuite_run.add_argument(
        "--force-download",
        action="store_true",
        help="Force EvoSuite jar re-download even if cached.",
    )
    evosuite_run.add_argument(
        "--java-bin",
        default="java",
        help="Java executable used to run EvoSuite (default: java).",
    )
    evosuite_run.add_argument(
        "--time-budget",
        type=int,
        default=120,
        help="EvoSuite search budget in seconds (default: 120).",
    )
    evosuite_run.add_argument(
        "--seed",
        type=int,
        default=1337,
        help="Deterministic EvoSuite seed (default: 1337).",
    )
    evosuite_run.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Timeout per compile/classpath/generation command in seconds (default: 1800).",
    )

    ablation_parser = subparsers.add_parser(
        "ablation",
        help="Fairness-controlled ablation runner for mapper scope on/off variants.",
    )
    ablation_subparsers = ablation_parser.add_subparsers(dest="ablation_command", required=True)

    ablation_run = ablation_subparsers.add_parser(
        "run",
        help=(
            "Run healing and regen with and without mapper scope limiter using equal budgets, "
            "fixed seeds, and consistent stopping criteria labels."
        ),
    )
    ablation_run.add_argument(
        "--case",
        default=None,
        help="Case JSON path or case_id present in cases/index/cases.jsonl.",
    )
    ablation_run.add_argument(
        "--all-cases",
        action="store_true",
        help="Run ablations for every case in cases/index/cases.jsonl.",
    )
    ablation_run.add_argument(
        "--cases-root",
        type=Path,
        default=Path("cases"),
        help="Cases root directory (default: cases).",
    )
    ablation_run.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    ablation_run.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )
    ablation_run.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("artifacts/ablations"),
        help="Ablation artifacts root (default: artifacts/ablations).",
    )
    ablation_run.add_argument(
        "--output-csv",
        type=Path,
        default=Path("artifacts/ablations/ablation_summary.csv"),
        help="Ablation summary CSV output path (default: artifacts/ablations/ablation_summary.csv).",
    )
    ablation_run.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        help="Shared iteration budget used for controlled runs (default: 5).",
    )
    ablation_run.add_argument(
        "--max-minutes",
        type=int,
        default=30,
        help="Shared wall-clock budget in minutes (default: 30).",
    )
    ablation_run.add_argument(
        "--shared-budget-seconds",
        type=int,
        default=None,
        help="Shared seconds budget override for both approaches; default derives from --max-minutes.",
    )
    ablation_run.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Timeout per command invocation in seconds (default: 1800).",
    )
    ablation_run.add_argument(
        "--seed",
        type=int,
        default=1337,
        help="Fixed seed for controlled runs (default: 1337).",
    )
    ablation_run.add_argument(
        "--evosuite-jar",
        type=Path,
        default=None,
        help="Path to EvoSuite jar. If omitted, config/env/download path is used.",
    )
    ablation_run.add_argument(
        "--download-evosuite",
        action="store_true",
        help="Download EvoSuite jar when no local configured jar is available.",
    )
    ablation_run.add_argument(
        "--evosuite-download-url",
        default=None,
        help="Download URL for EvoSuite jar (used with --download-evosuite).",
    )
    ablation_run.add_argument(
        "--force-download",
        action="store_true",
        help="Force re-download of EvoSuite jar even if cached.",
    )
    ablation_run.add_argument(
        "--java-bin",
        default="java",
        help="Java executable used to run EvoSuite (default: java).",
    )
    ablation_run.add_argument(
        "--reassert-command",
        default=None,
        help=(
            "Optional direct ReAssert tool command. If omitted, auto-detection uses "
            "sync.healing.reassert_command, REASSERT_COMMAND, or 'reassert' on PATH."
        ),
    )
    ablation_run.add_argument(
        "--disable-direct-reassert",
        action="store_true",
        help="Disable direct ReAssert tool execution in healing variants.",
    )

    rq1_parser = subparsers.add_parser("rq1", help="RQ1 evaluation commands.")
    rq1_subparsers = rq1_parser.add_subparsers(dest="rq1_command", required=True)

    rq1_eval = rq1_subparsers.add_parser(
        "eval",
        help=(
            "Evaluate agent vs human updates for cases with human-updated tests and write "
            "artifacts/rq1_summary.csv plus per-case JSON summaries."
        ),
    )
    rq1_eval.add_argument(
        "--cases-root",
        type=Path,
        default=Path("cases"),
        help="Cases root directory (default: cases).",
    )
    rq1_eval.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    rq1_eval.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )
    rq1_eval.add_argument(
        "--artifacts-cases-root",
        type=Path,
        default=Path("artifacts/cases"),
        help="Case runner artifacts root (default: artifacts/cases).",
    )
    rq1_eval.add_argument(
        "--artifacts-healing-root",
        type=Path,
        default=Path("artifacts/healing"),
        help="Healing artifacts root (default: artifacts/healing).",
    )
    rq1_eval.add_argument(
        "--artifacts-regen-root",
        type=Path,
        default=Path("artifacts/regen"),
        help="Regenerative artifacts root (default: artifacts/regen).",
    )
    rq1_eval.add_argument(
        "--output-csv",
        type=Path,
        default=Path("artifacts/rq1_summary.csv"),
        help="RQ1 summary CSV output path (default: artifacts/rq1_summary.csv).",
    )
    rq1_eval.add_argument(
        "--output-cases-root",
        type=Path,
        default=Path("artifacts/rq1_cases"),
        help="Per-case JSON summaries output directory (default: artifacts/rq1_cases).",
    )
    rq1_eval.add_argument(
        "--with-ast-similarity",
        action="store_true",
        help="Compute optional AST-similarity heuristic in addition to line-diff similarity.",
    )

    rq3_parser = subparsers.add_parser("rq3", help="RQ3 test-quality evaluation commands.")
    rq3_subparsers = rq3_parser.add_subparsers(dest="rq3_command", required=True)

    rq3_eval = rq3_subparsers.add_parser(
        "eval",
        help=(
            "Evaluate JaCoCo coverage and PIT mutation score for human/healing/regen suites and "
            "write artifacts/rq3_quality.csv."
        ),
    )
    rq3_eval.add_argument(
        "--cases-root",
        type=Path,
        default=Path("cases"),
        help="Cases root directory (default: cases).",
    )
    rq3_eval.add_argument(
        "--repos-root",
        type=Path,
        default=Path("repos"),
        help="Root directory for cloned repos. Relative paths resolve from --workdir (default: repos).",
    )
    rq3_eval.add_argument(
        "--repo-index",
        type=Path,
        default=Path("phase2/data/processed/repo_index.json"),
        help=(
            "Repo state index JSON path. Relative paths resolve from --workdir "
            "(default: phase2/data/processed/repo_index.json)."
        ),
    )
    rq3_eval.add_argument(
        "--artifacts-healing-root",
        type=Path,
        default=Path("artifacts/healing"),
        help="Healing artifacts root (default: artifacts/healing).",
    )
    rq3_eval.add_argument(
        "--artifacts-regen-root",
        type=Path,
        default=Path("artifacts/regen"),
        help="Regenerative artifacts root (default: artifacts/regen).",
    )
    rq3_eval.add_argument(
        "--output-csv",
        type=Path,
        default=Path("artifacts/rq3_quality.csv"),
        help="RQ3 quality CSV output path (default: artifacts/rq3_quality.csv).",
    )
    rq3_eval.add_argument(
        "--output-cases-root",
        type=Path,
        default=Path("artifacts/rq3_cases"),
        help="Per-case JSON summaries output directory (default: artifacts/rq3_cases).",
    )
    rq3_eval.add_argument(
        "--eval-root",
        type=Path,
        default=Path("phase2/evaluation/rq3_test_quality"),
        help="RQ3 evaluation artifacts root (default: phase2/evaluation/rq3_test_quality).",
    )
    rq3_eval.add_argument(
        "--timeout-seconds",
        type=int,
        default=3600,
        help="Timeout per JaCoCo/PIT command in seconds (default: 3600).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    ctx = _build_context(args)

    if args.command == "init-layout":
        receipt = init_layout(ctx)
        print(f"[phase2] init-layout complete: {receipt}")
        return 0
    if args.command == "prepare-data":
        receipt = prepare_data(ctx)
        print(f"[phase2] prepare-data complete: {receipt}")
        return 0
    if args.command == "run-rq2":
        receipt = run_rq2_mapper(ctx)
        print(f"[phase2] run-rq2 complete: {receipt}")
        return 0
    if args.command == "build-cases":
        receipt = build_change_cases(ctx)
        print(f"[phase2] build-cases complete: {receipt}")
        return 0
    if args.command == "sync-regen":
        receipt = sync_regenerative(ctx)
        print(f"[phase2] sync-regen complete: {receipt}")
        return 0
    if args.command == "sync-heal":
        receipt = sync_healing(ctx)
        print(f"[phase2] sync-heal complete: {receipt}")
        return 0
    if args.command == "evaluate-rq1":
        receipt = evaluate_rq1(ctx)
        print(f"[phase2] evaluate-rq1 complete: {receipt}")
        return 0
    if args.command == "evaluate-rq3":
        receipt = evaluate_rq3(ctx)
        print(f"[phase2] evaluate-rq3 complete: {receipt}")
        return 0
    if args.command == "run-all":
        receipts = run_all(ctx)
        print("[phase2] run-all complete:")
        for receipt in receipts:
            print(f"  - {receipt}")
        return 0
    if args.command == "run":
        if args.run_command == "benchmark":
            try:
                result = run_benchmark(
                    workdir=ctx.workdir,
                    n_cases=args.cases,
                    approaches_csv=args.approaches,
                    with_mapper=args.with_mapper,
                    cases_root=args.cases_root,
                    repos_root=args.repos_root,
                    repo_index_path=args.repo_index,
                    benchmark_root=args.benchmark_root,
                    summary_path=args.summary_path,
                    max_iterations=args.max_iterations,
                    max_minutes=args.max_minutes,
                    timeout_seconds=args.timeout_seconds,
                    seed=args.seed,
                    budget_seconds=args.budget_seconds,
                    config=ctx.config,
                    evosuite_jar=args.evosuite_jar,
                    download_evosuite=args.download_evosuite,
                    evosuite_download_url=args.evosuite_download_url,
                    force_download=args.force_download,
                    java_bin=args.java_bin,
                    reassert_command=args.reassert_command,
                    enable_direct_reassert=not args.disable_direct_reassert,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "command": "run benchmark",
                            "error": str(exc),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 1
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    if args.command == "dataset":
        urls = _collect_dataset_urls(args, ctx)
        if not urls and args.dataset_command == "manifest":
            urls = list_cached_urls(ctx.workdir)
        if not urls:
            parser.error(
                "No dataset URLs resolved. Pass --url/--urls-file or define dataset.url(s) in config."
            )
            return 2

        if args.dataset_command == "fetch":
            results = fetch_dataset_urls(
                workdir=ctx.workdir,
                urls=urls,
                timeout_seconds=args.timeout_seconds,
                force=args.force,
            )
            used_cache = sum(1 for item in results if item["used_cache"])
            print(
                "[phase2] dataset fetch complete: "
                f"{len(results)} URL(s), cache hits={used_cache}, "
                f"cache_dir={ctx.workdir / 'phase2' / 'data' / 'raw' / 'classes2test'}"
            )
            return 0

        if args.dataset_command == "manifest":
            manifest_path, records = build_manifest(
                workdir=ctx.workdir,
                urls=urls,
                output_path=args.output,
            )
            print(
                "[phase2] dataset manifest complete: "
                f"{len(records)} record(s), output={manifest_path}"
            )
            return 0
    if args.command == "repo":
        if args.repo_command == "clone":
            index_path, states = clone_from_manifest(
                workdir=ctx.workdir,
                manifest_path=args.manifest,
                repos_root=args.repos_root,
                index_path=args.index_path,
            )
            reused = sum(1 for state in states if state["reused_existing_clone"])
            print(
                "[phase2] repo clone complete: "
                f"{len(states)} repo(s), reused={reused}, index={index_path}"
            )
            return 0

        if args.repo_command == "checkout":
            index_path, states = checkout_from_manifest(
                workdir=ctx.workdir,
                commit=args.commit,
                manifest_path=args.manifest,
                repos_root=args.repos_root,
                index_path=args.index_path,
            )
            print(
                "[phase2] repo checkout complete: "
                f"{len(states)} repo(s), commit={args.commit}, index={index_path}"
            )
            return 0
    if args.command == "pin":
        if args.pin_command == "verify":
            output_path, results = verify_base_commit_pins(
                workdir=ctx.workdir,
                manifest_path=args.manifest,
                repos_root=args.repos_root,
                repo_index_path=args.repo_index,
                output_path=args.output,
                search_window=args.window,
            )
            usable = sum(1 for row in results if row["usable"])
            unusable = len(results) - usable
            print(
                "[phase2] pin verify complete: "
                f"{len(results)} record(s), usable={usable}, unusable={unusable}, output={output_path}"
            )
            return 0
    if args.command == "test":
        if args.test_command == "run":
            result = run_repo_tests(
                workdir=ctx.workdir,
                repo_id=args.repo_id,
                repos_root=args.repos_root,
                repo_index_path=args.repo_index,
                output_path=args.output,
                extra_args=args.extra_args,
                timeout_seconds=args.timeout_seconds,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    if args.command == "map":
        if args.map_command == "one":
            top_k = args.top_k
            if top_k is None:
                top_k = int(ctx.config.get("rq2", {}).get("top_k", 3))
            result = map_manifest_record_one(
                workdir=ctx.workdir,
                record_id=args.record_id,
                top_k=top_k,
                manifest_path=args.manifest,
                repos_root=args.repos_root,
                repo_index_path=args.repo_index,
                output_path=args.output,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.map_command == "eval":
            top_k = args.top_k
            if top_k is None:
                top_k = int(ctx.config.get("rq2", {}).get("top_k", 3))
            result = evaluate_rq2_scale(
                workdir=ctx.workdir,
                top_k=top_k,
                manifest_path=args.manifest,
                repos_root=args.repos_root,
                repo_index_path=args.repo_index,
                pins_path=args.pins,
                use_all_records=args.all_records,
                output_csv=args.output_csv,
                output_report=args.output_report,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    if args.command == "case":
        if args.case_command == "mine":
            result = mine_change_cases(
                workdir=ctx.workdir,
                manifest_path=args.manifest,
                pins_path=args.pins,
                repos_root=args.repos_root,
                repo_index_path=args.repo_index,
                ranked_output_path=args.ranked_output,
                max_commits_per_record=args.max_commits_per_record,
                nearby_window=args.nearby_window,
                selected_per_record=args.selected_per_record,
                run_tests=not args.no_test_run,
                test_timeout_seconds=args.test_timeout_seconds,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.case_command == "run":
            result = run_case_deterministic(
                workdir=ctx.workdir,
                case_input=args.case,
                repos_root=args.repos_root,
                repo_index_path=args.repo_index,
                artifacts_root=args.artifacts_root,
                timeout_seconds=args.timeout_seconds,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    if args.command == "heal":
        if args.heal_command == "run":
            try:
                result = run_iterative_healing(
                    workdir=ctx.workdir,
                    case_input=args.case,
                    repos_root=args.repos_root,
                    repo_index_path=args.repo_index,
                    artifacts_root=args.artifacts_root,
                    max_iterations=args.max_iterations,
                    max_minutes=args.max_minutes,
                    timeout_seconds=args.timeout_seconds,
                    config=ctx.config,
                    reassert_command=args.reassert_command,
                    enable_direct_reassert=not args.disable_direct_reassert,
                    use_mapper_scope=args.use_mapper_scope,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "command": "heal run",
                            "error": str(exc),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 1
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    if args.command == "regen":
        if args.regen_command == "run":
            try:
                result = run_regenerative_sync(
                    workdir=ctx.workdir,
                    case_input=args.case,
                    config=ctx.config,
                    repos_root=args.repos_root,
                    repo_index_path=args.repo_index,
                    artifacts_root=args.artifacts_root,
                    evosuite_jar=args.evosuite_jar,
                    download_evosuite=args.download_evosuite,
                    evosuite_download_url=args.evosuite_download_url,
                    force_download=args.force_download,
                    java_bin=args.java_bin,
                    seed=args.seed,
                    budget_seconds=args.budget_seconds,
                    timeout_seconds=args.timeout_seconds,
                    use_mapper_scope=args.use_mapper_scope,
                    max_minutes=args.max_minutes,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "command": "regen run",
                            "error": str(exc),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 1
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    if args.command == "evosuite":
        if args.evosuite_command == "run":
            try:
                result = run_evosuite_for_case(
                    workdir=ctx.workdir,
                    case_id=args.case_id,
                    config=ctx.config,
                    repos_root=args.repos_root,
                    repo_index_path=args.repo_index,
                    artifacts_root=args.artifacts_root,
                    evosuite_jar=args.evosuite_jar,
                    download=args.download,
                    download_url=args.download_url,
                    force_download=args.force_download,
                    java_bin=args.java_bin,
                    seed=args.seed,
                    time_budget_seconds=args.time_budget,
                    timeout_seconds=args.timeout_seconds,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "command": "evosuite run",
                            "error": str(exc),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 1
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    if args.command == "ablation":
        if args.ablation_command == "run":
            try:
                result = run_ablation_variants(
                    workdir=ctx.workdir,
                    case_input=args.case,
                    run_all_cases=args.all_cases,
                    cases_root=args.cases_root,
                    repos_root=args.repos_root,
                    repo_index_path=args.repo_index,
                    artifacts_root=args.artifacts_root,
                    output_csv=args.output_csv,
                    max_iterations=args.max_iterations,
                    max_minutes=args.max_minutes,
                    timeout_seconds=args.timeout_seconds,
                    shared_budget_seconds=args.shared_budget_seconds,
                    seed=args.seed,
                    config=ctx.config,
                    evosuite_jar=args.evosuite_jar,
                    download_evosuite=args.download_evosuite,
                    evosuite_download_url=args.evosuite_download_url,
                    force_download=args.force_download,
                    java_bin=args.java_bin,
                    reassert_command=args.reassert_command,
                    enable_direct_reassert=not args.disable_direct_reassert,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "command": "ablation run",
                            "error": str(exc),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 1
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    if args.command == "rq1":
        if args.rq1_command == "eval":
            try:
                result = evaluate_rq1_cases(
                    workdir=ctx.workdir,
                    cases_root=args.cases_root,
                    repos_root=args.repos_root,
                    repo_index_path=args.repo_index,
                    artifacts_cases_root=args.artifacts_cases_root,
                    artifacts_healing_root=args.artifacts_healing_root,
                    artifacts_regen_root=args.artifacts_regen_root,
                    output_csv=args.output_csv,
                    output_cases_root=args.output_cases_root,
                    with_ast_similarity=args.with_ast_similarity,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "command": "rq1 eval",
                            "error": str(exc),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 1
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    if args.command == "rq3":
        if args.rq3_command == "eval":
            try:
                result = evaluate_rq3_quality(
                    workdir=ctx.workdir,
                    cases_root=args.cases_root,
                    repos_root=args.repos_root,
                    repo_index_path=args.repo_index,
                    artifacts_healing_root=args.artifacts_healing_root,
                    artifacts_regen_root=args.artifacts_regen_root,
                    output_csv=args.output_csv,
                    output_cases_root=args.output_cases_root,
                    eval_root=args.eval_root,
                    timeout_seconds=args.timeout_seconds,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "command": "rq3 eval",
                            "error": str(exc),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 1
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
