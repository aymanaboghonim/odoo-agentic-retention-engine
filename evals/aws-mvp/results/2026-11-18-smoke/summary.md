# AWS MVP smoke

Run: 2026-11-18T15:38:03Z (UTC), mode apply. Raw data: [results.json](results.json).

| Check | What | Result | Detail |
| :-- | :-- | :-- | :-- |
| SM-1 | HTTPS via CloudFront; HTTP redirects | pass | https 200; http 301 -> https://d1lajgmkx8ikgu.cloudfront.net/ |
| SM-2 | no public path to the host | pass | public ipv4: none; ingress rules: [(8080, 'pl-3b927c52')] |
| SM-3 | origin guard and auth | pass | CloudFront without token: 401; host without X-Origin-Verify: 403 |
| SM-4 | Cognito RBAC | pass | viewer read 200, viewer decide 403, approver decide 409 |
| SM-5 | odoo_ro read-only and website_sale_loyalty | pass | erp_minimal_fixtures --preflight-only exit 0 |

Limits: the end-to-end retention flow is run separately (runbook); this run does not measure cost or latency.
