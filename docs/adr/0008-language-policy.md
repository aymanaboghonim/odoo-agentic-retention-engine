# ADR-0008: Language policy

- Status: accepted
- Date: 2026-10-24

## Decision

- Documentation is in English. Technical and business terms stay in English.
- Arabic is used for: customer-facing UI and messages, test and seed data, and short optional summaries.
- The application must fully support Arabic: right-to-left layout, text input, formatting of numbers, dates and currency. This is not negotiable.
- POC customer messages are first tested in English and Arabic cases of the golden set. Full Arabic evaluation is part of the MVP.

## Consequences

- Arabic adds evaluation and test work. It is limited to the customer-facing scope.
