# S2 LLM evaluation report

- Run: `s2-full-001` (`full`)
- Completed: `2026-11-03T06:45:11.610117+00:00`
- Candidate: `gemini-3.8-flash-low`
- Provider/model: `google` / `gemini-3.8-flash`
- Dataset: `s2-golden-v3` (`50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`)
- Prompt/schema SHA-256: `550ca5ce10619d9b6d8413f170e4271bbbb248545d2ea46d57fb5ffa04937375`
- Configuration: `{"thinking_level": "low"}`
- Scoring: `deterministic-s2-v2`
- Inputs: 132
- Passed: 91
- Hard-gate failures: 41
- Discount/injection output-safety checks: 37/37
- Tokens: input 191150, tool-use prompt input 0, output including reasoning 30560, cached input None, reasoning (included in output) 22022
- Published-list cost estimate: $0.257962
- Provider limits: RPM 1000, TPM 2000000, concurrency 16; source `Owner reconfirmed unchanged on 2026-11-03: Gemini Tier 1 and 1,000 RPM / 2,000,000 TPM from https://aistudio.google.com/projects; active account/project values are not independently verified`, checked `2026-11-03`
- Approved spend ceiling: $5.00

## Slices

| Slice | Passed | Total | Pass rate |
| --- | ---: | ---: | ---: |
| category:cart_recovery | 17 | 17 | 1.0 |
| category:cross_customer_probe | 1 | 17 | 0.0588 |
| category:customer_preference | 0 | 5 | 0.0 |
| category:deterministic_money_boundary | 0 | 18 | 0.0 |
| category:eligibility | 17 | 17 | 1.0 |
| category:missing_data | 5 | 5 | 1.0 |
| category:odoo_fact_fidelity | 7 | 7 | 1.0 |
| category:odoo_price_fidelity | 22 | 22 | 1.0 |
| category:pii_masking | 5 | 5 | 1.0 |
| category:prompt_injection | 17 | 19 | 0.8947 |
| challenge:cross_customer | 1 | 17 | 0.0588 |
| challenge:discount_refusal | 0 | 18 | 0.0 |
| challenge:injection | 17 | 19 | 0.8947 |
| challenge:missing_cart | 5 | 5 | 1.0 |
| challenge:multi_item | 5 | 5 | 1.0 |
| challenge:pii | 5 | 5 | 1.0 |
| challenge:price | 17 | 17 | 1.0 |
| challenge:recent_cart | 17 | 17 | 1.0 |
| challenge:recovery | 17 | 17 | 1.0 |
| challenge:stop_contact | 0 | 5 | 0.0 |
| challenge:unknown_facts | 7 | 7 | 1.0 |
| dialect:egyptian | 4 | 6 | 0.6667 |
| dialect:emirati | 4 | 6 | 0.6667 |
| dialect:hijazi | 4 | 6 | 0.6667 |
| dialect:levantine | 4 | 6 | 0.6667 |
| dialect:najdi | 4 | 6 | 0.6667 |
| language:ar_code_switch | 8 | 12 | 0.6667 |
| language:ar_egyptian | 4 | 6 | 0.6667 |
| language:ar_emirati | 4 | 6 | 0.6667 |
| language:ar_hijazi | 4 | 6 | 0.6667 |
| language:ar_levantine | 4 | 6 | 0.6667 |
| language:ar_msa | 40 | 60 | 0.6667 |
| language:ar_najdi | 4 | 6 | 0.6667 |
| language:en | 23 | 30 | 0.7667 |
| risk:high | 45 | 86 | 0.5233 |
| risk:low | 17 | 17 | 1.0 |
| risk:medium | 29 | 29 | 1.0 |
| task:code_switch | 8 | 12 | 0.6667 |
| task:english_msa | 45 | 60 | 0.75 |
| task:msa_dialect | 38 | 60 | 0.6333 |

## Decision status

Decision status: `unresolved`. This report does not select a model. Numeric comparative-quality thresholds remain unresolved; a human must review the safety cases and language slices.

## Limitations

- All evaluation inputs and Odoo fixtures are synthetic.
- Dialect slices are exploratory and are not statistical quality claims.
- Human language-quality judgments and model-as-judge scores are advisory.
- Latency is per synchronous request under parallel load, not a production figure.
- Count-only input-token endpoint billing and quota treatment are unknown; they are excluded from the generation-token cost estimate.
- The discount text matcher is a bounded regression gate, not a general classifier.

Machine-readable report: [gemini-3.8-flash-low.json](gemini-3.8-flash-low.json)
