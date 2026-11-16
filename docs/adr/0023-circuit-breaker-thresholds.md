# ADR-0023: Circuit breaker thresholds

- Status: accepted
- Date: 2026-11-16
- Issue: #87

## Context

[ADR-0022](0022-runtime-state-fixed-rule-fallback-and-breaker.md) left the breaker off until its numbers were set from measured data. The owner asked for the most realistic numbers from the release-gate evaluations. The measured baselines (62 calls per run, plus 44 held-out calls):

| Run | P50 | P95 | Max | Failure rate |
| :-- | :-- | :-- | :-- | :-- |
| v2 prompt | 2.6 s | 5.2 s | 33.0 s (one call that timed out and was retried) | 1.6% |
| v3 prompt | 2.6 s | 4.7 s | 5.4 s | 0% |
| held-out | 2.0 s | 4.9 s | 5.4 s | 0% |

The provider client timeout is 30 s. Traffic is small, so a window can hold few calls.

## Decision

| Setting | Value | Why |
| :-- | :-- | :-- |
| `BREAKER_ENABLED` | `YES` | The owner asked to set the breaker. It can only move `active` to `fallback_fixed_rule`, and an admin re-enables. |
| `BREAKER_WINDOW_MINUTES` | 15 | Long enough to hold a sample, short enough to react within a quarter of an hour. |
| `BREAKER_MIN_SAMPLE` | 20 | A few failed calls in a quiet period must not trip it. |
| `BREAKER_MAX_FAILURE_RATE_PERCENT` | 10 | About 6 times the worst measured rate (1.6%). The check is strictly greater, so with 20 attempts it trips at 3 failures. |
| `BREAKER_MAX_P95_SECONDS` | 15 | About 3 times the worst measured P95 (5.2 s) and below the 30 s client timeout. One slow call cannot trip it with 20 or more attempts; two can. |

These are starting values from a small synthetic sample (106 calls, one provider, one model). They are not a promise of production behavior. Review them after the first real traffic and change them in a new ADR.

## Consequences

- The deployment defaults in `deploy/full-stack-compose.yml` and `.env.example` carry these values. The code still has no defaults and fails closed on a missing or invalid value when enabled.
- A real provider outage that fails 3 of 20 calls in 15 minutes moves the engine to the fixed-rule fallback without a human; an admin must re-enable.
- A trip is audited in `engine_runtime_events` with actor `circuit-breaker`.
