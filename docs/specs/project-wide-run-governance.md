# Spec: Project-wide preflight, progress, and review-report standards

- Issue: #51
- ADRs: none
- Status: agreed

## Purpose

Prevent predictable failures after long waits, make long-running work observable, and give reviewers a concise, trustworthy summary of results.

## Scope and non-goals

This is a project-wide standard for every phase and domain, not an S3-only rule. Apply it to expensive or long-running benchmarks, evaluations, data preparation, model runs, migrations, and commands with external-service or state-changing effects. S3 is the first implementation and test case, not the boundary of the policy. A short check with natural per-check output does not need a separate progress system or review report.

Implement the first complete example for the S3 benchmark. Inventory existing commands and track unrelated gaps instead of rewriting them here. Do not add a general job framework or progress-bar dependency, force parallel execution, or make CI start benchmark services or download models.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| INV-1 | A fast preflight checks required inputs, configuration, tools, permissions, and output paths before expensive setup or side effects. An end-to-end smoke check validates representative service contracts before the full S3 run. | `test_preflight_rejects_invalid_inputs_before_docker`; `test_s3_smoke_checks_backends_without_model_download` |
| INV-2 | Preflight and smoke output state what they cover and do not guarantee. They must not be described as proof that the full run, model download, or retrieval quality will succeed. | `test_preflight_and_smoke_document_limits`; CI stays service- and model-free |
| INV-3 | Long work reports its current stage, completed/total units where known, and elapsed time. Stages without countable units emit a heartbeat at least every 30 seconds. | `test_progress_reports_stage_units_and_elapsed`; `test_slow_stage_emits_heartbeat` |
| INV-4 | A failed stage exits nonzero with the failing stage identified, cleans up only resources created by that run, and does not replace the last successful result with partial output. | `test_launcher_failure_keeps_published_reports`; `test_failure_names_stage_and_cleans_run_resources` |
| INV-5 | Results intended for human review include Markdown and machine-readable reports from the same successful run. Markdown includes key results, interpretation, configuration/provenance, limitations, and a link to raw data. | `test_markdown_summary_is_human_readable_and_links_machine_report` |
| INV-6 | Where parallel execution is plausible, a controlled comparison records elapsed time, resource impact, ranking/measurement reliability, and the chosen default. If it is not relevant, the runbook records why. | S3 runbook experiment record; `test_benchmark_report_records_execution_mode`; `test_execution_mode_metadata_is_reported` |
| INV-7 | Existing long-running commands are inventoried; concrete out-of-scope gaps have a follow-up issue or a documented reason no follow-up is needed. | Inventory below and in the S3 runbook; S1 gap tracked in #53 |

## Behavior

- **Given** a missing or invalid required input, **when** a long command starts, **then** preflight reports the exact prerequisite and exits before starting services, downloading models, or mutating state.
- **Given** valid S3 inputs and ready backends, **when** the smoke check runs, **then** it exercises a small synthetic backend contract without loading the embedding model; its output names what the check does not cover.
- **Given** a running benchmark, **when** a query, repetition, or long setup stage advances, **then** progress output shows stage and elapsed time, plus completed/total work where available.
- **Given** a failure in retrieval, memory collection, report generation, or cleanup, **when** the launcher exits, **then** it identifies the failed stage, returns nonzero, and leaves the prior published JSON/Markdown pair intact.
- **Given** a successful run, **when** reports are published, **then** JSON and Markdown describe the same completed measurements and link to each other.
- **Given** a plausible sequential/parallel choice, **when** a bounded pilot compares the modes, **then** it holds the data and measurement settings constant, records wall time, resource use, and ranking/measurement consistency, and does not treat concurrent-load latency as equivalent to serial latency. If comparison is not relevant, the runbook records the reason and limits.

## Risks

- A preflight can pass while a later model download or full run still fails; the scope of each check must be explicit.
- Progress output must not include credentials, full customer-like records, or other sensitive values.
- Parallel execution can change latency through contention. The measured result may justify keeping the full benchmark sequential.

## Existing command inventory

- `make setup` performs locked dependency synchronization after checking the required tool versions and Docker; it uses the package manager's own output.
- `make ci` reports each pre-commit check and traceability result, so no additional heartbeat system is needed.
- `make benchmark-s3` is the first implementation of this standard and is covered by the S3 runbook.
- S1 Odoo Compose startup and `scripts/spike_s1_odoo.py` probe/seed lack stage progress. This out-of-scope gap is tracked in #53.

## Open questions

None. The owner agreed to the spec and the 30-second heartbeat default on 2026-10-31.
