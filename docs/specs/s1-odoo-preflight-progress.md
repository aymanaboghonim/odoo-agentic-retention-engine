# Spec: S1 Odoo preflight and progress

- Issue: #53
- Standard: [project-wide run governance](project-wide-run-governance.md)
- Baseline: [S1 Odoo baseline](s1-odoo-baseline.md)
- Status: agreed (issue #53 "Done when" list)

## Purpose

Give the local S1 Odoo operations the project-wide preflight and progress behavior: a fast preflight, stage progress with bounded heartbeats, and explicit stage failures. The probe and optional seed keep their current behavior and safety rules.

## Scope and non-goals

Scope: `scripts/spike_s1_odoo.py` (probe, optional seed), a `--preflight-only` mode, an optional `--wait-ready` mode for the manual Odoo startup wait, the S1 runbook, and hermetic tests.

Non-goals: starting or stopping Compose from the script, changing what the probe reads or what the seed writes, new dependencies, a job framework, and starting Odoo in CI.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| S1P-INV-1 | Preflight runs before any probe or seed call. It reports missing configuration, an unreachable or not-ready Odoo, and a rejected API key or database, by name, and exits nonzero before any seed write. | `test_preflight_failure_stops_before_probe_and_seed`; `test_preflight_reports_missing_api_key` |
| S1P-INV-2 | Preflight only reads. It uses the login page and one `search_count` call, and never calls `create` or `write`. | `test_preflight_makes_read_only_calls` |
| S1P-INV-3 | Each stage prints start, completed or failed, and elapsed seconds. A stage that runs longer than the heartbeat interval prints a heartbeat. The default interval is 30 seconds. | `test_stage_reports_start_and_completion`; `test_slow_stage_emits_heartbeat` |
| S1P-INV-4 | A failure names its stage, returns nonzero, and the seed is not attempted after an earlier failure. | `test_failed_stage_names_stage_and_exits_nonzero` |
| S1P-INV-5 | Progress and failure output never contain the API key or full record data. | `test_progress_output_hides_api_key` |
| S1P-INV-6 | `--wait-ready` is bounded. It reports progress while waiting and fails with the stage name when the timeout passes. | `test_wait_ready_times_out_with_stage`; `test_wait_ready_succeeds_after_retry` |
| S1P-INV-7 | The existing loopback-only target guard and API-only, verified seed are unchanged. CI does not start Odoo or use real credentials. | Existing S1 tests; all new tests use fakes |

## Behavior

- **Given** `ODOO_API_KEY` is missing or `ODOO_URL` is not a loopback URL, **when** the script starts, **then** preflight fails on `configuration` before any request.
- **Given** Odoo is still installing modules, **when** `--wait-ready SECONDS` is used, **then** the script polls the login page with progress until it answers or the bound passes.
- **Given** a valid configuration, **when** preflight passes, **then** the probe runs with stage progress, and the seed runs only if requested.
- **Given** `--preflight-only`, **when** preflight passes, **then** the script prints what it did and did not verify and exits 0 without probing or seeding.

## Limits (stated in the runbook and in the output)

Preflight proves that the configuration is valid, the login page answers, and the API key can read from the named database once. It does not prove that module installation is complete, that demo data exists, that a reminder is enabled, or that the seed will succeed. The probe and seed write no files, so there is no output path to check. The script does not use Docker, so Docker is not a preflight requirement.

## Risks

- The login page can answer before installation is fully done; the authenticated read is the stronger readiness signal, and the probe still reports its own failures.
- A long single API call cannot show countable progress; it relies on the heartbeat.
