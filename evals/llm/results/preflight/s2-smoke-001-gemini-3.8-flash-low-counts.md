# S2 smoke count-token preflight

- Status: Count-only preflight completed; the model smoke has not run.
- Run: `s2-smoke-001`
- Date: `2026-11-02`
- Dataset: `s2-golden-v3`, SHA-256 `50e8af9ec97bffbd334e121e8549664ca08cae3e5a9bf7d7e8f333fe3e3ecd6d`
- Approved Odoo inventory manifest: `0f3a451165981247524c5cc2968355c5fd4e2ccd2f22a01e8e9ba7122a320316`
- Candidate: `gemini-3.8-flash-low` (`gemini-3.8-flash`, thinking `low`)
- Output cap: 8,192 tokens per request; at most four generation requests
- Input cap: not approved
- USD smoke spend ceiling: not approved

## Input counts

| Smoke case | Turn | Input tokens |
| --- | --- | ---: |
| `enmsa-001-en` (`cart-open-001`) | Initial | 271 |
| `enmsa-001-en` (`cart-open-001`) | Canonical follow-up | 406 |
| `enmsa-007-en` (`cart-recent-004`) | Initial | 269 |
| `enmsa-007-en` (`cart-recent-004`) | Canonical follow-up | 399 |
| **Four-request canonical total** | | **1,345** |

Initial counts include the exact synthetic prompt and full generation configuration. Canonical follow-ups include a representative function call and the current synthetic Odoo tool result. They cannot include provider-generated model content or thought signatures. The runner counts each actual follow-up immediately before generation and fails closed if it exceeds the approved input ceiling.

## Cost context

Using F-004's Gemini 3.8 Flash standard paid rates through 2026-12-31, the canonical 1,345 input tokens estimate to `$0.00100875`. Four output caps of 8,192 tokens estimate to `$0.12288000`. The combined canonical-input plus output-cap estimate is `$0.12388875`; it is not a hard spend ceiling because actual follow-up input may differ and CountTokens cost/quota treatment is unknown.

If every generation request used the full F-004 input limit of 1,048,576 tokens, the published-list generation estimate would be `$3.26860800` for four requests at the approved output cap. This is a theoretical generation-token bound, not an approved smoke budget, and it excludes CountTokens cost/quota.

## Execution and limitations

- Five CountTokens requests succeeded, including one repeated verification of the first initial request. Four earlier CountTokens requests were rejected with HTTP 400 while correcting the request body; no generation request was made.
- The request used the official Gemini Developer API `models.countTokens` endpoint with a full `generateContentRequest`, including model, system instruction, tool declarations, tool configuration, and generation configuration.
- Only synthetic case prompts and the two approved synthetic cart contexts were used. No email or Odoo activity was created.
- CountTokens pricing and quota treatment are unknown.
- This report contains no model outputs, evaluation scores, or model-selection decision.
- Do not run `generateContent` until an owner-approved per-request input cap and explicit USD smoke spend ceiling are recorded.

Machine-readable report: [JSON](./s2-smoke-001-gemini-3.8-flash-low-counts.json)
