# S2 LLM evaluation report

- Run: `s2-smoke-002` (`smoke`)
- Completed: `2026-11-03T06:12:17.947351+00:00`
- Candidate: `gemini-3.8-flash-low`
- Provider/model: `google` / `gemini-3.8-flash`
- Dataset: `s2-golden-v3` (`50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`)
- Prompt/schema SHA-256: `550ca5ce10619d9b6d8413f170e4271bbbb248545d2ea46d57fb5ffa04937375`
- Configuration: `{"thinking_level": "low"}`
- Scoring: `deterministic-s2-v2`
- Inputs: 2
- Passed: 2
- Hard-gate failures: 0
- Discount/injection output-safety checks: not applicable (0 cases in this run)
- Tokens: input 3366, tool-use prompt input 0, output including reasoning 386, cached input None, reasoning (included in output) 255
- Published-list cost estimate: $0.003972
- Provider limits: RPM 1000, TPM 2000000, concurrency 1; source `Owner-reported RPM/TPM from https://aistudio.google.com/projects; owner confirmed Tier 1 on 2026-11-02; 3,000,000 Batch tokens from https://ai.google.dev/gemini-api/docs/rate-limits; RPM/TPM not independently verified`, checked `2026-11-02`
- Approved spend ceiling: $5.00

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

Machine-readable report: [s2-smoke-002-gemini-3.8-flash-low.json](s2-smoke-002-gemini-3.8-flash-low.json)
