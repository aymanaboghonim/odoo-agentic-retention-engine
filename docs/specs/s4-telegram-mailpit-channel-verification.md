# Spec: S4 Telegram and Mailpit channel verification

- Issue: #44
- ADRs: ADR-0012, ADR-0013, ADR-0017
- Status: agreed

## Purpose

Verify the selected POC messaging channels with a local Mailpit email path and a
bounded real Telegram test, without opening CI to external services or sending
to real users.

## Scope and non-goals

Implement minimal Telegram and SMTP channel adapters, hermetic adapter tests,
an isolated local Mailpit smoke check, and a separately invoked Telegram live
check. The live check is restricted to a dedicated test bot/chat, one fixed
synthetic message, explicit operator approval, and worker-owned dispatch.

For local POC inbound events, use Telegram `getUpdates` polling and pass
normalized updates to an API event-ingress port; do not configure a webhook.
Polling avoids requiring a public inbound endpoint for local development.
This does not implement database persistence or select production/VPS
topology. WhatsApp, real customer data, general-purpose live dispatch, and the
production safety gate are out of scope.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| S4-1 | Only the worker-owned path can dispatch a message; inbound event handling cannot dispatch a reply. | `test_only_worker_dispatches_telegram_messages`; `test_poller_forwards_normalized_events_and_advances_offset` |
| S4-2 | Live Telegram dispatch requires explicit approval and can send only one fixed synthetic message to a configured dedicated test destination. | `test_worker_dispatch_requires_approval_and_uses_fixed_message`; `test_live_cli_refuses_without_confirmation` |
| S4-3 | Bot credentials are never printed, logged, or included in reports; CI makes no Telegram request. | `test_telegram_client_uses_https_and_keeps_token_out_of_errors`; fake HTTP in channel tests; `make ci` |
| S4-4 | Mailpit receives only synthetic English/Arabic test messages and is bound to loopback for host access. | `test_mailpit_messages_are_synthetic_and_bilingual`; `test_mailpit_compose_is_pinned_and_loopback_only`; `make smoke-s4` |
| S4-5 | Telegram updates are received by polling, not webhook, in the local POC; polling never runs while a webhook is configured. | `test_poller_fails_closed_when_webhook_is_configured`; `test_poller_forwards_normalized_events_and_advances_offset` |
| S4-6 | Arabic message text remains intact through channel serialization and is checked in RTL-capable rendering/output. | `test_mailpit_verifies_text_recipient_and_arabic_rtl`; `make smoke-s4` |
| S4-7 | Preflight and smoke failures are explicit and nonzero; normal CI does not start Mailpit or require credentials. | `test_mailpit_preflight_fails_when_docker_is_missing`; `test_mailpit_preflight_rejects_ports_already_in_use`; `make ci` |

## Behavior

- **Given** an inbound Telegram update, **when** the poller receives
  it, **then** it forwards a normalized event through the API ingress port
  without running a workflow or dispatching a reply.
- **Given** a live-check command without explicit approval or a dedicated
  destination, **when** invoked, **then** preflight fails before contacting
  Telegram.
- **Given** an approved live check with valid dedicated test configuration,
  **when** invoked, **then** the worker sends only the fixed synthetic
  diagnostic message once and reports success or an explicit provider error
  without exposing credentials.
- **Given** a synthetic English or Arabic email, **when** the Mailpit smoke
  sends it over SMTP, **then** the Mailpit local interface/API exposes the
  captured message and its Arabic text remains intact. This check does not
  prove rendering in every email client.
- **Given** invalid Mailpit configuration or an unavailable service, **when**
  preflight or smoke runs, **then** it names the failing prerequisite/stage,
  exits nonzero, and does not report success.
- **Given** untrusted Telegram message text asks to bypass policy or send to
  another recipient, **when** inbound data is parsed, **then** it remains
  untrusted event content and cannot change the worker's fixed live-test
  destination or message.

## Risks

- Telegram's bot token is part of Bot API request authentication; HTTP client
  and exception logging must redact request URLs and headers.
- Polling and webhooks are mutually exclusive update-delivery modes. The local
  poller must fail clearly if a webhook is set and must not silently delete it.
- Mailpit's local SMTP and UI/API defaults are intentionally unauthenticated;
  network exposure must be limited to loopback and the isolated Compose
  network.
- The test dispatch path is not a production safety gate, durable job system,
  or evidence of production retries/idempotency.
- The API ingress port is only a channel boundary in S4; durable event storage
  and full worker/API wiring belong to the POC implementation.

## Open questions

- The dedicated Telegram test bot/chat and local credential availability are
  runtime prerequisites; no credential or chat identifier belongs in the repo.
- Durable inbound-event storage is not part of this channel-only spike.
