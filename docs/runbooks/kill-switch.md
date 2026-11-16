# Runbook: engine runtime state, kill-switch, and release-gate evaluation

Spec: [release-gates-and-kill-switch.md](../specs/release-gates-and-kill-switch.md). Decision: [ADR-0022](../adr/0022-runtime-state-fixed-rule-fallback-and-breaker.md). Issue #87.

The engine has one runtime state in the engine database. The worker reads it before every customer-facing step. The API only writes it, and only an `admin` can.

## The three states

| State | What happens |
| :-- | :-- |
| `active` | Normal. The model writes proposals; everything goes through the gate. |
| `fallback_fixed_rule` | New carts get a fixed 0% reminder (English or MSA) through the same gate, consent, allowlist, and approval path. No model call. Proposals the model already made are held, not executed. |
| `halted` | No new runs, no proposals, no execution. Approvals can still be recorded. |

In-flight runs:

- A held or halted run changes nothing and stays resumable. It resumes when the state returns to `active` (and, for fixed-rule runs, in fallback), unless its approval expires first ([operator RBAC spec](../specs/operator-rbac-and-expiring-approvals.md)).
- A halt or fallback that arrives after a run was claimed fails that run (`engine_halted` or `engine_fallback`) before its next send. A message already sent is never undone.
- If the state cannot be read, the engine behaves as `halted`.

## Read and change the state

You need a bearer token with the right role (`APPROVAL_ACCESS_TOKEN`, see [approval-local.md](approval-local.md)). Reading needs `viewer`; changing needs `admin`. The reason is a short note with no names, emails, or phone numbers.

```sh
PYTHONPATH=src uv run --no-sync python -m engine.approval_cli runtime
PYTHONPATH=src uv run --no-sync python -m engine.approval_cli set-runtime \
  --state halted --reason "provider incident under review"
PYTHONPATH=src uv run --no-sync python -m engine.approval_cli set-runtime \
  --state active --reason "incident resolved" --expected-version <version from runtime>
```

`--expected-version` is optional but recommended: if another admin changed the state first you get HTTP 409 instead of overwriting. Every change is recorded with the token `sub`, the reason, and the time. The API has `GET /runtime` and `POST /runtime/state` for the same actions.

## Circuit breaker

The worker checks the model's recent failures and latency each scan. If a threshold is crossed with enough samples, it switches `active` to `fallback_fixed_rule` (actor `circuit-breaker`). It never re-enables the model and never halts. An admin returns the engine to `active` after fixing the cause.

The breaker is off unless `BREAKER_ENABLED=YES`. Then every number is required and has no default:

| Setting | Meaning |
| :-- | :-- |
| `BREAKER_WINDOW_MINUTES` | look-back window |
| `BREAKER_MIN_SAMPLE` | model attempts needed before it may trip |
| `BREAKER_MAX_FAILURE_RATE_PERCENT` | provider or output-contract failures as a share of attempts |
| `BREAKER_MAX_P95_SECONDS` | P95 time from `generating_proposal` to `evaluating_policy` |

Set the numbers from the baselines in the release-gate evaluation report (below), not from guesses. Attempts are runs that reserved a model budget, so fixed-rule runs never dilute the rates.

## Release-gate evaluation

Gates, thresholds, and limits are in the spec. The evaluation runs reviewed English and MSA cases through the real proposal step with the aggregate ceiling of USD 2.00, in the isolated full-stack Compose (the same setup as the POC-2 evaluation). It needs `OPENAI_API_KEY` and an explicit owner approval before any paid call.

```sh
bash scripts/release_gate_eval.sh smoke preflight        # read-only, no model call
bash scripts/release_gate_eval.sh smoke live              # one paid case; owner reviews the report
bash scripts/release_gate_eval.sh evaluation preflight <sha256 of the reviewed smoke raw.json>
bash scripts/release_gate_eval.sh evaluation live <sha256 of the reviewed smoke raw.json>
```

Reports are written to `evals/release/results/release-gate-87-{smoke,evaluation}/` (`raw.json` and `README.md`) only after the whole run succeeds. Preflight does not check provider access, model output quality, or delivery. The stages print progress with elapsed time; a failed run names the stage and keeps any earlier report.

## Not verified yet

The live evaluation and a live halt/fallback check on the isolated Compose stack have not been run; the behavior is covered by hermetic tests. Do both only with the owner's approval.
