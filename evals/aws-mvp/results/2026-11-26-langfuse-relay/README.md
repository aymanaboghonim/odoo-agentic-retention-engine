# AWS host exports to Langfuse through the CloudFront relay (#152)

Run on 2026-11-26 from `main` at `958e85c` (#129 relay, #150 engine side). Smoke data: [summary.md](summary.md) and [results.json](results.json).

## Steps and results

| Step | Result |
| :-- | :-- |
| Preflight | Instance stopped, 0 connected container instances; latest core revision 14 (Terraform, #150) has `LANGFUSE_OTLP_ENDPOINT` and the `TELEMETRY_RELAY_KEY` secret in `worker`, `approval-api`, `operator`; the Langfuse keys, relay key, and Grafana token are set in SSM (not the placeholder). |
| Deploy workflow (run 45773881541, owner-approved) | Green in 85 s while the stack was stopped. The rollout registered core revision 15, printed `stack stopped (no connected container instance)`, and exited after 4 s without waiting (#148). Log: 0 account ID matches; 0 artifacts (#142). |
| Start | Requested 07:16:53 UTC; core service running == desired after 467 s; the API answers 401 through CloudFront at 07:26:02. |
| Smoke `--apply` | SM-1, SM-2, SM-3, SM-5, SM-6 pass; SM-4 not run (Cognito tokens not requested; not in scope). |
| SM-6 | From the operator container: Grafana traces, metrics, and logs accepted, and the Langfuse export accepted through the relay (`export-langfuse status=ok`, check ID `eebffc50-0b85-413e-a0f2-4b4016524826`, 46.6 s). |
| Langfuse | `GET /api/public/v2/observations` (`F-090`) lists trace `eebffc500b85413ea0f24b4016524826`: `aorte.check`, type EVENT, environment `aws-mvp`, no input or output. |
| Container logs | CloudWatch `/aorte-mvp/ecs`, 07:16 to 07:30 UTC: no line matching ERROR, Failed, Traceback, 403, refused, or `TelemetryConfigError`. |
| Stop | Instance stopped at 07:29:29 UTC. |

## Cost

The instance ran from about 07:17 to 07:29:29 UTC (about 0.21 h), about USD 0.01 at USD 0.0376 per hour (F-076). No LLM call was made. CloudFront requests and function invocations are inside the Always Free allowance (F-065). No payment method on Grafana or Langfuse.

## Not done, and why

- **SM-4 (Cognito RBAC).** Needs owner sign-in with MFA; unchanged since 2026-11-18.
- **A run trace from the engine (stage spans, operator decisions).** Needs a run through propose and approve, which needs the run list (#131); the full flow is the Tier 2 close (#133). This run proves the path: the same exporter and relay carry run spans.

## Findings

- The relay keeps the Langfuse unit count low: only `LANGFUSE_SPANS` go there, under a daily cap of 300 per process (`langfuse_daily_unit_cap`, #150).
