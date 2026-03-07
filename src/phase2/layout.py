from pathlib import Path


PHASE2_DIRS: tuple[str, ...] = (
    "artifacts",
    "artifacts/cases",
    "artifacts/healing",
    "artifacts/regen",
    "artifacts/ablations",
    "artifacts/mapping",
    "artifacts/change_cases",
    "cases",
    "cases/schema",
    "cases/index",
    "repos",
    "phase2",
    "phase2/configs",
    "phase2/configs/env",
    "phase2/configs/experiments",
    "phase2/data",
    "phase2/data/raw",
    "phase2/data/raw/classes2test",
    "phase2/data/processed",
    "phase2/data/classes2test_raw",
    "phase2/data/records_prepared",
    "phase2/data/repos_mirror",
    "phase2/data/base_commits",
    "phase2/data/change_cases",
    "phase2/rq2_ast_mapper",
    "phase2/rq2_ast_mapper/src",
    "phase2/rq2_ast_mapper/tests",
    "phase2/rq2_ast_mapper/outputs",
    "phase2/sync",
    "phase2/sync/regenerative_evosuite",
    "phase2/sync/regenerative_evosuite/prompts",
    "phase2/sync/regenerative_evosuite/generated_tests",
    "phase2/sync/regenerative_evosuite/merged_suite",
    "phase2/sync/regenerative_evosuite/logs",
    "phase2/sync/iterative_healing_reassert",
    "phase2/sync/iterative_healing_reassert/patches",
    "phase2/sync/iterative_healing_reassert/logs",
    "phase2/evaluation",
    "phase2/evaluation/rq1_agent_vs_human",
    "phase2/evaluation/rq1_agent_vs_human/metrics",
    "phase2/evaluation/rq1_agent_vs_human/diffs",
    "phase2/evaluation/rq1_agent_vs_human/similarity",
    "phase2/evaluation/rq3_test_quality",
    "phase2/evaluation/rq3_test_quality/jacoco",
    "phase2/evaluation/rq3_test_quality/pit",
    "phase2/evaluation/rq3_test_quality/summaries",
    "phase2/artifacts",
    "phase2/artifacts/patches",
    "phase2/artifacts/logs",
    "phase2/artifacts/mapping",
    "phase2/artifacts/tables",
    "phase2/artifacts/plots",
    "phase2/scripts",
    "phase2/scripts/runners",
    "phase2/scripts/utils",
    "phase2/tools",
    "phase2/tools/evosuite",
    "phase2/docs",
)


def ensure_phase2_layout(workdir: Path) -> list[Path]:
    created: list[Path] = []
    for rel_dir in PHASE2_DIRS:
        target = workdir / rel_dir
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            created.append(target)
    return created
