# Runbook: S2 bilingual LLM evaluation

This runbook uses the frozen synthetic S2 dataset. CI and all commands below are separate from live provider calls. No command in normal CI contacts Odoo or an LLM provider.

## 1. Review the dataset

Generate the human worksheet and the pending machine-readable review ledger:

```sh
uv run --no-sync python -m scripts.s2_llm_cli review-assets
```

Review every input and its paired meaning, expected action, risk/category, required tool use, price-check flag, discount-claim check, and synthetic cart facts in `evals/llm/s2_review_worksheet.md`. Contact values are omitted from the worksheet. Version 3 uses six shared MSA pivots with distinct dialect wording; it is exploratory, not broad dialect coverage. Approve English/MSA cases and record qualified reviewers for each dialect in `evals/llm/s2_reviews.json`. Version 1 was returned with changes requested, and v2 received final wording corrections; no earlier approvals carry forward. Keep cases pending until reviewed; the dataset stays in draft status, and must not be made runnable by changing its status. The review ledger must match the exact dataset hash. Any dataset edit requires a new review. This step is offline and does not call Odoo or a model provider.

## 2. Prepare owner approvals

Copy `evals/llm/s2_live_approval.example.json` to `evals/llm/s2_live_approval.json`. Keep the working approval file out of Git. Do not put provider keys in it. Set `owner_approved` and explicitly list approved candidates only after review.

For each candidate, record active RPM/TPM limits from the provider project dashboard, the source and check date, and per-request token ceilings. Use only the published-list cost caps already recorded in the S2 spec. Do not infer provider quotas or account credits. The owner approved hard ceilings of 4,000 input tokens and 8,192 output tokens per request. For the smoke, the owner also approved at most four generation requests and a $0.15 per-run generation-token ceiling. The maximum published-list reservation is $0.13488 using F-004 rates; the 4,000-token input cap is not permission to pad or lengthen prompts. The owner reports 1,000 RPM and 2,000,000 TPM for Gemini 3.8 Flash and Flash-Lite, checked 2026-11-02, sourced from [Google AI Studio projects](https://aistudio.google.com/projects); the account-specific values are owner-reported, not independently verified. Before each Gemini smoke generation request, the runner calls `countTokens` with the exact synthetic contents, system instruction, tool declarations, and generation configuration; it fails before generation if the actual input exceeds the approved per-request ceiling. `countTokens` does not generate an answer; its direct billing/quota treatment is not specified in the official docs and remains unknown, so it is excluded from the $0.15 generation-token ceiling. The smoke report uses successful response token usage to calculate a published-list estimate; no account-level billed amount is available from the response. Both selected smoke cases may call the cart tool, so reserve for up to four generation requests (two initial requests and up to two follow-ups). The owner approved creating and removing one fixed internal activity on the eligible synthetic cart during smoke. Read-only inspection of this disposable instance selected its active `admin` user and `To-Do` activity type; their instance-specific IDs are stored only in the ignored live-approval file and must not be reused on another database.

Neither Gemini GenerateContent nor OpenAI Responses returns a per-request USD cost in its generation response; each returns token usage. S2 calculates a published-list estimate from complete usage and F-004, not account billing. Provider-side reporting is aggregate: Google AI Studio has a project/model daily cost dashboard and monthly project spend caps with an approximately 10-minute delay; OpenAI's organization Costs API returns cost amounts in daily buckets and requires an admin API key. OpenAI's separate Usage API offers finer-grained usage buckets, not per-request USD costs ([F-032](../facts-register.md), [F-035](../facts-register.md), [F-036](../facts-register.md)).

**Monitoring recommendation, not yet implemented:** preserve per-call provider/model identifiers and exact usage, calculate a versioned published-price estimate, and send it as a clearly labeled estimate to the planned Langfuse generation and a custom OpenTelemetry metric for Grafana. Langfuse can calculate per-generation cost from usage and configured prices and alert on thresholds, but inferred costs remain estimates; use provider-side aggregate reports for reconciliation. The current S2 CLI has no Langfuse/Grafana cost instrumentation. Do not send customer data in traces or metric labels. The monitoring path, alert thresholds, and reconciliation cadence remain open; see [ADR-0007](../adr/0007-free-tier-observability.md), [F-033](../facts-register.md), and Q20 in [STATUS](../STATUS.md).

Gemini usage accounting must fail closed unless prompt, tool-use prompt, candidate, and thought counts are exact. A single missing component may be derived from `total_token_count` and the other three present components. Multiple missing components may be derived only when `total_token_count` equals the sum of the present components, which proves every omitted nonnegative count is zero; a nonzero residual cannot be allocated exactly. Reject negative residuals and total mismatches. CI tests both exact derivation paths and the fail-closed cases; CI does not make live provider calls.

Store provider keys and the local Odoo API key only in process environment variables. Never pass them as command-line arguments or write them to reports.

## 3. Start a disposable local Odoo instance

Use a unique Compose project name and the S1 profile so this run has its own database and filestore:

```sh
ODOO_HOST_PORT=8070 docker compose --project-name s2-llm-smoke --env-file .env -f deploy/docker-compose.yml --profile s1 up -d
```

`ODOO_HOST_PORT` changes only the host-side loopback port; Odoo still listens on port 8069 inside the container. Set `ODOO_URL=http://127.0.0.1:8070` in the local process environment for this run. Choose another unused loopback port if 8070 is occupied; never connect the S2 runner to an existing Odoo instance.

Complete local Odoo initialization and create an API key for the local administrator. Store it in the untracked local `.env` as `DISPOSABLE_ODOO_API_KEY`. This is an admin-level credential and remains security-sensitive even though its database is disposable: use it only with the S2 instance on loopback, never reuse it on a shared Odoo, and revoke it or discard the instance after the smoke. Never commit, print, log, or include it in reports. Load local-only environment variables into the shell without printing them:

```sh
set -a
. ./.env
set +a
export ODOO_API_KEY="${DISPOSABLE_ODOO_API_KEY:?set the disposable instance key in .env}"
export S2_DISPOSABLE_INSTANCE=YES
export ODOO_URL=http://127.0.0.1:8070
```

`ODOO_URL` must resolve to loopback. The seed uses read-only `fields_get` calls to check required Odoo model field presence, then checks the measured 10-hour website setting, disabled reminder, USD website and pricelist, and unused S2 references. It sets each new cart line's `price_unit` from the reviewed synthetic fixture so a default pricelist rule cannot silently alter the approved scenario; it does not change catalog prices, pricelists, website settings, or existing products or partners. Do not infer JSON-2 write permission from the UI-only `readonly` metadata; the seed validates actual create/read behavior through the local Odoo API. F-028 remains unverified until the activity lifecycle and provider smoke are exercised.

For the already-approved `s2-smoke-001` instance, the owner explicitly authorized correcting only the two synthetic cart lines after a read-only check found that the default pricelist had applied a 10% reduction. The JSON-2 writes restored the reviewed fixture unit prices, and read-back plus `get_cart_context` matched the approved inventory. This is not permission to change prices in any other database. If another seeded cart differs from its reviewed fixture, stop before provider calls and obtain owner approval before any API write.

Seed only this disposable instance, with a unique run ID:

```sh
uv run --no-sync python -m scripts.s2_odoo_smoke seed --execute-live --run-id s2-smoke-001
```

Inspect both `evals/llm/s2_odoo_inventory.md` and the inventory JSON. Only after verifying the exact 1-hour negative control, 11-hour eligible cart, USD product facts, and disabled reminder, set `approval.status` to `approved` and enter a reviewer ID. The inventory hash covers its contents but excludes the approval block. Never reuse this database for another experiment.

## 4. Preflight and run the bounded synchronous smoke

Preflight checks the dataset review, selected cases, approvals, current facts, local environment, and output path; it does not call Odoo or a provider. It reserves the full output cap at the published output rate, including possible Gemini thought tokens, and assumes no cache discount:

```sh
uv run --group llm-eval python -m scripts.s2_llm_cli preflight \
  --mode smoke --candidate gemini-3.8-flash-low
```

The smoke command repeats preflight. It rechecks Odoo field metadata and reads both approved carts from the local Odoo API before creating a provider client, then evaluates the fixed English recovery and recent-cart control cases. Before each Gemini generation request, it sends the corresponding full `generateContentRequest` to the official `countTokens` REST endpoint so system instructions, tools, and generation settings are counted; the locked Python SDK's Developer API `CountTokensConfig` rejects these fields (F-030). The runner uses one request with a 30-second timeout, keeps the API key in the `x-goog-api-key` header, and fails closed on count errors or an input-ceiling breach. Tool follow-ups place the function result in a `user` turn and JSON-serialize the returned model content, including byte-valued thought signatures, before counting. Only the eligible recovery case can create the fixed internal activity, after deterministic scoring and a live eligibility re-check. The command removes only an activity created by this run, verifies removal, and publishes JSON and Markdown only after cleanup succeeds. This command is limited to the approved smoke cases and does not start the full comparison; do not retry automatically:

The count-only preflight completed on 2026-11-02 for the approved `s2-golden-v3` dataset and inventory. Its paired report is [machine-readable](../../evals/llm/results/preflight/s2-smoke-001-gemini-3.8-flash-low-counts.json) and [human-readable](../../evals/llm/results/preflight/s2-smoke-001-gemini-3.8-flash-low-counts.md). Canonical counts are 271/269 input tokens for the two initial requests and 406/399 for their follow-ups; the follow-up counts omit provider-generated model content and are not a substitute for the runtime exact count. The report reflects the state when it ran, before the owner approved the 4,000-token input cap and $0.15 generation-token ceiling; it remains a count-only report, not a model smoke result.

```sh
uv run --group llm-eval python -m scripts.s2_llm_cli smoke \
  --execute-live --candidate gemini-3.8-flash-low --run-id s2-smoke-001
```

Review the paired report in `evals/llm/results/smoke/`. A prior smoke report used an older prompt and cannot approve the changed prompt. To authorize the full comparison later, place the exact new JSON report SHA-256 and reviewer ID into the private approval file. Any report edit invalidates that approval.

Historical `s2-smoke-001` on 2026-11-02 passed both cases with zero hard-gate failures, but it used an older prompt and cannot approve the full comparison. Its details remain in its [Markdown report](../../evals/llm/results/smoke/s2-smoke-001-gemini-3.8-flash-low.md) and [JSON report](../../evals/llm/results/smoke/s2-smoke-001-gemini-3.8-flash-low.json).

The revised-prompt run `s2-smoke-002` on 2026-11-03 passed 2/2 cases with zero hard-gate failures. It used 3,366 input tokens and 386 output tokens (including 255 reasoning tokens); the F-004 published-list estimate is $0.003972, not account billing. The eligible-cart activity was created and cleaned up, and the recent-cart control created none. The JSON report SHA-256 is `8588e73c7e49c8ac391408ce049cea8708ee3121aa2b17acb7db5371265c9e78`. The owner re-reviewed the corrected [human-readable report](../../evals/llm/results/smoke/s2-smoke-002-gemini-3.8-flash-low.md) and re-approved that exact JSON hash on 2026-11-03. The local approval file records it. This smoke does not select a model.

## 5. Preflight and run the full synchronous-parallel comparison

After the owner has reviewed the new smoke report and its hash is recorded in the approval file, run preflight for all approved candidates:

The owner selected all four candidates and approved the hard token ceilings. The owner also approved the Flash-Lite cap of $2.428160 and a full-run concurrency of 16. On 2026-11-03, the owner reconfirmed unchanged active limits: Gemini Tier 1 and 1,000 RPM/2,000,000 TPM for both Gemini candidates; OpenAI Build and 5,000 RPM/2,000,000 TPM for both Luna candidates. These account-specific values are owner-reported, not independently verified. Official tier information is in [F-037](../facts-register.md). The previous Batch run is provisional and not decision-grade. Do not use its scores to select a model. `OPENAI_API_KEY` is available only in the owner's local environment; never put credentials in the approval file or chat.

Before each OpenAI generation request, the runner calls the documented Responses input-token endpoint with the same model, input items, instructions, tool declarations, structured-output format, reasoning effort, and truncation settings. It fails before generation if counting fails or exceeds the approved input ceiling. The count-only endpoint's billing and quota treatment is not specified in the official docs; do not treat its token count as provider response usage or account billing. See [F-038](../facts-register.md).

```sh
uv run --group llm-eval python -m scripts.s2_llm_cli preflight \
  --mode full --smoke-report evals/llm/results/smoke/<new-approved-smoke>.json
```

The full comparison defaults to all four approved configurations, executes candidates sequentially, and runs each candidate's synchronous requests in parallel with a maximum of 16 cases in flight. The run-time cost guard considers the shared GPT-6 Luna cap across both candidates. If running only a subset is explicitly approved, add `--candidates <candidate-id>` once per candidate to both commands. Any absent reviewer approval, active quota, token ceiling, valid rate, or budget causes a fail-closed preflight.

```sh
uv run --group llm-eval python -m scripts.s2_llm_cli full \
  --execute-live --run-id s2-full-001 \
  --smoke-report evals/llm/results/smoke/<new-approved-smoke>.json
```

Full-run tool requests use frozen fixtures, not Odoo. The command stages all candidate report pairs and publishes them together only after every candidate run and report generation succeeds. Transient 429/5xx errors receive bounded retries; any remaining provider error or cost-guard failure makes the run incomplete and nonzero. A completed evaluation with deterministic case failures still publishes the complete result set, reports a nonzero exit, and must not be interpreted as a model-selection decision.

Run `s2-full-001` completed all 132 cases for each of the four candidates on 2026-11-03 and published all report pairs. The command exited nonzero because deterministic case failures were found. Pass counts: Gemini 3.8 Flash 91/132, Gemini Flash-Lite 70/132, Luna `none` 106/132, and Luna `low` 111/132. All candidates passed the bounded discount/injection text checks 37/37, but all had other hard-gate failures, including cross-customer and money-boundary cases. Review the [Gemini 3.8 Flash report](../../evals/llm/results/full/s2-full-001/gemini-3.8-flash-low.md), [Flash-Lite report](../../evals/llm/results/full/s2-full-001/gemini-3.5-flash-lite-minimal.md), [Luna none report](../../evals/llm/results/full/s2-full-001/gpt-6-luna-none.md), and [Luna low report](../../evals/llm/results/full/s2-full-001/gpt-6-luna-low.md). Combined published-list usage estimate was $0.384558, not account billing; count-only endpoint costs are unknown and excluded. No model was selected. Any future live rerun requires fresh smoke review and approval because the smoke Markdown report was corrected after this run.

## 6. Review, record, and clean up

Review Markdown first, then inspect the linked JSON for per-case details, language/task/challenge/dialect/risk slices, discount/injection output-safety counts, input usage including tool-use prompt tokens, output usage including thought tokens, cache usage, cost estimate, candidate settings, dataset and prompt/schema hashes, quotas, smoke activity cleanup, and limitations. The discount text matcher is a bounded regression gate, not a general classifier or a replacement for deterministic money controls. Model-as-judge or human language-quality notes are advisory; deterministic safety/fact checks are not.

Do not select a model from a partial run or from the smoke alone. Record the final choice only after the reviewed full comparison. If a live run is not authorized or its required facts are unknown, leave model selection unresolved in ADR-0016 and keep the open questions in `docs/STATUS.md`.

Stop and remove only the dedicated S2 Compose project when finished:

```sh
docker compose --project-name s2-llm-smoke --env-file .env -f deploy/docker-compose.yml --profile s1 down --volumes --remove-orphans
```

This removes the database and filestore for that named disposable project. Do not run it with the S1 project name.
