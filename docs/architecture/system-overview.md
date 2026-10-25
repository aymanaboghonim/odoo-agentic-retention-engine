# Architecture overview

This page records the current system context, container view, and AI engine components. The diagrams use Mermaid C4 syntax as selected in [ADR-0010](../adr/0010-c4-diagrams-in-mermaid.md).

## C1: System context

```mermaid
C4Context
    title System Context - AI Retention Engine

    Person(customer, "Customer", "Shops through Odoo and may receive a retention message")
    Person(operator, "Marketing operator", "Reviews actions that require human approval")

    System(engine, "AI Retention Engine", "Processes abandoned-cart and retention workflows with deterministic policy checks")
    System_Ext(odoo, "Odoo", "Existing e-commerce and ERP system of record")
    System_Ext(channels, "Customer messaging channels", "Telegram first; email through Mailpit in the POC")
    System_Ext(llm, "LLM provider", "Model provider behind the LLMClient port; final model selection follows S2")

    Rel(customer, odoo, "Browses and places orders")
    Rel(engine, odoo, "Reads operational data and requests business actions through the Odoo API")
    Rel(engine, channels, "Sends approved customer messages")
    Rel(channels, customer, "Delivers messages")
    Rel(operator, engine, "Reviews pending actions; interface is not selected")
    Rel(engine, llm, "Requests proposal or message generation; synthetic data only")
```

## C2: Containers

```mermaid
C4Container
    title Container View - AI Retention Engine

    System_Boundary(engine, "AI Retention Engine") {
        Container(api, "API", "FastAPI + LangGraph", "Handles requests and runs agent workflows")
        Container(worker, "Worker", "Python scheduler", "Scans abandoned carts and refreshes features on a schedule")
        ContainerDb(ai_db, "AI-owned database", "PostgreSQL", "Stores features, audit data, approvals, idempotency keys, and agent checkpoints")
        ContainerDb(retrieval, "Retrieval backend (logical)", "pgvector or Qdrant", "pgvector may share the AI database; Qdrant is a separate service; choice is deferred to S3")
    }

    Container_Ext(odoo_api, "Odoo API", "XML-RPC / JSON-RPC", "Applies Odoo business rules for state-changing actions")
    ContainerDb_Ext(odoo_db, "Odoo PostgreSQL", "PostgreSQL", "System-of-record data; the engine has read-only access")
    System_Ext(channels, "Messaging channels", "Telegram first; Mailpit for POC email")
    System_Ext(llm, "LLM provider", "Selected behind the LLMClient port after spike S2")

    Rel(worker, odoo_db, "Reads cart and order data")
    Rel(api, odoo_api, "Requests state-changing business actions")
    Rel(api, ai_db, "Persists run state, approvals, and audit records")
    Rel(worker, ai_db, "Stores scheduled feature results")
    Rel(api, retrieval, "Searches derived product and policy content")
    Rel(api, llm, "Requests generation with synthetic data")
    Rel(api, channels, "Dispatches approved messages")
```

## C3: AI engine components

```mermaid
C4Component
    title Component View - AI Engine

    Container_Boundary(engine, "AI Engine (API + Worker)") {
        Component(api, "API entrypoint", "FastAPI", "Accepts requests and starts or resumes workflows")
        Component(worker, "Scheduled worker", "Python scheduler", "Finds abandoned carts and refreshes features")
        Component(agent, "Agent workflow", "LangGraph", "Coordinates context, proposal, approval, and execution steps")
        Component(policy, "Policy gate", "Deterministic Python", "Evaluates discount, margin, approval, and usage rules")
        Component(features, "Feature service", "Python", "Loads or refreshes customer and cart features")
        Component(retriever, "Retriever", "Retriever port", "Searches derived product and policy content")
        Component(odoo_adapter, "Odoo adapter", "Read-only SQL + Odoo API", "Separates data reads from state-changing API calls")
        Component(channel_adapter, "Channel adapters", "ChannelPort", "Sends messages through configured channels")
        Component(llm_client, "LLM client", "LLMClient port", "Calls the selected model adapter")
        Component(state_store, "State and audit store", "AI-owned PostgreSQL", "Persists checkpoints, approvals, features, and audit records")
    }

    Rel(api, agent, "Starts or resumes a run")
    Rel(worker, odoo_adapter, "Scans for eligible carts")
    Rel(worker, features, "Schedules feature refresh")
    Rel(agent, features, "Loads customer and cart context")
    Rel(agent, retriever, "Retrieves relevant content")
    Rel(agent, llm_client, "Requests a proposal or message draft")
    Rel(agent, policy, "Submits proposals for deterministic evaluation")
    Rel(agent, odoo_adapter, "Requests approved Odoo actions")
    Rel(agent, channel_adapter, "Dispatches approved messages")
    Rel(agent, state_store, "Persists checkpoints, approvals, and audit events")
```

## Scope and decisions still pending

- Odoo is the system of record; the engine reads its database and uses the Odoo API for state changes. The engine's own state and audit data use a separate database ([ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md)).
- The API and scheduled worker are entrypoints in one modular monolith ([ADR-0002](../adr/0002-modular-monolith.md)); scheduled jobs replace a streaming platform in running code ([ADR-0004](../adr/0004-batch-over-streaming.md)).
- The vector retrieval backend is not selected. The pgvector and Qdrant candidates remain behind a `Retriever` port until spike S3 ([ADR-0006](../adr/0006-retrieval-store-benchmark.md)).
- The final LLM provider/model follows spike S2 ([ADR-0003](../adr/0003-llm-interface-and-model-selection.md)). Only synthetic data may be sent to cloud services ([ADR-0007](../adr/0007-free-tier-observability.md)).
- The operator interface, connection details, and deployment topology are not specified here; see the remaining architecture extraction issues.
