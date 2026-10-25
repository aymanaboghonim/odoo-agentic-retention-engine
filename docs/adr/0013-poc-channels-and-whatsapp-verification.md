# ADR-0013: POC channels and WhatsApp verification

- Status: accepted
- Date: 2026-10-25

## Context

The POC needs a social channel for testing. ADR-0005 selected Telegram for the POC, but its rationale relied in part on an unverified claim about the Meta test-recipient limit (F-009). WhatsApp is outside the POC scope. Telegram's documented no-cost default and broadcast limits are recorded in F-022.

## Decision

- Keep Telegram behind the `ChannelPort` as the POC social channel, and use Mailpit for POC email.
- WhatsApp is not part of the POC. It remains planned for the MVP, subject to verifying the current setup requirements and test-recipient limit before implementation. F-009 remains unverified; no decision relies on the claimed maximum.

## Alternatives

- Use WhatsApp in the POC: not selected because WhatsApp setup and the test-recipient limit have not been verified, while WhatsApp is outside the POC scope.

## Consequences

- The POC can continue with Telegram and local email testing without making WhatsApp's unknown test-recipient limit a planning fact.
- Before implementing WhatsApp for the MVP, verify its setup requirements, recipient limit, and applicable costs from current official sources.
- Telegram throughput and any paid-broadcast eligibility or charges must be respected if the POC use case exceeds the standard free broadcast allowance.
