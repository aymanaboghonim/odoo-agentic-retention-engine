# Runbook: Q12 privacy operations (local)

Spec: [privacy-consent-retention.md](../specs/privacy-consent-retention.md). Issue #85.

All commands work on the Engine DB only. Output shows stages and counts, never names, emails, phones, or the pepper. Each command runs a preflight first (settings, DB access, schema `0011`). `--preflight-only` stops after it with no change.

## Settings

```bash
export ENGINE_DATABASE_URL=postgresql://engine:<ENGINE_DB_PASSWORD>@127.0.0.1:5433/engine
export PRIVACY_RETENTION_DAYS=30            # integer >= 1
export PRIVACY_TOMBSTONE_PEPPER=<secret>    # at least 32 characters; never commit it
```

Changing the pepper makes old tombstones unmatchable. Keep it stable for the life of the Engine DB.

## Consent

```bash
PYTHONPATH=src uv run --no-sync python -m engine.privacy_cli consent grant \
  --customer-id <odoo_partner_id> --channel email --source <evidence-ref>
PYTHONPATH=src uv run --no-sync python -m engine.privacy_cli consent revoke \
  --customer-id <odoo_partner_id> --channel email --source <evidence-ref>
PYTHONPATH=src uv run --no-sync python -m engine.privacy_cli consent show \
  --customer-id <odoo_partner_id> --channel email
```

`--source` is a short non-contact reference (a ticket ID, for example). Granting consent to an erased customer fails unless you add `--fresh-after-erasure`, which records explicit fresh consent and lifts the tombstone block.

## Retention sweep

```bash
PYTHONPATH=src uv run --no-sync python -m engine.privacy_cli sweep --batch-size 500 --max-batches 100
```

The sweep deletes only terminal runs whose `retention_expires_at` has passed, plus all their child rows, in bounded batches. Active runs are never touched.

## Right to erasure

```bash
PYTHONPATH=src uv run --no-sync python -m engine.privacy_cli --preflight-only erase --customer-id <id>
PYTHONPATH=src uv run --no-sync python -m engine.privacy_cli erase --customer-id <id> --confirm-erasure
```

Erasure cannot be undone. It deletes every run for the customer, including runs still `executing` with a held effect, so reconcile those in Odoo first. It also deletes their child rows, coupon reservations, and consents, then writes an `HMAC-SHA256(pepper, customer_id)` tombstone. Erasing the partner in Odoo is a separate Odoo operator task.

## Minimal ERP fixtures (isolated Compose Odoo only)

`scripts/erp_minimal_fixtures.py` runs inside the isolated full-stack Compose network (`ODOO_URL=http://odoo:8069`). It uses the Odoo JSON-2 API only. Run the preflight first. It checks the read-only role, `website_sale_loyalty` installed (F-042), and product and website lookups, and it makes no write:

```bash
python -m scripts.erp_minimal_fixtures --preflight-only
python -m scripts.erp_minimal_fixtures --confirmed-synthetic
```

It creates six scenarios (`eligible`, `missing-email`, `blacklisted`, `recent`, `no-consent`, `duplicate`) and prints the consent commands for the four consented ones. Re-runs reuse the same records. This has not yet been run live (see the spec notes).
