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

## Cloud deployment (AWS MVP)

The MVP runs on one ECS-on-EC2 host in a private subnet ([ADR-0019](../adr/0019-aws-ecs-on-ec2-cloud-runtime.md), [ADR-0020](../adr/0020-aws-cloudfront-s3-edge-and-backups.md), [ADR-0024](../adr/0024-mvp-ecs-task-layout-and-cloudfront-origin.md), [ADR-0027](../adr/0027-operator-container-in-core-task.md)). All containers are in one ECS task in bridge mode, so they reach each other by link name, as in the local Compose stack. Ports and auth per hop are in [data-connections.md](data-connections.md#aws-mvp-network).

```mermaid
flowchart LR
    person(["Operator<br/>browser or CLI"])
    cognito["Cognito<br/>managed login, TOTP MFA"]
    cf["CloudFront"]
    ui[("S3 UI bucket<br/>private, OAC")]
    ssm["SSM<br/>Run Command, port forwarding"]

    subgraph host["EC2 t3a.medium, private subnet, no public IPv4"]
        api["approval-api"]
        worker["worker"]
        op["operator<br/>one-off commands"]
        odoo["Odoo"]
        odoodb[("odoo-db")]
        enginedb[("engine-db")]
        mailpit["Mailpit"]
    end
    ext["OpenAI, Telegram<br/>IPv6 egress only"]

    person -- "sign in (PKCE)" --> cognito
    person -- "HTTPS + bearer token" --> cf
    cf -- "/*" --> ui
    cf -- "/api/* VPC origin, X-Origin-Verify" --> api
    person -- "allowlisted commands" --> ssm --> op
    api --> enginedb
    worker --> enginedb
    worker -- "read-only role" --> odoodb
    worker -- "JSON-2 API key" --> odoo
    worker -- "SMTP" --> mailpit
    odoo --> odoodb
    worker --> ext
    op --> enginedb
    op --> odoo

    classDef app fill:#dbeafe,stroke:#1d4ed8,color:#0f172a
    classDef data fill:#dcfce7,stroke:#15803d,color:#0f172a
    classDef external fill:#f1f5f9,stroke:#64748b,color:#0f172a
    classDef person fill:#fef3c7,stroke:#b45309,color:#0f172a
    class api,worker,op,odoo,mailpit app
    class odoodb,enginedb,ui data
    class cognito,cf,ssm,ext external
    class person person
```

The only inbound path is CloudFront to the API port. There is no NAT, no Elastic IP, and port 22 is closed. The live smoke checks this ([2026-11-18 report](../../evals/aws-mvp/results/2026-11-18-smoke/summary.md)).

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
