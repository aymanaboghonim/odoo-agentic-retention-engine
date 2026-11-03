# Runbook: local S4 channel checks

This runbook verifies Mailpit locally and provides a separately approved live
Telegram test. It does not call an LLM, PostgreSQL, or Odoo. All email content
is synthetic; the Telegram command sends one fixed synthetic diagnostic to a
dedicated test chat.

## Mailpit preflight and smoke

Requirements: the pinned Docker/Compose tooling used by the repo, free loopback
ports `1025` and `8025`, and permission to access the Docker daemon.

```sh
make preflight-s4
make smoke-s4
```

Preflight checks the Docker daemon, Compose file, and local port availability.
It does not start a service or verify that the image can be pulled. The smoke
starts an isolated Compose project using the pinned Mailpit image, sends two
synthetic emails to `.invalid` recipients over loopback SMTP, verifies the
captured text and HTML through Mailpit's local API, then removes only the
project created by that run. The Arabic HTML is checked for its `dir="rtl"`
and `lang="ar"` markers. This does not prove rendering in every email client.

Mailpit's SMTP listener and UI/API are unauthenticated by default. This setup
binds ports only to `127.0.0.1`; do not change the host binding or use this
instance for real email.

## Optional Telegram inbound poller

The local adapter uses `getUpdates` polling and refuses to poll if Telegram
reports a webhook URL. It never removes or replaces a webhook. The poller
returns normalized message events through a caller-supplied API ingress
callback; durable persistence and full API/worker wiring are outside S4. Do
not point it at a bot used by another process, because Telegram's polling and
webhook delivery modes are mutually exclusive. The poller's offset is
in-memory only, so future persistent ingress must deduplicate update IDs after
restarts.

## Required live Telegram check

The live command is separate from CI and Mailpit smoke. It makes one real Bot
API `sendMessage` request through the worker-owned test dispatcher. It does not
send model-generated content and does not invoke Odoo, PostgreSQL, or an LLM.
The recipient is the `TELEGRAM_TEST_CHAT_ID` configured locally; the operator
must verify it belongs to the dedicated test chat before approving.

1. Configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_TEST_CHAT_ID` in a private local
   `.env` file. Do not commit the file or paste credentials into chat.
2. Load local variables without echoing their values:

   ```sh
   set -a
   . ./.env
   set +a
   ```

3. Confirm that the configured bot and chat are dedicated to testing, then
   invoke the one-message check:

   ```sh
   PYTHONPATH=src uv run --no-sync python scripts/s4_channels.py --confirm-dedicated-test-chat
   ```

The command fails before contacting Telegram if approval or either setting is
missing. It sends only the fixed message
`S4 channel verification: synthetic test only. No customer data.` and prints
the provider result without the token or message body. Bot API tokens are part
of HTTPS request URLs; application errors redact the URL. This manual check is
not part of `make ci`.

## Failure handling and limits

The preflight identifies missing Docker access, invalid Compose configuration,
or occupied ports before service startup. Smoke and live-check failures return
nonzero with the failing stage. The Mailpit run removes its own ephemeral
Compose project even after a smoke failure. A successful smoke validates only
the local SMTP/API contract; the Telegram check validates one authorized
message to the configured test chat. Neither proves production readiness,
durable delivery, retries, idempotency, or the production safety gate.
