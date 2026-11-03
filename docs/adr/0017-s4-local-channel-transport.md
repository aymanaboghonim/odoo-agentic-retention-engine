# ADR-0017: S4 local Telegram polling and Mailpit transport

- Status: accepted
- Date: 2026-11-03

## Context

ADR-0013 selects Telegram and Mailpit for the POC but leaves the local
transport undecided. The channel spike must be testable locally without a
public inbound endpoint, and the POC uses synthetic data only. Telegram's
official docs describe `getUpdates` long polling and webhooks as mutually
exclusive; webhooks require a valid SSL certificate and supported ports
([F-039](../facts-register.md#f-039)). Mailpit's documented default SMTP and
HTTP ports and the pinned image are recorded in [F-040](../facts-register.md#f-040).

## Decision

- Use Telegram `getUpdates` polling for local POC inbound events. Check
  `getWebhookInfo` first and fail if a webhook is set; never silently delete or
  replace it. Pass normalized updates to the API event-ingress port. Durable
  event storage is outside S4.
- Send Telegram messages only through the worker-owned dispatch path. A
  separate manual test command may send one fixed synthetic diagnostic message
  only after explicit operator confirmation of the dedicated test bot/chat.
- Use Mailpit SMTP from the local engine at `127.0.0.1:1025`. Its local UI/API
  is at `http://127.0.0.1:8025`. Bind both host ports to loopback; use synthetic
  addresses and messages. Mailpit is not a production mail relay.
- Pin the Mailpit container to the verified v1.33.0 multi-platform image
  digest in F-040.

These are local POC choices only. They do not decide production authentication,
network boundaries, webhook topology, or email provider.

## Alternatives

- **Telegram webhook for local POC:** not selected because the local setup
  should not require a public HTTPS endpoint. It remains a possible deployment
  option after separate review.
- **Unpinned Mailpit image or externally bound ports:** rejected to keep the
  local test reproducible and prevent unintended network exposure.

## Consequences

- Local inbound testing requires no public listener, but polling is exclusive
  with an existing webhook. The in-memory poller offset is not durable; the
  future API ingress must deduplicate update IDs across restarts.
- Mailpit's default local services are unauthenticated and SMTP is unencrypted;
  loopback binding and synthetic-only messages are mandatory.
- This ADR does not create an LLM, Odoo, or PostgreSQL integration, nor does the
  manual test path implement the production safety gate.
