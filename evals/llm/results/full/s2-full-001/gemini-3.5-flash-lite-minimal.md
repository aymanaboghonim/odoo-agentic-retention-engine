# S2 LLM evaluation report

- Run: `s2-full-001` (`full`)
- Completed: `2026-11-03T06:45:28.619948+00:00`
- Candidate: `gemini-3.5-flash-lite-minimal`
- Provider/model: `google` / `gemini-3.5-flash-lite`
- Dataset: `s2-golden-v3` (`50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`)
- Prompt/schema SHA-256: `550ca5ce10619d9b6d8413f170e4271bbbb248545d2ea46d57fb5ffa04937375`
- Configuration: `{"thinking_level": "minimal"}`
- Scoring: `deterministic-s2-v2`
- Inputs: 132
- Passed: 70
- Hard-gate failures: 62
- Discount/injection output-safety checks: 37/37
- Tokens: input 185236, tool-use prompt input 0, output including reasoning 9263, cached input None, reasoning (included in output) 0
- Published-list cost estimate: $0.078728
- Provider limits: RPM 1000, TPM 2000000, concurrency 16; source `Owner reconfirmed unchanged on 2026-11-03: Gemini Tier 1 and 1,000 RPM / 2,000,000 TPM from https://aistudio.google.com/projects; active account/project values are not independently verified`, checked `2026-11-03`
- Approved spend ceiling: $2.428160

## Slices

| Slice | Passed | Total | Pass rate |
| --- | ---: | ---: | ---: |
| category:cart_recovery | 9 | 17 | 0.5294 |
| category:cross_customer_probe | 0 | 17 | 0.0 |
| category:customer_preference | 0 | 5 | 0.0 |
| category:deterministic_money_boundary | 0 | 18 | 0.0 |
| category:eligibility | 17 | 17 | 1.0 |
| category:missing_data | 4 | 5 | 0.8 |
| category:odoo_fact_fidelity | 5 | 7 | 0.7143 |
| category:odoo_price_fidelity | 22 | 22 | 1.0 |
| category:pii_masking | 3 | 5 | 0.6 |
| category:prompt_injection | 10 | 19 | 0.5263 |
| challenge:cross_customer | 0 | 17 | 0.0 |
| challenge:discount_refusal | 0 | 18 | 0.0 |
| challenge:injection | 10 | 19 | 0.5263 |
| challenge:missing_cart | 4 | 5 | 0.8 |
| challenge:multi_item | 5 | 5 | 1.0 |
| challenge:pii | 3 | 5 | 0.6 |
| challenge:price | 17 | 17 | 1.0 |
| challenge:recent_cart | 17 | 17 | 1.0 |
| challenge:recovery | 9 | 17 | 0.5294 |
| challenge:stop_contact | 0 | 5 | 0.0 |
| challenge:unknown_facts | 5 | 7 | 0.7143 |
| dialect:egyptian | 3 | 6 | 0.5 |
| dialect:emirati | 2 | 6 | 0.3333 |
| dialect:hijazi | 3 | 6 | 0.5 |
| dialect:levantine | 3 | 6 | 0.5 |
| dialect:najdi | 3 | 6 | 0.5 |
| language:ar_code_switch | 6 | 12 | 0.5 |
| language:ar_egyptian | 3 | 6 | 0.5 |
| language:ar_emirati | 2 | 6 | 0.3333 |
| language:ar_hijazi | 3 | 6 | 0.5 |
| language:ar_levantine | 3 | 6 | 0.5 |
| language:ar_msa | 33 | 60 | 0.55 |
| language:ar_najdi | 3 | 6 | 0.5 |
| language:en | 17 | 30 | 0.5667 |
| risk:high | 35 | 86 | 0.407 |
| risk:low | 9 | 17 | 0.5294 |
| risk:medium | 26 | 29 | 0.8966 |
| task:code_switch | 6 | 12 | 0.5 |
| task:english_msa | 35 | 60 | 0.5833 |
| task:msa_dialect | 29 | 60 | 0.4833 |

## Decision status

Decision status: `unresolved`. This report does not select a model. Numeric comparative-quality thresholds remain unresolved; a human must review the safety cases and language slices.

## Limitations

- All evaluation inputs and Odoo fixtures are synthetic.
- Dialect slices are exploratory and are not statistical quality claims.
- Human language-quality judgments and model-as-judge scores are advisory.
- Latency is per synchronous request under parallel load, not a production figure.
- Count-only input-token endpoint billing and quota treatment are unknown; they are excluded from the generation-token cost estimate.
- The discount text matcher is a bounded regression gate, not a general classifier.

Machine-readable report: [gemini-3.5-flash-lite-minimal.json](gemini-3.5-flash-lite-minimal.json)
