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

The breaker is on by default in the Compose file and `.env.example` with the numbers in [ADR-0023](../adr/0023-circuit-breaker-thresholds.md) (15 minutes, 20 attempts, 10% failures, P95 15 s). The code has no defaults: with `BREAKER_ENABLED=YES` every number is required. Set `BREAKER_ENABLED=NO` to turn it off:

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

## Verified live, and not yet

Verified on the isolated stack on 2026-11-16: `halted` and `fallback_fixed_rule` behave as described above (scan skipped, propose refused, fixed proposal with no model key and zero model calls, LLM-made proposal held, completed after admin re-enable). See the spec for the details.

Evaluation (2026-11-16, prefix `release-gate-87-v2`): completed 62/62 and did **not** pass with the first prompt. G1 to G3 had 0 violations; G4 (English contract validity 95.24%) and G5 (action conformance 87.88% Arabic, 88.89% English) are below their thresholds. See the spec and `evals/release/results/release-gate-87-v2-evaluation/`. The first attempt (`release-gate-87-v1`) stopped on a provider timeout; a failed evaluation leaves its durable runs and an unreconciled call, so a rerun needs a new prefix and review ID, and the evaluation retries a timeout once.

After the prompt change (`proposal-v3-consent-and-facts`, prefix `release-gate-87-v3`) the evaluation **passed** all gates: contract validity and action conformance 100% in both languages, P50 2.6 s, P95 4.7 s, max 5.4 s, failure rate 0%. The prompt was tuned on those cases, so 100% is optimistic. A held-out set of 44 new cases (`heldout_cases.json`, owner-reviewed) also passed: contract validity 100%, action conformance 93.75% per language, P95 4.9 s. Run it with `bash scripts/release_gate_eval.sh {smoke|evaluation} live <sha> heldout`.

Not verified: the breaker against a real provider failure. Breaker numbers are set in ADR-0023 from the baselines above (v2: P50 2.6 s, P95 5.2 s, max 33 s, failure 1.6%; v3: P95 4.7 s, max 5.4 s, failure 0%, 62 calls each) are the input for the owner.
