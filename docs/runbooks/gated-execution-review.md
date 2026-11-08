# Runbook: gated synthetic execution review

This runbook is for issue #67 only. It creates a synthetic cart through the
supported Odoo JSON-2 API, prepares a fixed or live-model proposal, and
dispatches only after interactive operator review. Do not point it at a real or
shared Odoo instance. Do not bypass the loopback host checks.

Load the ignored workspace configuration in the operator terminal and select
the disposable Odoo API key:

```sh
set -a; . ./.env; set +a
export ODOO_API_KEY="$DISPOSABLE_ODOO_API_KEY"
```

For model proposals, load `OPENAI_API_KEY` from the owner's private local
provider environment in that terminal. Do not copy it into the workspace
`.env`, print it, or commit it.

## Required configuration

Set these in the current terminal without committing or printing secrets:

- Local `ENGINE_DATABASE_URL`; initialize the engine migrations first.
- `GATE_TIMEZONE`, `GATE_CURRENCY`, `GATE_DAILY_BUDGET`, and
  `GATE_CATEGORY_MIN_MARGINS`.
- `ODOO_URL`, `ODOO_DB_NAME`, `ODOO_API_KEY`, and
  `ODOO_READONLY_DATABASE_URL`; Odoo must be the isolated local development
  instance.
- `EXECUTION_SYNTHETIC_ONLY=YES`.
- For Mailpit, optional `MAILPIT_SMTP_HOST` (default `127.0.0.1`) and
  `MAILPIT_SMTP_PORT` (default `1025`).
- For Telegram, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_TEST_CHAT_ID`. The chat must
  be dedicated to synthetic review. The script asks the operator to confirm it
  for each send.

For this review only, the owner approved a USD 450 daily discount budget and
0.20 minimum margin for `Furniture / Office`. The costed `Customizable Desk`
demo product is currently USD 750 with USD 500 unit cost, so four 15% synthetic
discounts fit the daily budget. These values are not production defaults. The
separate aggregate model-generation ceiling is USD 0.50.

For live-model proposals, also set `OPENAI_API_KEY`,
`PROPOSAL_SYNTHETIC_ONLY=YES`, `PROPOSAL_BUDGET_USD`,
`PROPOSAL_INPUT_TOKEN_LIMIT`, `PROPOSAL_OUTPUT_TOKEN_LIMIT`,
`PROPOSAL_REVIEW_ID=poc2-67-live-review`, and
`PROPOSAL_REVIEW_BUDGET_USD=0.50`. The script rejects a different review ID,
aggregate ceiling, or per-run budget above the ceiling. Check the current
provider rate record in [facts-register.md](../facts-register.md) before live
calls. The pinned English and Arabic policy passages must already be indexed
in the local engine database. The embedding model is loaded from its local
pinned snapshot only.

## Preflight and synthetic cart

Run a fast preflight before any write or provider call. Preflight checks local
engine schema/configuration, Odoo configuration, selected channel readiness,
and optionally cart eligibility. It does not send, call the LLM, load embedding
models, remotely verify Telegram credentials, or establish retrieval quality.

For review, start loopback-only Mailpit after its documented preflight and keep
it running while inspecting messages:

```sh
make preflight-s4
docker compose -p gated-review -f deploy/s4-mailpit-compose.yml up -d --wait mailpit
```

Open the Mailpit UI at `http://127.0.0.1:8025`. Stop only this review instance
after the owner has finished inspecting its messages.

```sh
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py preflight --channel mailpit
```

For Telegram, use `--channel telegram`; for model runs add `--mode model`.
Create one disposable synthetic cart only after preflight:

```sh
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py create-cart
```

The command is interactive and requires the exact phrase
`CREATE SYNTHETIC ODOO CART`. It creates a synthetic partner and draft sale
order through Odoo JSON-2, then confirms eligibility through the read-only
adapter. It selects a costed `Customizable Desk` demo product and prints its
category, price, and cost so the gate can evaluate positive discounts. The
disposable Odoo currently has two websites; the CLI selects and prints the
lowest-ID site before asking for the exact confirmation phrase. Preserve the
printed synthetic reference if Odoo returns an ambiguous create result; inspect
Odoo before attempting setup again.

After creating a cart, run model preflight for that cart before any provider
call:

```sh
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py preflight --channel mailpit --mode model --cart-id CART_ID
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py preflight --channel telegram --mode model --cart-id CART_ID
```

## Prepare, inspect, approve, and execute

Each proposal is bound to exactly one language and channel before approval.
Create a separate synthetic cart for every example. This avoids the gate's
one-coupon-per-customer/cart/day rule from making later discount examples
ineligible. Run each example through `prepare`, inspect it, and execute it
before moving to the next:

```sh
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py prepare --cart-id FIXED_MAIL_EN_CART --channel mailpit --language en --mode fixed
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py prepare --cart-id FIXED_MAIL_AR_CART --channel mailpit --language ar --mode fixed
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py prepare --cart-id FIXED_TELEGRAM_EN_CART --channel telegram --language en --mode fixed
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py prepare --cart-id FIXED_TELEGRAM_AR_CART --channel telegram --language ar --mode fixed
```

Repeat with new cart IDs and `--mode model` for the live examples, after model
preflight. Every model generation uses the same durable aggregate review
budget. If the gate
requires approval, inspect the stored text and gate result before approving:

```sh
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py show --run-id RUN_ID
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py approve --run-id RUN_ID
```

Approval requires an interactive terminal and the exact run-specific phrase.
Before each send, `execute` displays the approved message and exact destination,
then requires a run-specific send phrase and a separate explicit confirmation
that the destination is dedicated to synthetic review:

```sh
PYTHONPATH=src uv run --no-sync python scripts/gated_execution_review.py execute --run-id RUN_ID --channel mailpit
```

Use `--channel telegram` for Telegram. Do not continue unless the displayed
chat is the dedicated test chat. The script never changes a proposal's channel
at execution time. Inspect each received message personally in the selected
channel before proceeding to the next example. A provider response confirms
submission only; it is not proof of receipt or content quality.

## Recovery and limits

The execution journal holds unknown or interrupted effects and never retries
them automatically. Do not rerun `execute` after an unknown result. Inspect the
run's effect journal and external system, then reconcile manually. A confirmed
coupon followed by a failed send remains partial; the code does not claim
transactional delivery. Fixed proposals use zero discount and create no coupon.
Live model proposals may propose a structured discount; the deterministic gate
and human approval remain authoritative.

The CLI reports stage and elapsed time, and emits a 30-second heartbeat during
long stages. It does not prove prompt quality, production consent, delivery
reliability, or suitability for production.
