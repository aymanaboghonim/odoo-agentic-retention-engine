# S2 LLM evaluation report

- Run: `s2-ab-smoke-clarity` (`smoke`)
- Completed: `2026-11-03T11:27:46.661780+00:00`
- Candidate: `gpt-6-luna-low`
- Provider/model: `openai` / `gpt-6-luna`
- Dataset: `s2-golden-v3` (`50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`)
- Prompt: `s2-prompts-v3-tool-clarity` (`tool-clarity`)
- Prompt/schema SHA-256: `c273bcbe95cf55a3418c9c1774bf454274a3041d5b418d617a5aeea5947822d3`
- Configuration: `{"reasoning_effort": "low"}`
- Scoring: `deterministic-s2-v3`
- Inputs: 2
- Passed: 2
- Hard-gate failures: 0
- Unsupported discount-claim checks: 2/2 (all customer-facing outputs; no_action with null text is allowed)
- Tokens: input 4103, tool-use prompt input None, output including reasoning 141, cached input 0, reasoning (included in output) 0
- Published-list cost estimate: $0.000481
- Provider limits: RPM 5000, TPM 2000000, concurrency 1; source `Owner reconfirmed unchanged on 2026-11-03: active Build organization tier; 5,000 RPM / 2,000,000 TPM from https://developers.openai.com/api/docs/models/gpt-6-luna; active account values are not independently verified`, checked `2026-11-03`
- Approved spend ceiling: $0.15

## Slices

| Slice | Passed | Total | Pass rate |
| --- | ---: | ---: | ---: |
| category:cart_recovery | 1 | 1 | 1.0 |
| category:eligibility | 1 | 1 | 1.0 |
| challenge:recent_cart | 1 | 1 | 1.0 |
| challenge:recovery | 1 | 1 | 1.0 |
| language:en | 2 | 2 | 1.0 |
| risk:low | 1 | 1 | 1.0 |
| risk:medium | 1 | 1 | 1.0 |
| task:english_msa | 2 | 2 | 1.0 |

## Smoke-only Odoo activity gate

- Approved Odoo inventory manifest SHA-256: `0f3a451165981247524c5cc2968355c5fd4e2ccd2f22a01e8e9ba7122a320316`
- Cleanup: `completed`

| Case | Internal activity created |
| --- | --- |
| enmsa-001-en | True |
| enmsa-007-en | False |

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

Machine-readable report: [s2-ab-smoke-clarity-gpt-6-luna-low.json](s2-ab-smoke-clarity-gpt-6-luna-low.json)
