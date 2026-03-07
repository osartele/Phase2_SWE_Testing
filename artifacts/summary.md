# Benchmark Summary

Generated at: `2026-03-02T03:35:05Z`

## Run Configuration

- Cases evaluated: `1`
- Case IDs: `case_0de800c69510b756`
- Approaches: `healing, regen, human`
- Mapper scope enabled: `True`
- Max iterations: `1`
- Max minutes: `1`
- Timeout seconds: `120`
- EvoSuite seed: `1337`
- EvoSuite budget seconds: `120`

## Outcome Table

| approach | runs | successes | pass_rate | avg_runtime_seconds |
| --- | --- | --- | --- | --- |
| healing | 1 | 0 | 0.0 | 6.985 |
| regen | 1 | 0 | 0.0 | None |
| human | 1 | 0 | 0.0 | 4.844 |

## RQ1 Table

| approach | rows | available_runs | pass_rate | avg_time_seconds | avg_patch_line_similarity |
| --- | --- | --- | --- | --- | --- |
| healing | 1 | 1 | 0.0 | 6.985 | 0.0 |
| regen | 1 | 0 | None | None | None |

## RQ3 Table

| approach | rows | available_runs | avg_coverage_pct | avg_mutation_score_pct | avg_runtime_seconds |
| --- | --- | --- | --- | --- | --- |
| healing | 1 | 1 | None | None | 2.219 |
| regen | 1 | 0 | None | None | None |
| human | 1 | 1 | None | None | 2.125 |

## Key Failure Modes

### Stop Reasons

- `error`: `1`
- `no_progress_no_assertion_failures`: `1`
- `still_failing`: `1`

### Execution Errors

- `jacoco_command_failed(exit=-1, error=command_error_FileNotFoundError: [WinError 2] The system cannot find the file specified); pit_command_failed(exit=-1, error=command_error_FileNotFoundError: [WinError 2] The system cannot find the file specified)`: `2`
- `FileNotFoundError: EvoSuite jar is not configured. Set --evosuite-jar, EVOSUITE_JAR, or configure tools.evosuite.jar_path / tools.evosuite.download_url.`: `1`
- `missing_regen_summary`: `1`

### RQ3 Status Distribution

- `error`: `2`
- `missing_regen_artifact`: `1`

## Artifact Index

- `benchmark_root`: `C:\Users\garre\Desktop\P2\New folder\artifacts\benchmark`
- `benchmark_outcomes_csv`: `C:\Users\garre\Desktop\P2\New folder\artifacts\benchmark\benchmark_outcomes.csv`
- `benchmark_outcomes_table_csv`: `C:\Users\garre\Desktop\P2\New folder\artifacts\tables\benchmark_outcomes_table.csv`
- `rq1_csv`: `C:\Users\garre\Desktop\P2\New folder\artifacts\benchmark\rq1_summary.csv`
- `rq3_csv`: `C:\Users\garre\Desktop\P2\New folder\artifacts\benchmark\rq3_quality.csv`
- `rq2_report`: `C:\Users\garre\Desktop\P2\New folder\artifacts\benchmark\rq2_error_report.md`
- `pass_plot`: `C:\Users\garre\Desktop\P2\New folder\artifacts\plots\benchmark_pass_rate.svg`
- `coverage_plot`: `C:\Users\garre\Desktop\P2\New folder\artifacts\plots\benchmark_coverage.svg`
- `mutation_plot`: `C:\Users\garre\Desktop\P2\New folder\artifacts\plots\benchmark_mutation.svg`