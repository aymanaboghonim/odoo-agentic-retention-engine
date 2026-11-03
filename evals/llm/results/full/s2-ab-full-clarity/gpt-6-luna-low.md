# S2 LLM evaluation report

- Run: `s2-ab-full-clarity` (`full`)
- Completed: `2026-11-03T11:30:48.216924+00:00`
- Candidate: `gpt-6-luna-low`
- Provider/model: `openai` / `gpt-6-luna`
- Dataset: `s2-golden-v3` (`50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`)
- Prompt: `s2-prompts-v3-tool-clarity` (`tool-clarity`)
- Prompt/schema SHA-256: `c273bcbe95cf55a3418c9c1774bf454274a3041d5b418d617a5aeea5947822d3`
- Configuration: `{"reasoning_effort": "low"}`
- Scoring: `deterministic-s2-v3`
- Inputs: 132
- Passed: 128
- Hard-gate failures: 4
- Unsupported discount-claim checks: 129/132 (all customer-facing outputs; no_action with null text is allowed)
- Tokens: input 232329, tool-use prompt input None, output including reasoning 11205, cached input 37257, reasoning (included in output) 2028
- Published-list cost estimate: $0.025482
- Provider limits: RPM 5000, TPM 2000000, concurrency 16; source `Owner reconfirmed unchanged on 2026-11-03: active Build organization tier; 5,000 RPM / 2,000,000 TPM from https://developers.openai.com/api/docs/models/gpt-6-luna; active account values are not independently verified`, checked `2026-11-03`
- Approved spend ceiling: $0.35

## Slices

| Slice | Passed | Total | Pass rate |
| --- | ---: | ---: | ---: |
| category:cart_recovery | 17 | 17 | 1.0 |
| category:cross_customer_probe | 16 | 17 | 0.9412 |
| category:customer_preference | 5 | 5 | 1.0 |
| category:deterministic_money_boundary | 16 | 18 | 0.8889 |
| category:eligibility | 17 | 17 | 1.0 |
| category:missing_data | 5 | 5 | 1.0 |
| category:odoo_fact_fidelity | 7 | 7 | 1.0 |
| category:odoo_price_fidelity | 22 | 22 | 1.0 |
| category:pii_masking | 5 | 5 | 1.0 |
| category:prompt_injection | 18 | 19 | 0.9474 |
| challenge:cross_customer | 16 | 17 | 0.9412 |
| challenge:discount_refusal | 16 | 18 | 0.8889 |
| challenge:injection | 18 | 19 | 0.9474 |
| challenge:missing_cart | 5 | 5 | 1.0 |
| challenge:multi_item | 5 | 5 | 1.0 |
| challenge:pii | 5 | 5 | 1.0 |
| challenge:price | 17 | 17 | 1.0 |
| challenge:recent_cart | 17 | 17 | 1.0 |
| challenge:recovery | 17 | 17 | 1.0 |
| challenge:stop_contact | 5 | 5 | 1.0 |
| challenge:unknown_facts | 7 | 7 | 1.0 |
| dialect:egyptian | 6 | 6 | 1.0 |
| dialect:emirati | 5 | 6 | 0.8333 |
| dialect:hijazi | 5 | 6 | 0.8333 |
| dialect:levantine | 6 | 6 | 1.0 |
| dialect:najdi | 6 | 6 | 1.0 |
| language:ar_code_switch | 12 | 12 | 1.0 |
| language:ar_egyptian | 6 | 6 | 1.0 |
| language:ar_emirati | 5 | 6 | 0.8333 |
| language:ar_hijazi | 5 | 6 | 0.8333 |
| language:ar_levantine | 6 | 6 | 1.0 |
| language:ar_msa | 59 | 60 | 0.9833 |
| language:ar_najdi | 6 | 6 | 1.0 |
| language:en | 29 | 30 | 0.9667 |
| risk:high | 82 | 86 | 0.9535 |
| risk:low | 17 | 17 | 1.0 |
| risk:medium | 29 | 29 | 1.0 |
| task:code_switch | 12 | 12 | 1.0 |
| task:english_msa | 58 | 60 | 0.9667 |
| task:msa_dialect | 58 | 60 | 0.9667 |

## Decision status

Decision status: `unresolved`. This report does not select a model. Numeric comparative-quality thresholds remain unresolved; a human must review the safety cases and language slices.

## Limitations

- All evaluation inputs and Odoo fixtures are synthetic.
- Dialect slices are exploratory and are not statistical quality claims.
- Human language-quality judgments and model-as-judge scores are advisory.
- Latency is per synchronous request under parallel load, not a production figure.
- The discount text matcher is a bounded regression gate, not a general classifier.
- Model response text is not retained; aggregate scores do not establish message quality.
- Count-only input-token endpoint billing and quota treatment are unknown; they are excluded from the generation-token cost estimate.

Machine-readable report: [gpt-6-luna-low.json](gpt-6-luna-low.json)
