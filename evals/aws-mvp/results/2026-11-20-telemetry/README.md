# Live telemetry rollout on the AWS MVP stack (#140)

Run on 2026-11-20 from `main` at `d6257e2` (#126 telemetry core, #127 Grafana export). Smoke data: [summary.md](summary.md) and [results.json](results.json).

## Steps and results

| Step | Result |
| :-- | :-- |
| Terraform (`tf_safe.sh`, plan `328facc00902`, owner-approved) | Applied: SSM parameter `grafana-otlp-token` created; core task definition replaced with the telemetry settings and the `approval-api` `/healthz` check. Post-apply re-plan: no drift. |
| Token | Written to SSM as a SecureString (version 2) from the owner's file; value not shown. |
| Start | Core service running == desired after 469 s. |
| Deploy workflow (run 44705957423, owner-approved) | Engine image built from `main`; rollout COMPLETED after 686 s; the service uses core revision 12. |
| ECS container health | `approval-api` HEALTHY (new `/healthz` check), `engine-db` and `odoo-db` HEALTHY. |
| Smoke `--apply` | SM-1, SM-2, SM-3, SM-5, SM-6 pass; SM-4 not run (Cognito tokens not requested; not in #140 scope). |
| SM-6 | From the operator container over IPv6: the OTLP host has an AAAA record; traces, metrics, and logs accepted (check ID `d8e8fc94-a9da-4d12-9826-7173af789490`, 27.4 s). |
| ERP scan (`full_stack_worker --once`, `SCAN_MODE=erp`) | 12 carts scanned, 1 run created (skips: missing email 1, blacklisted 1, no consent 6, cooldown 3). |
| Container logs | No export error in the `worker` or `approval-api` logs. |
| Stop | Instance stopped at 09:58:40 UTC; inventory estimate USD 2.61/month at this state. |

## Seen in Grafana Cloud (owner, 2026-11-20)

- Metrics from `job="aorte-worker"`: `aorte_execution_held`, `aorte_runs`, `aorte_runtime_state_ratio` (active), `aorte_scan_carts_total`, `aorte_telemetry_check_total`, `aorte_worker_last_scan_age_seconds`. Prometheus storage adds unit suffixes (`_ratio`, `_seconds`, `_total`).
- The last-scan age rises to about 55 to 60 s and drops at each scan, as expected for the 60 s scan interval. A useful alert threshold for #139 is three intervals (about 180 s).
- Logs: service `aorte-worker`, 30 INFO lines in 15 minutes.
- Traces: service `aorte-worker` scan spans about once a minute, slowest about 67 ms.
- Grafana also created `asserts:*` series from the `aorte-worker` job (its own trial feature). They may count against the free active-series limit; measure the total in #133.

## Cost

The instance ran from 08:42:59 to 09:58:40 UTC (about 1.26 h), about USD 0.05 at USD 0.0376 per hour (F-076). No LLM call was made. Grafana: no payment method on the account.

## Not done, and why

- **Propose and execute for the new run.** The scan logs only counts, and the operator allowlist has no read-only way to list run IDs. A read-only `erp_cli list` is added to #131; the full propose/execute flow runs in the Tier 2 close (#133).
- **SM-4 (Cognito RBAC).** Needs owner sign-in with MFA; unchanged since 2026-11-18 and not part of #140.

## Findings

- The public deploy log shows the AWS account ID (13 times) and one bucket name, but no secret: #142.
- The Grafana account is in a 14-day trial; what happens without a payment method when it ends is recorded in F-085.
