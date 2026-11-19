# ADR-0030: Automatic execution reconciliation without resend

- Status: accepted
- Date: 2026-11-19
- Issue: #125

## Context

The execution journal records each external effect (`coupon`, `message`) before the attempt. A crash, timeout, or missing confirmation leaves the effect `unknown` (or `started` after a crash), the run stays `executing`, and `ExecutionHeld` asks for operator reconciliation ([gated execution](../specs/gated-execution.md), EX-4). No tool exists for that today. Held runs are rare, so they should not need routine human work, and the operator should not need a CLI to see them. One such run is known on `poc68-isolated` (STATUS).

What each system can tell us:

- Odoo: the coupon code is deterministic per run (`coupon_code_for_run`), so `loyalty.card` can be searched by code through JSON-2.
- Mailpit: search supports `message-id:` (`F-082`), but the engine creates a random Message-ID and does not store it.
- Telegram: the Bot API has no call that reads back a sent message, so only the operator can confirm what the chat received.

## Decision

1. **Evidence.** A read-only check looks up each held effect (`unknown`, or `started` past a stale limit): the Odoo coupon by code, the Mailpit message by a deterministic Message-ID that is stored before the send. Telegram gives `attestation required`. A lookup error gives `unknown`, never `absent`.
2. **Automatic by default.** A worker stage runs every `RECONCILE_INTERVAL_MINUTES`. Definitive evidence (`present` or `absent`) resolves the effect without a human. An effect still without definitive evidence after `RECONCILE_DEADLINE_HOURS` (Telegram, a lasting lookup error, a partial coupon) is closed as `failed` with reason `reconciled_unverified`. Both settings are required and have no default.
3. **Admin decision through the API.** `GET` held runs (`viewer`, tokenized data only) and `POST` resolve (`admin`, with evidence or an attestation plus a reason) record the decision only. The worker applies the transition and any Odoo change (ADR-0012). The Tier 3 operator UI lists held runs and calls these endpoints; a CLI in the operator container is the fallback until then.
4. **No resend, ever.** A resolved run is terminal: `completed` when every effect is confirmed, otherwise `failed` with a `reconciled_*` reason. A new offer needs a new run through scan, gate, and approval.
5. **Orphan coupon.** If the coupon is confirmed and the message is proven `not_sent`, the worker deactivates the coupon program. If the message may have been delivered (`reconciled_unverified`), the coupon stays active.
6. **Audit and signal.** Every resolution writes an audit event with the actor (`reconciler` for the worker, the admin's identity otherwise) and the evidence. The held count and resolutions are telemetry metrics with a Grafana alert.

## Alternatives

- **Manual only (admin decides every held run):** safe, but rare cases would wait for a person, and definitive evidence would still need a click. Rejected.
- **Re-queue a `not_sent` effect for the worker:** completes the offer, but wrong evidence would send a duplicate, and the run state machine gets a backward edge. Rejected.
- **Resolve in the API process:** the API must not change Odoo (ADR-0012). Rejected; the API records, the worker acts.
- **Resolve by hand in the database:** no audit, and against the rule that state changes go through the repository. Rejected.

## Consequences

- Held runs close by themselves, with an audited reason; customers never get a second message from reconciliation.
- A message whose delivery is unknown can leave a customer without an offer; that is the safe side.
- An admin attestation can close a Telegram effect sooner; it is audited.
- Erasure still deletes held runs (privacy spec); the automatic stage makes that case rarer.
