# Runbook: POC-2 synthetic evaluation

This workflow is for #69 only. It evaluates reviewed synthetic fixtures
through the current proposal service, with the local policy index and a live
provider. It does not create an Odoo coupon or dispatch a message. The
separate delivery review must use an isolated Odoo instance, the same #69
aggregate generation budget, the deterministic financial gate and explicit
run-specific destination confirmation.

The approved inventory is in `scripts/poc2_evaluation_cases.py`. It includes
English and MSA pairs, five reviewed dialect recovery inputs and two
runtime-visible injection inputs. Dialects use the Arabic runtime and are
exploratory. The old S2 tool-call, missing-cart and description-injection
cases are not comparable with this preloaded-context runtime. Fixture product
prices come from the reviewed S2 data; unit cost is a synthetic half-price
assumption and is not an Odoo measurement.

## Preparation and smoke

Use the isolated stack described in [full-stack-local.md](full-stack-local.md);
do not reset or replace its engine database or reuse the #68 proposal claims.
The host wrapper verifies the dedicated project and network, then starts a
temporary proposal container with local source and data mounted read-only.
The provider key is inherited from the process environment and must not be
printed, committed, or written into the workspace `.env`.

Create the dedicated output directory and set the private provider environment
in the operator shell:

```sh
mkdir -p evals/poc2/results
set -a
. "$HOME/.config/waddih/.env"
set +a
bash scripts/poc2_evaluation.sh smoke preflight
```

Preflight checks the stack, read-only role, approved review budget and token
ceilings, reviewed case inventory, local model cache, bilingual index and
output path. It does not call the provider, load the embedding model, execute a
proposal, send a message or prove output quality.

After preflight succeeds and the owner confirms the isolated setup, run the
single approved smoke:

```sh
bash scripts/poc2_evaluation.sh smoke live
```

Inspect the Markdown and raw results at `evals/poc2/results/poc2-69-smoke/`.
Ask the owner to review the result and approve the SHA-256 of `raw.json`
before the full evaluation. A report digest is an approval binding, not a
quality score.

## Reviewed evaluation

Substitute the approved lowercase SHA-256 for `REVIEWED_SMOKE_SHA256`:

```sh
bash scripts/poc2_evaluation.sh evaluation preflight REVIEWED_SMOKE_SHA256
bash scripts/poc2_evaluation.sh evaluation live REVIEWED_SMOKE_SHA256
```

The runner verifies the digest, current prompt/schema and case inventory,
and matching settled smoke run. It reserves for the remaining evaluation
cases and four delivery examples under the same immutable USD 1 aggregate
generation-estimate ceiling. An unknown provider result retains its
reservation and blocks further evaluation. Token-count billing and actual
charges remain unknown; the estimate does not cap them.

The runner records each completed case in a partial directory before it
atomically publishes matching Markdown and JSON reports. A failure leaves the
partial directory and durable run records for review. Do not delete the partial
directory, reset the database or retry an attempted case automatically. Ask
the owner to decide how to reconcile and proceed. Output quality, language and
actual delivery need separate human review; no single evaluation proves
production readiness.
