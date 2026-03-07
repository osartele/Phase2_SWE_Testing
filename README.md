# Phase 2 Pipeline Repository (RQ1/RQ2/RQ3)

This repository is organized for the Phase 2 experiment pipeline:

- RQ2: AST-based test-to-focal-method traceability mapping
- RQ1: Agent-synchronized tests vs human-updated tests
- RQ3: Post-synchronization test quality (coverage + mutation)

## Current Workspace Snapshot

At the start of this setup, the workspace contained:

- `main.py` (standalone Python array demo script)
- `main2.py` (dataset fetch/preview script for classes2test JSON)
- `Phase 1 (1).docx` (Phase 1 document)
- `image.png`, `image4.png` (images)

A new `phase2/` structure has been created for the full pipeline.

## Prerequisites

- Python 3.11+
- JDK 11+ (JDK 17 recommended for modern builds)
- Maven 3.8+ and/or Gradle 7+
- Git 2.30+
- EvoSuite jar (for `evosuite run` / `regen run`), provided via `--evosuite-jar`, `EVOSUITE_JAR`, config, or download URL.

Python dependencies are pinned in `pyproject.toml`.

## Reproducible Python Setup

Install the project (editable mode):

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

Run CLI help:

```bash
phase2 --help
```

Run from any location with a target workspace root:

```bash
phase2 --workdir /path/to/workspace init-layout
phase2 --workdir /path/to/workspace dataset fetch --url https://anonymous.4open.science/r/classes2test/dataset/13899/13899_8.json
phase2 --workdir /path/to/workspace dataset manifest --url https://anonymous.4open.science/r/classes2test/dataset/13899/13899_8.json
phase2 --workdir /path/to/workspace repo clone
phase2 --workdir /path/to/workspace repo checkout --commit <commit>
phase2 --workdir /path/to/workspace pin verify --window 200
phase2 --workdir /path/to/workspace test run --repo-id <repo_id>
phase2 --workdir /path/to/workspace map one --record-id <dataset_id> --top-k 3
phase2 --workdir /path/to/workspace map eval --top-k 3
phase2 --workdir /path/to/workspace case mine
phase2 --workdir /path/to/workspace case run --case <case_id-or-path>
phase2 --workdir /path/to/workspace evosuite run --case-id <case_id> --time-budget 120 --seed 1337 --evosuite-jar /path/to/evosuite.jar
phase2 --workdir /path/to/workspace regen run --case <case_id-or-path> --evosuite-jar /path/to/evosuite.jar --seed 1337 --budget-seconds 120
phase2 --workdir /path/to/workspace heal run --case <case_id-or-path> --reassert-command "reassert"
phase2 --workdir /path/to/workspace ablation run --case <case_id-or-path> --seed 1337 --max-minutes 30 --max-iterations 5
phase2 --workdir /path/to/workspace rq1 eval --with-ast-similarity
phase2 --workdir /path/to/workspace rq3 eval --timeout-seconds 3600
phase2 --workdir /path/to/workspace run benchmark --cases 10 --approaches healing,regen,human --with-mapper
phase2 --workdir /path/to/workspace run-all
```

Notes:

- `dataset fetch` caches by URL under `phase2/data/raw/classes2test/`.
- `dataset manifest` validates schema and writes normalized records to `phase2/data/processed/manifest.jsonl`.
- `dataset manifest` can run without URLs after `dataset fetch` (it uses cached URL index).
- `repo clone` reads manifest and clones/reuses repos under `repos/<repo_id>/`.
- `repo checkout --commit <sha>` fetches refs and checks out that commit for each manifest repo.
- `pin verify` writes `phase2/data/processed/pins.jsonl` with `base_commit`, `usable`, and reason fields.
- `test run --repo-id <id>` detects Maven/Gradle (wrapper preferred), runs tests, and returns JSON.
  Result JSON includes: `status`, `success`, `exit_code`, `failing_tests`, `report_paths`, `console_log_path`.
- `map one --record-id <id>` runs RQ2 AST traceability mapping and writes `artifacts/mapping/<record-id>.json`.
- `map eval` runs RQ2 at scale and writes `artifacts/rq2_metrics.csv` and `artifacts/rq2_error_report.md`.
- `case mine` mines real focal-file change commits and writes ranked candidates plus selected cases.
- `case run` deterministically executes baseline/modified commits and writes artifacts under `artifacts/cases/<case_id>/`.
- `evosuite run` runs EvoSuite at the modified commit with Java/runtime/classpath checks and writes outputs under `artifacts/regen/<case_id>/evosuite/`.
- `regen run` executes deterministic EvoSuite-based regeneration, filters passing generated tests, and writes artifacts under `artifacts/regen/<case_id>/`.
- `regen run --use-mapper-scope` enables mapper scope limiting during candidate filtering (keeps generated tests tied to mapped focal method).
- `heal run` executes a `ReAssert-style baseline`: it uses a direct ReAssert tool if available/configured, otherwise falls back to built-in rule-based repairs, and writes iteration artifacts under `artifacts/healing/<case_id>/iter_N/`.
- `heal run --disable-direct-reassert` forces rule-only baseline mode.
- `heal run --use-mapper-scope` limits repair focus to mapped test scope.
- `ablation run` automatically executes fairness-controlled variants for both approaches (`mapper on/off`), enforces shared budgets + fixed seed controls, and writes distinct artifacts under `artifacts/ablations/`.
- `rq1 eval` evaluates agent-vs-human outcomes for cases with human-updated tests and writes `artifacts/rq1_summary.csv` plus per-case JSON summaries under `artifacts/rq1_cases/`.
- `rq3 eval` evaluates test quality for `human`, `healing`, and `regen` suites using standardized JaCoCo and PIT runs, and writes `artifacts/rq3_quality.csv` plus per-case summaries under `artifacts/rq3_cases/`.
- `run benchmark --cases N --approaches healing,regen,human --with-mapper` executes selected approaches end-to-end on N cases, generates final tables/plots, and writes `artifacts/summary.md`.

Config loading behavior:

- If `--config` is provided, that file is used (`.yaml`, `.yml`, `.json`, `.toml`).
- Relative `--config` paths are resolved against `--workdir`.
- If `--config` is omitted, CLI auto-discovers:
  - `phase2/configs/experiments/default.yaml`
  - `phase2/configs/experiments/default.yml`
  - `phase2/configs/experiments/default.toml`
  - `phase2/configs/experiments/default.json`

Default config created in this repo:

- `phase2/configs/experiments/default.yaml`

CLI subcommands:

- `init-layout`
- `dataset fetch`
- `dataset manifest`
- `repo clone`
- `repo checkout`
- `pin verify`
- `test run`
- `map one`
- `map eval`
- `case mine`
- `case run`
- `evosuite run`
- `ablation run`
- `regen run`
- `heal run`
- `rq1 eval`
- `rq3 eval`
- `run benchmark`
- `prepare-data`
- `run-rq2`
- `build-cases`
- `sync-regen`
- `sync-heal`
- `evaluate-rq1`
- `evaluate-rq3`
- `run-all`

## Repository Layout

```text
pyproject.toml                     # Python project metadata and pinned dependencies
src/phase2/                       # Installable package + CLI implementation
repos/                            # Cloned subject repos by repo_id
cases/                            # Change-case schema, selected case payloads, and index

phase2/
  configs/
    env/                          # Environment/toolchain config templates
    experiments/                  # Experiment parameter sets (budgets, top-k, seeds)

  data/
    raw/classes2test/            # Cached dataset JSON by URL hash + cache index
    processed/                   # Normalized outputs (manifest.jsonl, pins.jsonl, repo_index.json)
    classes2test_raw/             # Raw pulled classes2test records
    records_prepared/             # Normalized records with pinned base metadata
    repos_mirror/                 # Cloned subject repositories
    base_commits/                 # Buildable pinned base commits per record
    change_cases/                 # Modified focal-class scenarios (+ ground truth tests)

  rq2_ast_mapper/
    src/                          # AST parser + mapper implementation
    tests/                        # Unit/integration tests for mapper
    outputs/                      # Top-k mapping predictions, confidence, error labels

  sync/
    regenerative_evosuite/
      prompts/                    # Agent prompts/config for regenerative synchronization
      generated_tests/            # Raw EvoSuite generated tests
      merged_suite/               # Filtered/deduped merged suite outputs
      logs/                       # Build/test/sync logs

    iterative_healing_reassert/
      patches/                    # Assertion repair and test-fix patches
      logs/                       # Failure traces and healing-loop logs

  evaluation/
    rq1_agent_vs_human/
      metrics/                    # Pass rate, time, iterations, diff size
      diffs/                      # Agent vs human patch diffs
      similarity/                 # Similarity-to-human analysis outputs

    rq3_test_quality/
      jacoco/                     # Coverage reports
      pit/                        # PIT mutation reports
      summaries/                  # Aggregated quality comparison tables

  artifacts/
    patches/                      # Final patch artifacts for all runs
    logs/                         # Consolidated pipeline logs
    tables/                       # CSV/JSON report-ready tables
    plots/                        # Report-ready figures

  scripts/
    runners/                      # Pipeline entrypoints/orchestrators
    utils/                        # Shared utilities (repo ops, parsing, metrics)

  docs/                           # Protocol notes, assumptions, and method docs
```

`cases/` layout:

```text
cases/
  schema/
    case.schema.json             # Canonical Case JSON schema
  <case_id>/
    case.json                    # Selected case payload
  index/
    cases.jsonl                  # Lightweight index (one row per case)
```

Case run artifact layout:

```text
artifacts/cases/<case_id>/
  case_run_summary.json
  baseline/
    commit_sha.txt
    stdout.log
    stderr.log
    run_result.json
    junit/
  modified/
    commit_sha.txt
    stdout.log
    stderr.log
    run_result.json
    junit/
```

Healing artifact layout:

```text
artifacts/healing/<case_id>/
  healing_summary.json
  iter_1/
    commit_sha.txt
    stdout.log
    stderr.log
    reassert_stdout.log          # present when direct tool is attempted
    reassert_stderr.log          # present when direct tool is attempted
    reassert_result.json         # present when direct tool is attempted
    repairs.json
    patch.diff
    iteration_result.json
    junit/
  iter_2/
  ...
```

Regeneration artifact layout:

```text
artifacts/regen/<case_id>/
  regen_summary.json
  candidate_results.json
  kept_generated.json
  generated_tests/
  filtered_tests/
  merged_suite/
    merge_patch.diff
    test_root_snapshot/
  logs/
    prepare_build_stdout.log
    prepare_build_stderr.log
    evosuite_stdout.log
    evosuite_stderr.log
    filter/
```

EvoSuite-only artifact layout:

```text
artifacts/regen/<case_id>/evosuite/
  evosuite_summary.json
  generated_tests/
  logs/
    prepare_stdout.log
    prepare_stderr.log
    project_test_classpath.txt
    evosuite_stdout.log
    evosuite_stderr.log
```

## Intended End-to-End Workflow

1. Data preparation (Step 1)
   - Pull classes2test records into `phase2/data/raw/classes2test/` with caching.
   - Validate and normalize one flat record per URL into `phase2/data/processed/manifest.jsonl`.
   - Clone/reuse each repo in `repos/<repo_id>/` and checkout target commits.
   - Verify method/file presence and pin reproducible base commits in `phase2/data/processed/pins.jsonl`.
   - Run Java tests per repo with `phase2 test run --repo-id ...` and capture structured outcomes.

2. RQ2 traceability mapper (Step 2)
   - Parse focal class and JUnit test ASTs using `phase2/rq2_ast_mapper/src/`.
   - Emit top-k focal-method predictions + confidence into `phase2/rq2_ast_mapper/outputs/`.
   - Score against labeled `focal_method` (top-1/top-3 and error categories).

3. Change case construction (Step 3)
   - From each base commit, apply mined focal-class changes.
   - Save selected mined cases to `cases/<case_id>/case.json` and index entries in `cases/index/cases.jsonl`.

4. Synchronization approaches (Step 4)
   - Regenerative (Agent + EvoSuite): generate, merge, filter, dedup tests under `phase2/sync/regenerative_evosuite/`.
   - Iterative healing (Agent + ReAssert-style): run failures and patch broken tests under `phase2/sync/iterative_healing_reassert/`.

5. RQ1 evaluation (Step 5)
   - Compare final synced suites vs human-updated suites.
   - Store pass rate, time, iterations, diff size, and similarity outputs in `phase2/evaluation/rq1_agent_vs_human/`.

6. RQ3 evaluation (Step 6)
   - Compute JaCoCo coverage + PIT mutation for regen vs healing vs human.
   - Save outputs to `phase2/evaluation/rq3_test_quality/`.

7. Deliverables (Step 7)
   - Keep patches/logs/tables/plots in `phase2/artifacts/`.
   - Expose a one-command runner through CLI (for example: `phase2 --workdir /path/to/workspace run-all`).

## Next Build Targets

- Add a single orchestration entrypoint in `phase2/scripts/runners/`.
- Add schema definitions for prepared records and change cases.
- Add reproducibility config files in `phase2/configs/experiments/`.
