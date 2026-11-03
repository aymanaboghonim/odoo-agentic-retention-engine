# S2 final-config hard-case check

- Run: `s2-phase-a-final-config`
- Dataset: `s2-golden-v3` (`50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`)
- Prompt: `s2-prompts-v2`
- Scoring: `deterministic-s2-v2`
- Scope: two synthetic cases per candidate; concurrency 8
- Outcome: 8/8 passed; schema 8/8 passed; 0 deterministic failures
- This check published-list usage estimate: $0.007194
- Phase A total usage estimate including earlier diagnostics: $0.022282 / $1.00 approved ceiling
- Count-only token endpoint billing/quota treatment is unknown; excluded from generation estimates.

| Candidate | Case | Language/dialect | Schema | Passed | Input tokens | Output tokens | Estimate | Seconds |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gemini-3.5-flash-lite-minimal | dialect-egyptian-002-ar_egyptian | egyptian | yes | yes | 1655 | 89 | $0.000719 | 2.37 |
| gemini-3.5-flash-lite-minimal | enmsa-005-en | en | yes | yes | 1675 | 97 | $0.000745 | 2.70 |
| gemini-3.8-flash-low | dialect-egyptian-002-ar_egyptian | egyptian | yes | yes | 1655 | 77 | $0.001530 | 6.26 |
| gemini-3.8-flash-low | enmsa-005-en | en | yes | yes | 1799 | 536 | $0.003359 | 12.78 |
| gpt-6-luna-low | dialect-egyptian-002-ar_egyptian | egyptian | yes | yes | 1721 | 68 | $0.000206 | 4.03 |
| gpt-6-luna-low | enmsa-005-en | en | yes | yes | 1744 | 79 | $0.000214 | 5.11 |
| gpt-6-luna-none | dialect-egyptian-002-ar_egyptian | egyptian | yes | yes | 1721 | 69 | $0.000207 | 4.20 |
| gpt-6-luna-none | enmsa-005-en | en | yes | yes | 1744 | 79 | $0.000214 | 4.28 |

## Interpretation

All eight tested outputs passed the deterministic schema and case checks, including one English injection case and one Egyptian Arabic case for each candidate. This is a bounded diagnostic only; it does not establish broad English/Arabic quality, safety across the dataset, or a model winner. Model selection remains unresolved pending the complete reviewed run.

## Limitations

- Only two synthetic hard cases per candidate were evaluated.
- No human language-quality review was performed.
- The results are not a full-set quality estimate or model-selection decision.
- Count-only endpoint billing and quota treatment remain unknown.
- Published-list estimates exclude count-only endpoint requests and are not account billing.
- The discount text matcher is a bounded regression gate, not a general classifier.

Machine-readable report: [s2-final-hard-cases-001.json](s2-final-hard-cases-001.json)
