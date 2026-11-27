# Runbook: held runs and reconciliation evidence

Spec: [execution reconciliation](../specs/execution-reconciliation.md). Decision: [ADR-0030](../adr/0030-execution-reconciliation-without-resend.md).

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

## 3. Resolve

Not yet: automatic resolution and the admin `POST` resolve come in #132. Until then a held run stays `executing`; the alert keeps firing, and the evidence above tells the owner what happened. Do not change the database by hand (ADR-0030).
