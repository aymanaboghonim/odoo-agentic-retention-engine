# Spec: Privacy, PII tokenization, consent, retention, and right-to-erasure

- Issue: #85
- Parent: [data protection and AI threat model](data-protection-threat-model.md)
- ADRs: [ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md),
  [ADR-0003](../adr/0003-llm-interface-and-model-selection.md),
  [ADR-0014](../adr/0014-odoo-19-baseline-and-seeding.md),
  [ADR-0019](../adr/0019-aws-ecs-on-ec2-cloud-runtime.md),
  [ADR-0020](../adr/0020-aws-cloudfront-s3-edge-and-backups.md)
- Status: agreed (owner, 2026-11-15)

## Purpose

Enforce `Q12` privacy and ERP-readiness controls across the engine before any cloud deployment (`#88`): zero raw customer PII at rest or in outbound LLM/telemetry payloads, fail-closed per-channel opt-in consent and Odoo blacklist checks, outbound recipient allowlist safety, configurable retention TTL sweeping, right-to-erasure cascade purge with a non-PII tombstone, and deterministic minimal Odoo 19 edge-case fixture coverage with `website_sale_loyalty` preflight verification (`F-042`).

## Scope and non-goals

Scope: reversible per-run PII tokenization (`PiiTokenizer`) and dispatch-only rehydration, fail-closed `customer_consents` + Odoo `mail_blacklist` + `AllowlistGuard`, generalized read-only Odoo 19 ERP cart scanning (`erp` mode alongside preserved `synthetic` mode), configurable `PRIVACY_RETENTION_DAYS` TTL sweeper, right-to-erasure cascade purge + hashed tombstone (`erasure_requests`), and `scripts/erp_minimal_fixtures.py` (`website_sale_loyalty` preflight + 6 minimal fixture scenarios).

Non-goals: legal jurisdiction selection for commercial launch, OIDC/Cognito operator RBAC (`#86`), production release gates/kill-switch (`#87`), and OTel/Langfuse wiring (`Tier 2`).

## Decisions and reasons

- **Zero-PII-at-Rest (`TM-2`):** LLM prompts, `agent_proposals`, `approval_requests`, `run_events`, `proposal_calls`, and logs store only opaque placeholders (`{{PII_NAME_1}}`, `{{PII_EMAIL_1}}`, `{{PII_PHONE_1}}`). `ExecutionService` rehydrates placeholders strictly from live `OdooReader` (`current_cart`) for the matching `cart.partner_id` immediately before channel dispatch and never persists rehydrated PII back to the Engine DB.
- **Fail-closed consent, Odoo blacklist, and portfolio allowlist (`Q12`):** generalized `erp` execution requires (1) an explicit `opted_in` row in Engine DB `customer_consents` for `(customer_id, channel)`, (2) `email_blacklisted is False` from Odoo `mail_blacklist`, (3) no `erasure_requests` tombstone for `customer_id`, and (4) an explicit `AllowlistGuard` match (`EXECUTION_ALLOWLIST_DOMAINS` / `EXECUTION_ALLOWLIST_EMAILS` / `TELEGRAM_ALLOWLIST_CHAT_IDS`) so real external recipients cannot be contacted accidentally.
- **Configurable retention TTL & cascade erasure with hashed tombstone:** `runs` records `customer_id` and `retention_expires_at = now + PRIVACY_RETENTION_DAYS` (`>= 1`). Expired terminal runs are cascade-deleted across all run child tables. Right-to-erasure cascade-deletes all `runs`, child records, `gate_coupon_reservations`, and `customer_consents` for `customer_id`, then writes a non-PII `HMAC-SHA256(pepper, str(customer_id))` tombstone to `erasure_requests` that blocks future ERP re-enrollment until explicit fresh consent.
- **Preserved POC compatibility:** `synthetic` scan/execution paths remain intact alongside `erp` mode so all POC-2 Compose and evaluation contracts continue to pass.
- **Minimal Odoo 19 fixture suite & module preflight:** keep `ADR-0014` demo baseline and add API-only deterministic fixtures covering all 6 retention edge cases plus a read-only `ir_module_module` check confirming `website_sale_loyalty` is `installed` before positive-discount ERP runs (`F-042`).

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| TM-1 | Outbound LLM payloads contain no raw customer names, emails, phones, or non-allowlisted identifiers. | `test_outbound_payloads_contain_only_synthetic_data` |
| TM-2 | Customer names, emails, and phones are replaced by opaque tokens before LLM calls and restored only from live Odoo context at dispatch; Engine DB never stores raw contact PII. | `test_pii_is_masked_before_llm_call`, `test_dispatch_rehydrates_only_at_send_without_persisting_pii` |
| TM-3 | Customer context lookup in proposal generation is strictly bound to the run's `customer_id` and `cart_id`, with zero SQL write or free-form SQL tool access. | `test_customer_query_is_bound_to_authenticated_customer` |
| TM-4 | Cross-customer cart context or PII rehydration tokens belonging to another customer fail closed. | `test_cross_customer_data_request_is_refused` |
| TM-5 | Repeated cart triggers within cooldown/active state create at most one run and never repeat a coupon or message. | `test_duplicate_trigger_has_no_repeated_side_effect` |
| TM-6 | Proposal/execution step bounds fail closed into an explicit terminal state without side effects. | `test_run_stops_at_step_limit` |
| TM-7 | Untrusted customer or cart text cannot change policy, caps, destination, or tools. | `test_untrusted_text_cannot_change_policy` |
| TM-8 | System instructions stay strictly separated from untrusted customer/cart payload messages. | `test_system_and_user_content_are_separate_messages` |
| PR-1 | Generalized `erp` scan and dispatch refuse customers without an explicit `opted_in` row or present on Odoo `mail_blacklist`. | `test_erp_scan_and_dispatch_require_opt_in_and_non_blacklisted` |
| PR-2 | Outbound `AllowlistGuard` fails closed for unapproved email domains/addresses or Telegram chat IDs. | `test_allowlist_guard_blocks_unapproved_recipients` |
| PR-3 | Expired terminal runs (`retention_expires_at <= now`) are cascade-deleted across all Engine DB child tables without removing active non-expired runs. | `test_retention_ttl_sweeper_cascades_expired_runs_only` |
| PR-4 | Right-to-erasure cascade-deletes all customer runs, child tables, and consents, and writes a hashed tombstone that blocks repeat ERP scans. | `test_right_to_erasure_purges_all_customer_rows_and_blocks_rescan` |
| PR-5 | Generalized `erp` scanner suppresses customers already in an active run or within `CAMPAIGN_COOLDOWN_DAYS`, and skips missing-email or recent carts. | `test_erp_scanner_filters_and_suppresses_duplicates` |
| PR-6 | Positive-discount ERP readiness preflight fails closed when `website_sale_loyalty` is not `installed` (`F-042`). | `test_loyalty_module_readiness_preflight` |
| PR-7 | `scripts/erp_minimal_fixtures.py` exercises all 6 minimal deterministic edge-case scenarios via Odoo API only (zero SQL writes) and preserves run governance (`--preflight-only`). | `test_minimal_fixtures_cover_six_scenarios_via_api_only` |

## Behavior

- **Given** an eligible Odoo 19 abandoned cart with `partner_name`, `partner_email`, and `partner_phone`, **when** `ProposalService.generate` runs, **then** raw identifiers are tokenized to `{{PII_NAME_1}}`, `{{PII_EMAIL_1}}`, and `{{PII_PHONE_1}}` before LLM calls and persisted as tokens only; `ExecutionService` rehydrates `{{PII_*}}` from `current_cart` immediately before channel dispatch.
- **Given** a customer without `opted_in` consent, on Odoo `mail_blacklist`, with an `erasure_requests` tombstone, or targeting a destination outside `AllowlistGuard`, **when** scanning or executing in `erp` mode, **then** the engine skips or rejects fail-closed with zero external side effects.
- **Given** an erasure request for `customer_id`, **when** `RetentionService.erase_customer` runs, **then** all Engine DB rows for that customer are cascade-deleted atomically and a salted `HMAC-SHA256` tombstone prevents re-enrollment until explicit fresh consent.

## Risks

- Regex/word-boundary name scrubbing can miss short (`< 3` char) nicknames or unusual transliterations; fail-closed `CONTACT` scanning mitigates email/phone leaks, and Arabic + English test samples validate multi-script tokenization.
- Rehydration relies on live `OdooReader` (`current_cart`); if Odoo is unreachable at dispatch time, execution fails closed without sending raw tokens (`{{PII_NAME_1}}`) to customers.

## Implementation notes (#85)

Code: [privacy.py](../../src/engine/privacy.py), [privacy_cli.py](../../src/engine/privacy_cli.py), migration [0011](../../src/engine/migrations/versions/0011_privacy_consent_retention.py), ERP paths in [execution.py](../../src/engine/execution.py), [synthetic_scan.py](../../src/engine/synthetic_scan.py), [odoo.py](../../src/engine/odoo.py), fixtures in [erp_minimal_fixtures.py](../../scripts/erp_minimal_fixtures.py). Invariant tests: [test_privacy_consent_retention.py](../../tests/test_privacy_consent_retention.py). Runbook: [privacy-local.md](../runbooks/privacy-local.md).

Settings. All ERP settings have no default and fail closed when missing:

| Setting | Meaning |
| :-- | :-- |
| `SCAN_MODE` | `synthetic` (default, POC/Compose) or `erp` |
| `PROPOSAL_ERP_MODE`, `EXECUTION_ERP_MODE` | `YES` enables ERP runs in the proposal and execution steps |
| `PRIVACY_RETENTION_DAYS` | integer `>= 1`; sets `runs.retention_expires_at` |
| `PRIVACY_TOMBSTONE_PEPPER` | HMAC-SHA256 pepper, at least 32 characters; a secret |
| `CAMPAIGN_COOLDOWN_DAYS` | integer `>= 1`; any run for the customer inside it suppresses a new run |
| `ERP_SCAN_CHANNEL` | consent channel the scan requires: `email` or `telegram` |
| `EXECUTION_ALLOWLIST_DOMAINS`, `EXECUTION_ALLOWLIST_EMAILS`, `TELEGRAM_ALLOWLIST_CHAT_IDS` | comma lists; exact match only, no wildcards or subdomains; empty blocks everything |

How it works:

- Tokens: `{{PII_NAME_1}}` is the full name, `{{PII_NAME_2..}}` are name parts of 3 or more characters, plus `{{PII_EMAIL_1}}` and `{{PII_PHONE_1}}`. The customer's email and phone are masked first, then the contact-pattern check runs, then names are masked. This keeps a name part (for example a domain word) from hiding another address. Token-like text in untrusted input becomes `[token]`. The proposal stores `pii_tokens` (token to field reference, no values). Model output with an unknown token, a raw identity, or a contact pattern fails.
- Rehydration happens twice. A check before the claim proves the tokens resolve (the text is discarded). The real rehydration runs from the live cart reloaded just before the message effect. Pre-#85 proposals without `pii_tokens` dispatch only if they contain no token-like text.
- ERP recipient: email goes to the live Odoo `partner_email`, which must pass the allowlist. Odoo has no customer Telegram field, so ERP Telegram goes only to an operator-configured chat that must also be allowlisted. The destination hash in the execution binding is an HMAC with the pepper, not a plain hash of the address. Consent, tombstone, blacklist, and recipient are checked again before the coupon and before the message.
- Consent channel names: `email` (execution channel `mailpit`) and `telegram`.
- Erasure removes Engine DB rows only. It also deletes runs in `executing` state (including held `unknown` effects). Reconcile those in Odoo before erasing. Erasing or anonymizing the Odoo partner is an Odoo operator task, outside the engine.
- The retention sweep deletes in bounded batches with `SKIP LOCKED`. Runs without `retention_expires_at` (all pre-#85 runs and synthetic runs) are never swept; erasure still finds them by `trigger_ref`.

ERP execution wiring: the worker runs `SCAN_MODE=erp`, then [erp_cli.py](../../src/engine/erp_cli.py) runs `propose` (a `fixed` token template at zero LLM cost, or `model`), then `show` (a tokenized proposal and a privacy preview that print only the recipient domain and HMAC destination hash), then `execute`. `execute` needs `--confirm-run-id` and `--confirm-destination` to match exactly. Inside Compose, email goes to the private `mailpit` service: `MailpitEmailAdapter` accepts a dot-free service name only with `allow_private_service=True`, and only if it resolves to private, non-loopback addresses.

Live verification on the isolated `poc68-isolated` stack (2026-11-15):

- The fixtures created all six scenarios through JSON-2. A re-run reused them (6 partners, 7 orders, 1 blacklist entry).
- The Engine DB was migrated to `0011`, and opt-in was recorded for the four consented fixtures.
- One `erp` scan of 22 carts created 2 runs (`eligible`, first `duplicate` cart). It skipped `missing_email` 1, `blacklisted` 1, `recent` 1, `no_consent` 16, and `active_run` 1. A re-scan created 0.
- A fixed proposal, privacy preview, and `execute` delivered one Mailpit message to the fixture's live Odoo address, with the name rehydrated. A row scan of the run found 0 Engine DB rows with the raw name or email.

Not verified live: a `model` ERP proposal (no LLM call was made), Telegram ERP dispatch, and a positive-discount ERP coupon.
