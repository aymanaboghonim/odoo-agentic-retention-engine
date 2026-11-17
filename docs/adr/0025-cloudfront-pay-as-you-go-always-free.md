# ADR-0025: CloudFront on pay-as-you-go Always Free, not a flat-rate plan

- Status: accepted
- Date: 2026-11-17
- Issue: #96 (part B of #88)

## Context

The owner asked for a free CloudFront plan for the MVP. CloudFront has two ways to bill (`F-065`, `F-071`):

- Pay-as-you-go with an Always Free allowance: 1 TB data out, 10M HTTP/HTTPS requests, and 2M CloudFront Functions invocations per month. Origin fetches are free, including VPC origins.
- Flat-rate plans with no overage charges. The Free plan includes 1M requests and 100 GB per month.

The MVP design needs a VPC origin for `/api/*` ([ADR-0024](0024-mvp-ecs-task-layout-and-cloudfront-origin.md)). MVP traffic is a portfolio demo: a few operators and evaluation runs.

## Decision

Keep the distribution on pay-as-you-go and do not subscribe it to a flat-rate plan.

- The flat-rate Free and Pro plans do not include "private origins within VPC". Only Business and Premium do, and those are paid (`F-071`). The Free plan would force the public-origin design that ADR-0024 replaced.
- Accounts that use the AWS Free Tier cannot subscribe to any flat-rate plan (`F-071`).
- At MVP traffic, pay-as-you-go stays inside the Always Free allowance, so CloudFront costs USD 0 (`F-065`). Its allowance is also 10 times larger in requests than the Free plan.

## Alternatives

- **Flat-rate Free plan:** USD 0 and no overage charges, but no VPC origins, and the account may not be eligible. Rejected.
- **Flat-rate Business plan:** includes VPC origins, but it is paid and far above the `<= USD 13/month` target. Rejected.

## Consequences

- Good: USD 0 for CloudFront at MVP traffic, with the private VPC origin kept.
- Bad: pay-as-you-go has overage charges above the free allowance (`F-065` rates). The monthly budget alert (ADR-0019) and the measured cost in #97 watch for this. A traffic spike is unlikely for a private operator API, but it is not prevented.
- Teardown: a pay-as-you-go distribution can be disabled and deleted at any time. A plan subscription would first need canceling (`F-071`).
