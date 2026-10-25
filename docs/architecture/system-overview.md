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
    llm["LLM provider<br/>selected in S2"]

    customer -- "shops" --> odoo
    odoo -- "abandoned carts" --> engine
    engine -- "approved actions" --> odoo
    engine -- "approved messages" --> channels
    channels --> customer
    operator -- "reviews proposals / approvals" --> engine
    engine -- "model calls<br/>synthetic data only" --> llm

    classDef engine fill:#dbeafe,stroke:#1d4ed8,color:#0f172a
    classDef external fill:#f1f5f9,stroke:#64748b,color:#0f172a
    classDef person fill:#fef3c7,stroke:#b45309,color:#0f172a
    class engine engine
    class odoo,channels,llm external
    class customer,operator person
```

The engine reads from Odoo, proposes retention actions, and sends only approved messages. Proposals that need a human are reviewed by a marketing operator.

## C2: Runtime containers

```mermaid
flowchart LR
    odoo[("Odoo")]

    subgraph engine["AI Retention Engine"]
        worker["Worker<br/>scheduled jobs and run execution"]
        aidb[("AI database<br/>PostgreSQL")]
        api["API<br/>FastAPI"]
    end
    channels["Messaging channels"]
    llm["LLM provider<br/>selected in S2"]
    operator(["Marketing operator"])

    odoo -- "abandoned carts" --> worker
    worker --> aidb
    api -- "persist triggers / approvals" --> aidb
    aidb -- "ready runs" --> worker
    worker -- "approved business actions" --> odoo
    worker -- "approved messages" --> channels
    worker -- "model calls" --> llm
    operator -- "trigger / approval decision" --> api

    classDef app fill:#dbeafe,stroke:#1d4ed8,color:#0f172a
    classDef data fill:#dcfce7,stroke:#15803d,color:#0f172a
    classDef external fill:#f1f5f9,stroke:#64748b,color:#0f172a
    classDef person fill:#fef3c7,stroke:#b45309,color:#0f172a
    class api,worker app
    class aidb data
    class odoo,channels,llm external
    class operator person
```

The API durably records inbound triggers and operator decisions. The worker scans carts, processes all runs, and exclusively performs Odoo writes and message dispatch. It uses the separate AI database to claim work and persist run state.

## C3: Components (agent execution path)

```mermaid
flowchart LR
    operator(["Marketing operator"])

    subgraph engine["Engine"]
        api["API<br/>triggers and approvals"]
        state[("AI database")]
        worker["Worker<br/>single run executor"]
        workflow["Agent workflow<br/>LangGraph"]
        policy["Policy gate<br/>deterministic rules"]
        approval["Human approval<br/>when required"]
        adapters["Integration adapters"]
    end

    operator -- "trigger / approve / reject" --> api
    api -- "persist trigger / decision" --> state
    state -- "ready runs" --> worker
    worker --> workflow
    workflow -- "context and model calls" --> adapters
    workflow -- "proposals" --> policy
    policy -- "approval required" --> approval
    approval -- "decision" --> api
    policy -- "auto-approved or approved" --> adapters

    classDef app fill:#dbeafe,stroke:#1d4ed8,color:#0f172a
    classDef safety fill:#fee2e2,stroke:#b91c1c,color:#0f172a
    classDef data fill:#dcfce7,stroke:#15803d,color:#0f172a
    classDef person fill:#fef3c7,stroke:#b45309,color:#0f172a
    class api,worker,workflow,adapters app
    class policy safety
    class approval safety
    class state data
    class operator person
```

The API records triggers and approval decisions. The worker runs every workflow and owns all side effects. The workflow uses adapters for context reads and LLM calls before policy evaluation; only auto-approved or human-approved actions reach adapters for Odoo writes and message dispatch. The external systems are shown in the context and container views.

## Architecture constraints

- Odoo remains the system of record. The engine reads its database and uses the Odoo API for state changes; AI-owned state is stored separately ([ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md)).
- The API and scheduled worker are entrypoints in one modular monolith; the worker alone executes workflows and side effects ([ADR-0002](../adr/0002-modular-monolith.md), [ADR-0012](../adr/0012-worker-owned-agent-execution.md)). Scheduled jobs are used instead of a streaming platform ([ADR-0004](../adr/0004-batch-over-streaming.md)).
- The retrieval store is not selected; pgvector and Qdrant remain candidates until spike S3 ([ADR-0006](../adr/0006-retrieval-store-benchmark.md)).
- The final LLM provider/model follows spike S2 ([ADR-0003](../adr/0003-llm-interface-and-model-selection.md)). Only synthetic data may be sent to cloud services ([ADR-0007](../adr/0007-free-tier-observability.md)).
- C3 focuses on the internal workflow and adapter boundary. External systems are shown in C1 and C2 to avoid repeating a dense integration map.
