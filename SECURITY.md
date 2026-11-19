# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**. Do not open a public issue for a security problem.

Include what you found, how to reproduce it, and the impact you expect. Reports are handled on a best-effort basis by the maintainer; there is no bug bounty.

## Scope

- The code, workflows, and Terraform in this repository.
- The AWS MVP deployment described in [docs/runbooks/aws-mvp.md](docs/runbooks/aws-mvp.md). It is a portfolio demo that is usually stopped.

All data in this project is synthetic. No real customer data is stored or processed.

## What is already in place

- Secret scanning of the full git history runs in CI (`scripts/scan_secrets.sh`).
- Discount and margin limits are enforced in code, never by the model ([safety gate spec](docs/specs/safety-gate.md)).
- Customer data is tokenized before any LLM call ([privacy spec](docs/specs/privacy-consent-retention.md)).
- Infrastructure changes go through a saved plan, a policy check, and owner review ([deployment spec](docs/specs/aws-mvp-deployment.md)).
