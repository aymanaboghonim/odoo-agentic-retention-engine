# S2 LLM evaluation report

- Run: `s2-poc2-56-rerun-001` (`full`)
- Completed: `2026-11-05T15:31:08.077844+00:00`
- Candidate: `gpt-6-luna-low`
- Provider/model: `openai` / `gpt-6-luna`
- Dataset: `s2-golden-v3` (`50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`)
- Prompt: `s2-prompts-v3-tool-clarity` (`tool-clarity`)
- Prompt/schema SHA-256: `c273bcbe95cf55a3418c9c1774bf454274a3041d5b418d617a5aeea5947822d3`
- Configuration: `{"reasoning_effort": "low"}`
- Scoring: `deterministic-s2-v3`
- Inputs: 132
- Passed: 127
- Hard-gate failures: 5
- Unsupported discount-claim checks: 129/132 (all customer-facing outputs; no_action with null text is allowed)
- Tokens: input 232379, tool-use prompt input None, output including reasoning 11760, cached input 35185, reasoning (included in output) 2530
- Published-list cost estimate: $0.025951
- Provider limits: RPM 5000, TPM 2000000, concurrency 16; source `Owner-reported OpenAI Build tier limits (STATUS Q16), reconfirmed 2026-11-03; #56 re-run ceiling approved 2026-11-05`, checked `2026-11-03`
- Approved spend ceiling: $0.50

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
| category:odoo_price_fidelity | 20 | 22 | 0.9091 |
| category:pii_masking | 5 | 5 | 1.0 |
| category:prompt_injection | 19 | 19 | 1.0 |
| challenge:cross_customer | 16 | 17 | 0.9412 |
| challenge:discount_refusal | 16 | 18 | 0.8889 |
| challenge:injection | 19 | 19 | 1.0 |
| challenge:missing_cart | 5 | 5 | 1.0 |
| challenge:multi_item | 3 | 5 | 0.6 |
| challenge:pii | 5 | 5 | 1.0 |
| challenge:price | 17 | 17 | 1.0 |
| challenge:recent_cart | 17 | 17 | 1.0 |
| challenge:recovery | 17 | 17 | 1.0 |
| challenge:stop_contact | 5 | 5 | 1.0 |
| challenge:unknown_facts | 7 | 7 | 1.0 |
| dialect:egyptian | 6 | 6 | 1.0 |
| dialect:emirati | 5 | 6 | 0.8333 |
| dialect:hijazi | 6 | 6 | 1.0 |
| dialect:levantine | 6 | 6 | 1.0 |
| dialect:najdi | 6 | 6 | 1.0 |
| language:ar_code_switch | 11 | 12 | 0.9167 |
| language:ar_egyptian | 6 | 6 | 1.0 |
| language:ar_emirati | 5 | 6 | 0.8333 |
| language:ar_hijazi | 6 | 6 | 1.0 |
| language:ar_levantine | 6 | 6 | 1.0 |
| language:ar_msa | 58 | 60 | 0.9667 |
| language:ar_najdi | 6 | 6 | 1.0 |
| language:en | 29 | 30 | 0.9667 |
| risk:high | 81 | 86 | 0.9419 |
| risk:low | 17 | 17 | 1.0 |
| risk:medium | 29 | 29 | 1.0 |
| task:code_switch | 11 | 12 | 0.9167 |
| task:english_msa | 57 | 60 | 0.95 |
| task:msa_dialect | 59 | 60 | 0.9833 |

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
