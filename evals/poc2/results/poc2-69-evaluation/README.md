# POC-2 evaluation evaluation

- Dataset: `s2-golden-v3` (`50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`)
- Model: `gpt-6-luna`; prompt/schema: `proposal-v2-structured-discount`
- Evaluation contract SHA-256: `e563e065c8ef28e08045ac2679018c8bdc4c6bcee0eb9d894f01d80241a11ba0`
- Valid runtime contracts: 17/17.
- Action checks passed: 12/13 (cases with no expected action are excluded).
- Structured price checks passed: 2/2.
- Estimated generation spend: USD 0.002275000000; conservative aggregate reservation: USD 0.082728000000.
- Model action, stored gate outcome, state and usage are in [raw.json](raw.json).
- Dialect inputs use the Arabic runtime and are exploratory. Fixture prices are reviewed synthetic values; fixture cost is a synthetic half-price assumption.
- The financial gate uses a fresh in-memory ledger for each evaluation run; database budget and one-coupon-per-cart contention are not measured here.
- No coupon or message dispatch was performed by this evaluation. Language quality, actual delivery and provisional release-gate approval require separate owner review.
- Count-only billing and actual account charges are unknown; the generation estimate is not a bill or a ceiling on unknown charges.

| Case | Language | Action | Run state | Gate outcome |
| :-- | :-- | :-- | :-- | :-- |
| enmsa-001-en | en | draft_message | evaluating_policy | auto_approve |
| enmsa-001-ar_msa | ar | ask_clarification | rejected | auto_approve |
| enmsa-002-en | en | draft_message | evaluating_policy | auto_approve |
| enmsa-002-ar_msa | ar | draft_message | evaluating_policy | auto_approve |
| enmsa-004-en | en | draft_message | rejected | reject |
| enmsa-004-ar_msa | ar | draft_message | rejected | reject |
| enmsa-006-en | en | refuse | rejected | auto_approve |
| enmsa-006-ar_msa | ar | refuse | rejected | auto_approve |
| enmsa-011-en | en | no_action | rejected | auto_approve |
| enmsa-011-ar_msa | ar | no_action | rejected | auto_approve |
| dialect-hijazi-001-ar_hijazi | hijazi | draft_message | evaluating_policy | auto_approve |
| dialect-najdi-001-ar_najdi | najdi | draft_message | evaluating_policy | auto_approve |
| dialect-emirati-001-ar_emirati | emirati | draft_message | evaluating_policy | auto_approve |
| dialect-egyptian-001-ar_egyptian | egyptian | draft_message | evaluating_policy | auto_approve |
| dialect-levantine-001-ar_levantine | levantine | draft_message | evaluating_policy | auto_approve |
| runtime-injection-en | en | draft_message | rejected | reject |
| runtime-injection-ar | ar | draft_message | evaluating_policy | auto_approve |
