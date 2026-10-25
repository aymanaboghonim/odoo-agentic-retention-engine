# Architecture overview

Three focused diagrams show the system boundary, runtime containers, and agent execution path. They are Mermaid flowcharts that follow the C4 levels, as selected in [ADR-0011](../adr/0011-flowchart-diagrams-in-mermaid.md).

## C1: System context

```mermaid
flowchart LR
    customer(["Customer"])
    operator(["Marketing operator"])
    engine["AI Retention Engine<br/>agentic retention proposals"]
    odoo[("Odoo<br/>system of record")]
    channels["Messaging channels<br/>Telegram, email"]

    customer -- "shops" --> odoo
    odoo -- "abandoned carts" --> engine
    engine -- "approved actions" --> odoo
    engine -- "approved messages" --> channels
    channels --> customer
    operator -- "reviews proposals" --> engine

    classDef engine fill:#dbeafe,stroke:#1d4ed8,color:#0f172a
    classDef external fill:#f1f5f9,stroke:#64748b,color:#0f172a
    classDef person fill:#fef3c7,stroke:#b45309,color:#0f172a
    class engine engine
    class odoo,channels external
    class customer,operator person
```

The engine reads from Odoo, proposes retention actions, and sends only approved messages. Proposals that need a human are reviewed by a marketing operator.

## C2: Runtime containers

```mermaid
flowchart LR
    odoo[("Odoo")]

    subgraph engine["AI Retention Engine"]
        worker["Worker<br/>scheduled Python jobs"]
        aidb[("AI database<br/>PostgreSQL")]
        api["API<br/>FastAPI"]
    end
    channels["Messaging channels"]

    odoo -- "abandoned carts" --> worker
    worker --> aidb
    api --> aidb
    api -- "business actions" --> odoo
    api -- "approved messages" --> channels

    classDef app fill:#dbeafe,stroke:#1d4ed8,color:#0f172a
    classDef data fill:#dcfce7,stroke:#15803d,color:#0f172a
    classDef external fill:#f1f5f9,stroke:#64748b,color:#0f172a
    class api,worker app
    class aidb data
    class odoo,channels external
```

The API handles interactive runs. The worker scans carts and refreshes features on a schedule. Both keep state, approvals, and audit data in the AI database, which is separate from Odoo.

## C3: Components (agent execution path)

```mermaid
flowchart LR
    api["API"]
    worker["Worker"]

    subgraph engine["Engine"]
        workflow["Agent workflow<br/>LangGraph"]
        policy["Policy gate<br/>deterministic rules"]
        adapters["Integration adapters<br/>Odoo, LLM, channels"]
        state[("AI database")]
    end

    api --> workflow
    worker --> workflow
    workflow -- "proposals" --> policy
    policy -- "allowed actions" --> adapters
    workflow -- "run state" --> state

    classDef app fill:#dbeafe,stroke:#1d4ed8,color:#0f172a
    classDef safety fill:#fee2e2,stroke:#b91c1c,color:#0f172a
    classDef data fill:#dcfce7,stroke:#15803d,color:#0f172a
    class api,worker,workflow,adapters app
    class policy safety
    class state data
```

The API and the scheduled worker start the same workflow. Every proposal passes the policy gate before an adapter can act, so the LLM never decides money.

## Architecture constraints

- Odoo remains the system of record. The engine reads its database and uses the Odoo API for state changes; AI-owned state is stored separately ([ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md)).
- The API and scheduled worker are entrypoints in one modular monolith; scheduled jobs are used instead of a streaming platform in running code ([ADR-0002](../adr/0002-modular-monolith.md), [ADR-0004](../adr/0004-batch-over-streaming.md)).
- The retrieval store is not selected; pgvector and Qdrant remain candidates until spike S3 ([ADR-0006](../adr/0006-retrieval-store-benchmark.md)).
- The final LLM provider/model follows spike S2 ([ADR-0003](../adr/0003-llm-interface-and-model-selection.md)). Only synthetic data may be sent to cloud services ([ADR-0007](../adr/0007-free-tier-observability.md)).
- C3 focuses on the internal workflow and adapter boundary. External systems are shown in C1 and C2 to avoid repeating a dense integration map.
