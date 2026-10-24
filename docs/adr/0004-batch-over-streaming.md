# ADR-0004: Batch jobs instead of a streaming platform in running code

- Status: accepted
- Date: 2026-10-24

## Context

A large enterprise design may use Debezium, Kafka, Flink, Airflow and a feature store. That stack needs far more RAM than a dev laptop (8 GB WSL2) or the MVP VPS.

## Decision

- Document the Kafka/Flink/feature-store design as the to-be enterprise architecture only.
- In code, use a scheduler in the worker (APScheduler or similar) and Postgres tables or materialized views for `customer_features` (RFM, CLV, churn) and for the abandoned-cart scan.
- Daily feature refresh. Cart scan on a short interval (for example every few minutes). Cart threshold: abandoned for more than 2 hours (target, to confirm).

## Consequences

- Same business result at a small fraction of the resource cost.
- Near-real-time is limited to the scan interval. This is acceptable for the use case.
