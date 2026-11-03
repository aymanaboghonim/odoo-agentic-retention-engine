# S2 LLM evaluation report

- Run: `s2-full-001` (`full`)
- Completed: `2026-11-03T06:45:53.214786+00:00`
- Candidate: `gpt-6-luna-none`
- Provider/model: `openai` / `gpt-6-luna`
- Dataset: `s2-golden-v3` (`50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`)
- Prompt/schema SHA-256: `e4c4f1ab9db71be84c4e0421b4292514ba7fd826b8b817ce9a7e2059822bc0d7`
- Configuration: `{"reasoning_effort": "none"}`
- Scoring: `deterministic-s2-v2`
- Inputs: 132
- Passed: 106
- Hard-gate failures: 26
- Discount/injection output-safety checks: 37/37
- Tokens: input 194398, tool-use prompt input None, output including reasoning 8614, cached input 0, reasoning (included in output) 0
- Published-list cost estimate: $0.023747
- Provider limits: RPM 5000, TPM 2000000, concurrency 16; source `Owner reconfirmed unchanged on 2026-11-03: active Build organization tier; 5,000 RPM / 2,000,000 TPM from https://developers.openai.com/api/docs/models/gpt-6-luna; active account values are not independently verified`, checked `2026-11-03`
- Approved spend ceiling: $2.00

## Slices

| Slice | Passed | Total | Pass rate |
| --- | ---: | ---: | ---: |
| category:cart_recovery | 17 | 17 | 1.0 |
| category:cross_customer_probe | 2 | 17 | 0.1176 |
| category:customer_preference | 3 | 5 | 0.6 |
| category:deterministic_money_boundary | 9 | 18 | 0.5 |
| category:eligibility | 17 | 17 | 1.0 |
| category:missing_data | 5 | 5 | 1.0 |
| category:odoo_fact_fidelity | 7 | 7 | 1.0 |
| category:odoo_price_fidelity | 22 | 22 | 1.0 |
| category:pii_masking | 5 | 5 | 1.0 |
| category:prompt_injection | 19 | 19 | 1.0 |
| challenge:cross_customer | 2 | 17 | 0.1176 |
| challenge:discount_refusal | 9 | 18 | 0.5 |
| challenge:injection | 19 | 19 | 1.0 |
| challenge:missing_cart | 5 | 5 | 1.0 |
| challenge:multi_item | 5 | 5 | 1.0 |
| challenge:pii | 5 | 5 | 1.0 |
| challenge:price | 17 | 17 | 1.0 |
| challenge:recent_cart | 17 | 17 | 1.0 |
| challenge:recovery | 17 | 17 | 1.0 |
| challenge:stop_contact | 3 | 5 | 0.6 |
| challenge:unknown_facts | 7 | 7 | 1.0 |
| dialect:egyptian | 4 | 6 | 0.6667 |
| dialect:emirati | 4 | 6 | 0.6667 |
| dialect:hijazi | 4 | 6 | 0.6667 |
| dialect:levantine | 4 | 6 | 0.6667 |
| dialect:najdi | 5 | 6 | 0.8333 |
| language:ar_code_switch | 10 | 12 | 0.8333 |
| language:ar_egyptian | 4 | 6 | 0.6667 |
| language:ar_emirati | 4 | 6 | 0.6667 |
| language:ar_hijazi | 4 | 6 | 0.6667 |
| language:ar_levantine | 4 | 6 | 0.6667 |
| language:ar_msa | 51 | 60 | 0.85 |
| language:ar_najdi | 5 | 6 | 0.8333 |
| language:en | 24 | 30 | 0.8 |
| risk:high | 60 | 86 | 0.6977 |
| risk:low | 17 | 17 | 1.0 |
| risk:medium | 29 | 29 | 1.0 |
| task:code_switch | 10 | 12 | 0.8333 |
| task:english_msa | 50 | 60 | 0.8333 |
| task:msa_dialect | 46 | 60 | 0.7667 |

## Decision status

Decision status: `unresolved`. This report does not select a model. Numeric comparative-quality thresholds remain unresolved; a human must review the safety cases and language slices.

## Limitations

- All evaluation inputs and Odoo fixtures are synthetic.
- Dialect slices are exploratory and are not statistical quality claims.
- Human language-quality judgments and model-as-judge scores are advisory.
- Latency is per synchronous request under parallel load, not a production figure.
- Count-only input-token endpoint billing and quota treatment are unknown; they are excluded from the generation-token cost estimate.
- The discount text matcher is a bounded regression gate, not a general classifier.

Machine-readable report: [gpt-6-luna-none.json](gpt-6-luna-none.json)
