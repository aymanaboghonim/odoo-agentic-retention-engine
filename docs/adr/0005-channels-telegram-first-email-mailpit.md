# ADR-0005: Telegram first behind a ChannelPort; email with Mailpit

- Status: accepted
- Date: 2026-10-24

## Context

The agent must reach customers on a common social channel, and the POC must be testable without paid APIs. WhatsApp Business needs a Meta setup, and the free test number only reaches a few approved phones (F-009).

## Decision

- Define a `ChannelPort` (send message, receive webhook, message format). First adapter: Telegram Bot API (free, real two-way chat).
- WhatsApp adapter (Meta Cloud API message shapes) comes in the MVP.
- Define an `EmailPort`. POC uses Mailpit (local SMTP catcher). The MVP picks a free-tier email service (to decide later).
- Customer-facing text supports Arabic and English.

## Consequences

- Fast, free, testable POC.
- A second adapter is real work later, but the port keeps the agent code unchanged.
