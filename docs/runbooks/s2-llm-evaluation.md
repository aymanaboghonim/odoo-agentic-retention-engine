# Runbook: S2 bilingual LLM evaluation

This runbook uses the frozen synthetic S2 dataset. CI and all commands below are separate from live provider calls. No command in normal CI contacts Odoo or an LLM provider.

## 1. Review the dataset

Generate the human worksheet and the pending machine-readable review ledger:

```sh
uv run --no-sync python -m scripts.s2_llm_cli review-assets
```

Review every input and its paired meaning, expected action, risk/category, required tool use, price-check flag, and synthetic cart facts in `evals/llm/s2_review_worksheet.md`. Contact values are omitted from the worksheet. Approve English/MSA cases and record qualified reviewers for each dialect in `evals/llm/s2_reviews.json`. Keep cases pending until reviewed; the dataset stays in draft status, and must not be made runnable by changing its status. The review ledger must match the exact dataset hash. Any dataset edit requires a new review. This step is offline and does not call Odoo or a model provider.

## 2. Prepare owner approvals

Copy `evals/llm/s2_live_approval.example.json` to `evals/llm/s2_live_approval.json`. Keep the working approval file out of Git. Do not put provider keys in it. Set `owner_approved` and explicitly list approved candidates only after review.

For each candidate, record active RPM/TPM and Batch queue limits from the provider project dashboard, the source and check date, and per-request token ceilings. Use only the published-list cost caps already recorded in the S2 spec. Do not infer provider quotas or account credits. Before a Batch run, record the owner-reviewed smoke report hash and reviewer ID. For smoke, configure the local activity user and activity type IDs after inspecting the disposable Odoo instance.

Store provider keys and the local Odoo API key only in process environment variables. Never pass them as command-line arguments or write them to reports.

## 3. Start a disposable local Odoo instance

Use a unique Compose project name and the S1 profile so this run has its own database and filestore:

```sh
docker compose --project-name s2-llm-smoke --env-file .env -f deploy/docker-compose.yml --profile s1 up -d
```

Complete local Odoo initialization and create a local administrator API key. Load local-only environment variables into the shell without printing them:

```sh
set -a
. ./.env
set +a
export S2_DISPOSABLE_INSTANCE=YES
```

`ODOO_URL` must resolve to loopback. The seed uses read-only `fields_get` calls to check the required Odoo model fields and writable flags, then checks the measured 10-hour website setting, disabled reminder, USD website and pricelist, and unused S2 references. This metadata preflight does not prove that the local write lifecycle works; F-028 remains unverified until the disposable smoke succeeds. The seed does not modify existing products, partners, website settings, or database rows directly.

Seed only this disposable instance, with a unique run ID:

```sh
uv run --no-sync python -m scripts.s2_odoo_smoke seed --execute-live --run-id s2-smoke-001
```

Inspect both `evals/llm/s2_odoo_inventory.md` and the inventory JSON. Only after verifying the exact 1-hour negative control, 11-hour eligible cart, USD product facts, and disabled reminder, set `approval.status` to `approved` and enter a reviewer ID. The inventory hash covers its contents but excludes the approval block. Never reuse this database for another experiment.

## 4. Preflight and run the bounded synchronous smoke

Preflight checks the dataset review, selected cases, approvals, current facts, local environment, and output path; it does not call Odoo or a provider:

```sh
uv run --group llm-eval python -m scripts.s2_llm_cli preflight \
  --mode smoke --candidate gemini-3.8-flash-low
```

The smoke command repeats preflight. It rechecks Odoo field metadata and reads both approved carts from the local Odoo API before creating a provider client, then evaluates the fixed English recovery and recent-cart control cases. Only the eligible recovery case can create the fixed internal activity, after deterministic scoring and a live eligibility re-check. The command removes only an activity created by this run, verifies removal, and publishes JSON and Markdown only after cleanup succeeds:

```sh
uv run --group llm-eval python -m scripts.s2_llm_cli smoke \
  --execute-live --candidate gemini-3.8-flash-low --run-id s2-smoke-001
```

Review the paired report in `evals/llm/results/smoke/`. To authorize the full comparison later, place the exact JSON report SHA-256 and reviewer ID into the private approval file. Any report edit invalidates that approval.

## 5. Preflight and run the full Batch comparison

After the owner has reviewed the smoke report and its hash is recorded in the approval file, run preflight for all approved candidates:

```sh
uv run --group llm-eval python -m scripts.s2_llm_cli preflight \
  --mode batch --smoke-report evals/llm/results/smoke/s2-smoke-001-gemini-3.8-flash-low.json
```

The full comparison defaults to all four approved configurations, runs sequentially, and checks the combined GPT-6 Luna reservation before any provider client is created. If running only a subset is explicitly approved, add `--candidates <candidate-id>` once per candidate to both commands. Any absent reviewer approval, active quota, token ceiling, valid rate, or budget causes a fail-closed preflight.

```sh
uv run --group llm-eval python -m scripts.s2_llm_cli batch \
  --execute-live --run-id s2-batch-001 \
  --smoke-report evals/llm/results/smoke/s2-smoke-001-gemini-3.8-flash-low.json
```

Batch requests use frozen fixtures, not Odoo. The command stages all candidate report pairs and publishes them together only after every candidate run and report generation succeeds. A completed evaluation with deterministic case failures still publishes the complete result set, reports a nonzero exit, and must not be interpreted as a model-selection decision.

## 6. Review, record, and clean up

Review Markdown first, then inspect the linked JSON for per-case details, language/task/challenge/dialect/risk slices, token and cache usage, cost estimate, candidate settings, dataset and prompt/schema hashes, quotas, smoke activity cleanup, and limitations. Model-as-judge or human language-quality notes are advisory; deterministic safety/fact checks are not.

Do not select a model from a partial run or from the smoke alone. Record the final choice only after the reviewed Batch comparison. If a live run is not authorized or its required facts are unknown, leave model selection unresolved in ADR-0016 and keep the open questions in `docs/STATUS.md`.

Stop and remove only the dedicated S2 Compose project when finished:

```sh
docker compose --project-name s2-llm-smoke --env-file .env -f deploy/docker-compose.yml --profile s1 down --volumes --remove-orphans
```

This removes the database and filestore for that named disposable project. Do not run it with the S1 project name.
