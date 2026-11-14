# Runbook: local static POC walkthrough demo

- Issue: #81
- Artifact: [docs/demo/poc-walkthrough.html](../demo/poc-walkthrough.html)
- Generator & CLI: `scripts/poc3_local_demo.py`
- Status: **SYNTHETIC DEMO ARTIFACT ONLY — NOT PRODUCTION HOSTED**

## Purpose

Provide stakeholders and reviewers with an offline, zero-dependency walkthrough of the five stages proven in POC-2 without starting Docker, calling a live LLM, or hosting any web service:

1. **Abandoned cart scan** (`retention-review-*@example.test`)
2. **Policy retrieval & structured proposal** (English LTR and MSA Arabic RTL)
3. **Deterministic financial safety gate** (`auto_approve`, `needs_approval`, `reject`)
4. **Operator approval binding** (`snapshot_hash` and `gate_fingerprint`)
5. **Worker execution & channel dispatch record** (Mailpit, Telegram, or blocked; zero coupons on 0% or rejected offers)

## View in a browser (zero setup)

Open `docs/demo/poc-walkthrough.html` directly from the local filesystem (`file:///.../docs/demo/poc-walkthrough.html`). No web server, Node runtime, external CDN, or network access is required.

## Run from the terminal

Print all three synthetic scenarios in the terminal:

```sh
PYTHONPATH=src uv run --no-sync python scripts/poc3_local_demo.py
```

Inspect one scenario or export deterministic JSON:

```sh
PYTHONPATH=src uv run --no-sync python scripts/poc3_local_demo.py --scenario ar-human-approval-12pct
PYTHONPATH=src uv run --no-sync python scripts/poc3_local_demo.py --json
```

Regenerate or verify byte-for-byte parity of the committed HTML file:

```sh
PYTHONPATH=src uv run --no-sync python scripts/poc3_local_demo.py --write-html docs/demo/poc-walkthrough.html
PYTHONPATH=src uv run --no-sync python scripts/poc3_local_demo.py --check-html docs/demo/poc-walkthrough.html
```

## Boundaries

- All records are synthetic `.test` fixtures; no real customer PII or live Odoo instance is touched.
- Gate outcomes, margins, discount amounts, `gate_fingerprint`, `snapshot_hash`, and `RET-...` coupon codes are computed by the real deterministic engine modules (`engine.policy`, `engine.approval_data`, `engine.coupon_code`).
- This walkthrough is a static stakeholder review artifact only and must not be deployed or hosted as a production operator UI (`docs/architecture/poc-exit-review.md`).
