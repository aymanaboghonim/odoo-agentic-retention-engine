# Odoo Agentic Retention Engine

An agentic AI layer added on top of an existing e-commerce/ERP system (Odoo), to fix real pain points without rebuilding the core system.

First slice (POC): an abandoned cart is found, an agent writes a personal, policy-grounded message, and it is sent by Telegram and email. Discounts are limited by hard rules in code, not by the model.

## Status

POC-2 walking skeleton and bilingual evaluation are complete; Phase 3 (POC exit review and local stakeholder demo) is in progress. See [docs/STATUS.md](docs/STATUS.md) for the live checklist and [docs/architecture/poc-exit-review.md](docs/architecture/poc-exit-review.md) for the consolidated POC exit signoff memo and MVP handoff plan.

## Where to read

| What | Where |
| :-- | :-- |
| Vision, goals, scope | [docs/00-vision.md](docs/00-vision.md) |
| Live progress and open questions | [docs/STATUS.md](docs/STATUS.md) |
| POC exit review and MVP handoff | [docs/architecture/poc-exit-review.md](docs/architecture/poc-exit-review.md) |
| Local static demo walkthrough (demo artifact only) | [docs/runbooks/local-demo-walkthrough.md](docs/runbooks/local-demo-walkthrough.md) |
| How we work (process, traceability, branching) | [docs/process.md](docs/process.md) |
| Architecture decisions | [docs/adr/](docs/adr/) |
| Volatile facts to verify (prices, limits, versions) | [docs/facts-register.md](docs/facts-register.md) |

## Principles

- If it is not written in a file in this repo, it does not exist.
- Rules are enforced by checks, not by promises.
- Every change traces from issue to PR to commit to release.
- All data is synthetic.

## License

Apache-2.0. See [LICENSE](LICENSE).

## Acknowledgements

See [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).
