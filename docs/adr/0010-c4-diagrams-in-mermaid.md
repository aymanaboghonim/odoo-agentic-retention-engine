# ADR-0010: C4 diagrams in Mermaid

- Status: superseded by ADR-0011
- Date: 2026-10-25

## Context

We need C4 diagrams (context, container, component) in `docs/architecture/`. They must render in GitHub with no build step, and stay in Git as text so reviews and CI can see changes. Facts: [F-020](../facts-register.md) (GitHub renders Mermaid natively, not PlantUML or Structurizr) and [F-021](../facts-register.md) (Mermaid C4 is experimental, with no automatic layout).

## Decision

- Write C4 diagrams as Mermaid code blocks (`C4Context`, `C4Container`, `C4Component`) inside Markdown files.
- Keep each diagram small: one diagram per question, no more than about ten elements. Use the C1 and C2 levels first. C3 only for the engine.
- Do not use sprites, tags, links or legends (not supported, F-021).
- Check each diagram renders on GitHub in the PR preview before merging.
- If a diagram becomes unreadable because layout cannot be controlled, or Mermaid changes the C4 syntax, write a new ADR. The likely next step is Structurizr DSL with committed SVG files.

## Alternatives

- **Structurizr DSL:** best C4 model and layout, but GitHub does not render it. It needs a build step and committed images that can drift from the source.
- **PlantUML with the C4 library:** same problem: no native rendering, plus a Java or server dependency.
- **D2 or draw.io images:** pictures are hard to review in a diff and drift from the code.

## Consequences

- No extra tooling, no CI step, and diagrams show in GitHub directly.
- Layout is controlled only by statement order. Complex diagrams will look rough.
- The syntax is experimental, so a Mermaid change can break a diagram. Re-check when GitHub updates its Mermaid version.
