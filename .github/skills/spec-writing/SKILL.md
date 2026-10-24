---
name: spec-writing
description: 'Use when starting a feature, behavior, guardrail or integration before coding. Writes a short spec in docs/specs with scope, invariants (things that must always be true), key behaviors, risks and how each invariant is checked by a test or CI gate.'
---

# Spec writing

Spec first, then code. Specs live in `docs/specs/<feature>.md`. Create the folder when the first spec is written.

## Rules

- One spec per feature or behavior. Keep it under two pages.
- Every invariant must have a mechanical check: a test, a CI step, or a type or lint rule. An invariant with no check is a wish: move it to Open questions.
- Test by risk: money, data leaks, prompt injection, idempotency, integration contracts. No tests just for coverage.
- Link the spec in the issue and the PR. Link the ADRs it depends on.
- Do not copy facts (prices, limits). Link the facts register.

## Template

```markdown
# Spec: <feature>

- Issue: #N
- ADRs: ADR-NNNN
- Status: draft | agreed | implemented

## Purpose

One or two sentences. Which pain point, which user.

## Scope and non-goals

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| INV-1 | ... | test name or CI step |

## Behavior

Key cases as Given / When / Then. Include one failure case and one abuse case.

## Risks

What can go wrong and how we limit it.

## Open questions
```

## Procedure

1. Read the issue, [docs/00-vision.md](../../../docs/00-vision.md) and related ADRs.
2. Write the spec from the template. Ask the owner about anything unclear instead of guessing.
3. Get the owner to mark it `agreed` before implementation starts.
4. In the implementing PR, reference each invariant ID next to its check.
