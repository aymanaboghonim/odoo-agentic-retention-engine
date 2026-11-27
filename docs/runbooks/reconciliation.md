# Runbook: held runs and reconciliation evidence

Spec: [execution reconciliation](../specs/execution-reconciliation.md). Decision: [ADR-0030](../adr/0030-execution-reconciliation-without-resend.md).

Runbook for #131 (evidence, held-runs API) and #132 (automatic stage, admin resolve).

A run is **held** when it is `executing` and one of its effects is `unknown`, or `started` for longer than `RECONCILE_STALE_STARTED_MINUTES` (30 on AWS; a crash left it). The Grafana alert "Held effects above 0" fires for it. **Never resend or re-run a held effect**: a new offer needs a new run.

## 1. See held runs (no customer data)

- API, any `viewer` token: `GET /api/held-runs` lists the run ID, the held effects (effect, status, attempt, age in minutes, channel), and whether a Message-ID is stored. It reads the engine database only.
- Recent runs from the operator container: `python -m engine.erp_cli list [--state executing] [--limit 20]` prints run ID, state, trigger type, and created time.

## 2. Check the evidence (read-only)

In the operator container (`scripts/aws_mvp.py exec`, ADR-0027) or locally with `$DC run --rm --no-deps proposal ...`:

```bash
python -m engine.reconcile_cli check --limit 100
```

It needs the engine database URL, the Odoo JSON-2 settings, `RECONCILE_STALE_STARTED_MINUTES`, and `RECONCILE_MAILPIT_API_URL` (`http://mailpit:8025`, set in the task definition and in Compose). It prints one JSON line per held effect with an `evidence` code and changes nothing (REC-1):

| Evidence | Meaning |
| :-- | :-- |
| `present` | The coupon card exists in Odoo (archived included), or Mailpit has the exact Message-ID. |
| `absent` | Odoo has no card with the run's code, or Mailpit has no message with that exact Message-ID. |
| `unknown` | A lookup failed, the answer was malformed or not unique, more matches than one page, or no Message-ID was stored (runs before #131). Never read as `absent` (REC-3). |
| `attestation_required` | Telegram: the Bot API cannot read a sent message back; only an admin can attest. |

Mailpit's `message-id:` search matches substrings, so the check compares the returned `MessageID` exactly (measured 2026-11-27, `F-101`).

## 3. Automatic resolution (worker stage)

With `RECONCILE_STAGE=YES` (AWS; off by default in Compose) the worker runs the stage every `RECONCILE_INTERVAL_MINUTES` (10 on AWS). For each held effect it looks up the evidence (stored for `GET /held-runs`), then:

| Situation | Result |
| :-- | :-- |
| Message `present` (and the coupon confirmed when one was needed) | run `completed` |
| Message `absent`, or the coupon exists but the message never started | run `failed` (`reconciled_message_not_sent`); the coupon is then deactivated in Odoo |
| Coupon `absent` | run `failed` (`reconciled_coupon_not_created`) |
| No definitive evidence after `RECONCILE_DEADLINE_HOURS` (24 on AWS; Telegram, a lasting lookup error) | run `failed` (`reconciled_unverified`); the coupon stays active, because the customer may have the message. The alert "Held run closed unverified" fires. |

Nothing is ever resent or re-queued. Every resolution is a run event with actor `reconciler` (or the admin whose decision was applied) and the evidence, and is counted in `aorte.reconcile.resolutions`.

## 4. Admin decision (before the deadline)

An `admin` can decide sooner, mostly for Telegram, which only a person can check. The API records the decision; the worker applies it at its next stage (ADR-0012).

```bash
# Through the edge, with an admin token:
curl -sS -X POST "$BASE/api/held-runs/<run_id>/resolve" -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d '{"effect":"message","decision":"confirmed","basis":"attestation","reason":"seen in the test chat"}'
# Locally: APPROVAL_ACCESS_TOKEN=<admin token> python -m engine.approval_cli resolve \
#   --run-id <run_id> --effect message --decision confirmed --basis attestation --reason "seen in the test chat"
```

- `decision`: `confirmed` or `not_sent`; `basis`: `evidence` (the worker must find matching evidence) or `attestation` (the admin's word). An attestation never overrides definitive evidence to the contrary (REC-6); a mismatch is closed as `rejected_mismatch` and audited.
- The reason is 3 to 200 characters with no contact data. A second identical call is a no-op; a different decision while one is open is refused (409). A non-admin call is refused and audited.
- `approval_cli held` lists the held runs. The CLI fallback is in `approval_cli` (token based), not in `reconcile_cli`, so the audit actor is always a verified admin.

Do not change the database by hand (ADR-0030).
