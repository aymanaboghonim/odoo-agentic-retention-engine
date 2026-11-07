# Runbook: synthetic agent proposal

The proposal service is a Python integration boundary, not a worker trigger.
Issue #68 connects the trigger; #66 and #67 handle approval and execution. Only
synthetic data is allowed.

Set `PROPOSAL_SYNTHETIC_ONLY=YES`, `PROPOSAL_BUDGET_USD`,
`PROPOSAL_INPUT_TOKEN_LIMIT`, and `PROPOSAL_OUTPUT_TOKEN_LIMIT` explicitly.
`ProposalSettings.from_env` fails if these are absent or invalid. Configure a
local engine DB and the existing Odoo read-only and retrieval adapters as in
their runbooks. Initialize the engine DB migrations. The trusted trigger must
create a run with `trigger_ref` equal to `customer_id:cart_id`.

Construct `ProposalService` with the engine, `OdooReader`, `PostgresRetriever`,
`OpenAIClient`, configured `Gate`, and settings. `OpenAIClient` requires the
API key as an argument; do not print or commit it. The provider adapter uses
the default standard tier, disables stored responses and retries, does not
give the model tools, and sends only the prepared synthetic payload.

Call `generate` once for the bound run, customer, cart, language (`en` or `ar`)
and synthetic request. A valid draft is stored in `agent_proposals` with its
version, fingerprint, policy provenance and gate result; it remains in
`evaluating_policy`. Refusals, no-action responses and gate rejections do not
proceed to dispatch. The service never sends a message or creates a coupon.

`proposal_calls` stores reservations, reported token usage, conservative USD
estimates and rate provenance. Unknown usage or a transport failure retains the
reservation; do not retry an uncertain call automatically. There is no operator
reconciliation API in this issue. Inspect the failed run and provider records
before deciding how to recover. Generation estimates are not bills; the
count-only endpoint's cost is unknown (F-038). Check F-005 before a live run
and reverify pricing when stale or changed. Live evaluation and its approved
ceiling remain #69; CI uses no live provider calls.

To run the hermetic DB tests, use an isolated engine test database:

```sh
export TEST_DATABASE_URL='postgresql://engine:<password>@127.0.0.1:5433/engine'
export REQUIRE_DB_TESTS=1
uv run --no-sync pytest -q tests/test_agent_proposal.py tests/test_proposal_budget.py
```
