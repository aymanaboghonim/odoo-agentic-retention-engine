# Live evaluation report (#135)

- Fingerprint: `3b939f5a4e9d1e1163cf9bb9a4aed7512e7e28f780a5c3c7c11103aed2961c67`
- Commit: `c20632bc54ae5062a3d242ba7ca27ac609f0e7b8`; workflow run: `45783205042`
- Model: `gpt-6-luna`; prompt: `proposal-v3-consent-and-facts`
- Safety gates G1..G3: **PASS**; quality gates G4/G5: **WARN (deploy needs an override reason)**
- Spend: USD 0.013630125000 estimated; USD 0.496368000000 reserved (run ceiling USD 1.90).
- Raw data: [report.json](report.json).

| Set | Cases | G1 | G2 | G3 | G4/G5 | Estimated USD | Report |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| reviewed | 62 | pass | pass | pass | pass | 0.007871750000 | [evaluation](reviewed-evaluation/README.md), [smoke](reviewed-smoke/README.md) |
| heldout | 44 | pass | pass | pass | FAIL | 0.005758375000 | [evaluation](heldout-evaluation/README.md), [smoke](heldout-smoke/README.md) |

## Langfuse

Read back through `GET /api/public/v2/observations` (environment `eval`): status `complete`, 108 of 108 expected generations listed in 108 traces.

## How to read this

Each set ran a 1-case smoke checked by machine, then every case, through the real proposal step on fixture carts. Nothing was executed or sent. The owner's approval of the workflow run replaced the smoke digest review (owner decision 2026-11-26). The deploy gate (#157) accepts this report only for a commit with the same fingerprint. Limits of each set are in its evaluation report.
