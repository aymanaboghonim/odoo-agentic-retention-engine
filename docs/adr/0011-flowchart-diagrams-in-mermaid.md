# ADR-0011: Architecture diagrams as Mermaid flowcharts

- Status: accepted
- Date: 2026-10-25

## Context

[ADR-0010](0010-c4-diagrams-in-mermaid.md) chose Mermaid C4 syntax. The first C4 diagrams were rejected by the owner: the layout was cluttered and hard to read. Mermaid C4 is experimental and layout follows statement order only ([F-021](../facts-register.md)). GitHub still renders Mermaid natively ([F-020](../facts-register.md)), and standard `flowchart` diagrams give direction control, subgraphs and colors.

## Decision

- Keep the C4 levels (context, containers, components) but draw them as Mermaid `flowchart` diagrams.
- Use `flowchart LR`, subgraphs for boundaries and a small shared `classDef` palette (engine, external, data).
- One diagram per question. About ten nodes at most. Label only the edges that carry meaning.
- Check each diagram in the GitHub preview before merging.

## Alternatives

- **Keep Mermaid C4:** no layout control; the owner rejected the result.
- **Structurizr DSL or PlantUML:** not rendered by GitHub; needs a build step and committed images.
- **Interactive HTML tools (for example Archify):** not rendered in GitHub Markdown, so not reviewable in a PR. May be used later for a showcase page.

## Consequences

- Better readability and control with no new tooling.
- Diagrams are no longer strict C4 notation. The level names (C1, C2, C3) stay as a guide.
- Supersedes ADR-0010.
