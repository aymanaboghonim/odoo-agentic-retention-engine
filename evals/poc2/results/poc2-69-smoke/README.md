# POC-2 smoke evaluation

- Dataset: `s2-golden-v3` (`50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`)
- Model: `gpt-6-luna`; prompt/schema: `proposal-v2-structured-discount`
- Evaluation contract SHA-256: `e563e065c8ef28e08045ac2679018c8bdc4c6bcee0eb9d894f01d80241a11ba0`
- Valid runtime contracts: 1/1.
- Action checks passed: 1/1 (cases with no expected action are excluded).
- Structured price checks passed: 0/0.
- Estimated generation spend: USD 0.000093250000; conservative aggregate reservation: USD 0.004596000000.
- Model action, stored gate outcome, state and usage are in [raw.json](raw.json).
- Dialect inputs use the Arabic runtime and are exploratory. Fixture prices are reviewed synthetic values; fixture cost is a synthetic half-price assumption.
- The financial gate uses a fresh in-memory ledger for each evaluation run; database budget and one-coupon-per-cart contention are not measured here.
- No coupon or message dispatch was performed by this evaluation. Language quality, actual delivery and provisional release-gate approval require separate owner review.
- Count-only billing and actual account charges are unknown; the generation estimate is not a bill or a ceiling on unknown charges.

| Case | Language | Action | Run state | Gate outcome |
| :-- | :-- | :-- | :-- | :-- |
| enmsa-001-en | en | draft_message | evaluating_policy | auto_approve |
