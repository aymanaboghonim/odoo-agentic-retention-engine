# Spec: Operator RBAC (OIDC/JWKS) and expiring approvals

- Issue: #86
- Parent: [approval API and CLI](approval-api.md) (superseded in part, see below)
- ADRs: [ADR-0012](../adr/0012-worker-owned-agent-execution.md),
  [ADR-0019](../adr/0019-aws-ecs-on-ec2-cloud-runtime.md),
  [ADR-0020](../adr/0020-aws-cloudfront-s3-edge-and-backups.md)
- Facts: [F-057](../facts-register.md) (PyJWT, cryptography), [F-058](../facts-register.md) (Cognito JWTs), [F-059](../facts-register.md) (CloudFront origin headers)
- Status: draft (decisions from the plan approved by the owner on 2026-11-16; owner to mark `agreed`)

## Purpose

Replace the single static `APPROVAL_TOKEN` with real operator identity and authorization, and stop approvals from living forever. This is MVP Tier 1 work that blocks the cloud go-live (`#88`).

## Supersedes

This spec supersedes these decisions in [approval-api.md](approval-api.md): one named static-token operator, loopback-only access, and no time-based expiry. Its invariant `AA-1` is replaced by `RBAC-1`..`RBAC-7`. `AA-2`..`AA-4`, `AS-3`..`AS-5` stay in force.

## Scope and non-goals

In scope: OIDC/JWKS access-token verification (Amazon Cognito in AWS, a local RSA JWKS in CI and local dev), `viewer` / `approver` / `admin` roles, a mandatory `expires_at` on approvals with worker-side rechecks, and an `X-Origin-Verify` check for the CloudFront path.

Out of scope: Cognito and CloudFront Terraform (`#88`), the admin kill-switch endpoint (`#87`), an operator UI, and customer authentication.

## Decisions and reasons

- **Verify standard JWTs with PyJWT** (`PyJWKClient` for cached JWKS and `kid` rotation). Only `RS256` is accepted: Cognito signs with RS256, and a fixed algorithm list blocks `none` and HS/RS confusion (F-058).
- **Verify access tokens.** Required claims: `exp`, `iss`, `sub`, `token_use == "access"`, and `client_id` equal to the configured app client. `aud` is checked instead when it is present and a resource server is configured. Expiry is checked offline; revocation is not detectable offline, so access-token lifetime must stay short (F-058).
- **Roles come from one configured claim** (`OIDC_ROLES_CLAIM`, a list of strings). `viewer` < `approver` < `admin`; a higher role includes the lower ones. Unknown, missing, or malformed values grant nothing. The claim name is configuration because `cognito:groups` is not yet verified on a real token (open question).
- **Actor is the token `sub`.** It is an opaque ID, so no name or email is stored (consistent with [Q12](privacy-consent-retention.md)).
- **Remove the static token.** One auth path, fail closed. Local dev and CI use a generated RSA key and a JWKS file (`OIDC_JWKS_FILE`). That file mode is refused when origin verification is on, so it cannot reach the cloud path.
- **Origin verification.** When `APPROVAL_ORIGIN_VERIFY_SECRET` is set, every request must carry `X-Origin-Verify` with that value (constant-time compare), checked before authentication. CloudFront overwrites a viewer-supplied header with the configured value (F-059). `/api/*` must disable caching and forward `Authorization` (F-059).
- **Approval TTL: 24 hours by default** (`APPROVAL_TTL_HOURS`, integer 1..168, owner decision 2026-11-16). `expires_at = created_at + TTL` is set when the approval request is created and covers the whole window until execution. It is not part of `snapshot_hash`, so existing snapshot binding is unchanged.
- **Expiry is enforced three times:** at decision time, when listing (expired rows are hidden), and in the worker. A sweep moves expired pending approvals to `cancelled` (`awaiting_approval -> cancelled` already exists).
- **Existing rows** are backfilled with `expires_at = created_at` (already expired, fail closed) before the column becomes `NOT NULL`.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| RBAC-1 | Forged, unsigned (`alg=none`), HS-confused, wrong-key, or unknown-`kid` tokens are rejected with 401. Only `RS256` is accepted. | `test_forged_or_unsigned_tokens_are_rejected` |
| RBAC-2 | Expired, not-yet-valid, wrong `iss`, wrong `client_id`/`aud`, wrong `token_use`, or missing `sub`/`exp` are rejected with 401. | `test_invalid_claims_are_rejected` |
| RBAC-3 | `viewer` can list; `approver` and `admin` can list and decide; a `viewer` decision is 403. | `test_role_matrix` |
| RBAC-4 | No role, an unknown role, or a malformed roles claim grants nothing (403). | `test_missing_or_unknown_roles_fail_closed` |
| RBAC-5 | With an origin secret set, a missing or wrong `X-Origin-Verify` is rejected with 403 before authentication; the secret is never logged or returned. `OIDC_JWKS_FILE` plus an origin secret is a startup error. | `test_origin_verify_header`, `test_local_jwks_refused_with_origin_verify` |
| RBAC-6 | Missing or invalid auth settings fail startup closed; no static-token path remains. | `test_auth_settings_fail_closed`, `test_static_token_removed` |
| RBAC-7 | The stored actor is the token `sub`; tokens never appear in logs, responses, events, or CLI arguments. | `test_actor_is_subject_and_token_not_exposed` |
| AA-5 | `approval_requests.expires_at` is mandatory; deciding after expiry fails with 409 and writes nothing. | `test_decision_after_expiry_is_rejected` |
| AA-6 | Expired pending approvals are not listed and are swept to `cancelled` with an audit event. | `test_expired_pending_is_hidden_and_swept` |
| AA-7 | The worker refuses an expired human approval before the claim and again before the coupon and the message; the run ends rejected with reason `approval_expired` and no side effect occurs. | `test_execution_rejects_expired_approval` |

Existing tests for `AA-2`..`AA-4` are updated to mint JWTs instead of using the static token.

## Behavior

- **Given** a valid access token with role `approver` and a pending, unexpired approval, **when** the operator posts the exact `snapshot_hash` and a decision, **then** the decision is recorded with the token `sub` as actor and the run returns to `evaluating_policy` (approve) or `rejected` (reject).
- **Given** a token signed by another key, or an `alg=none` token, **when** any endpoint is called, **then** the response is 401 and nothing is read or written.
- **Given** an approval approved yesterday whose `expires_at` has now passed, **when** the worker executes the run, **then** execution is refused before any claim or effect and the run is rejected with `approval_expired`.
- **Abuse case:** a viewer posting a decision gets 403; a request that skips CloudFront and omits `X-Origin-Verify` gets 403 even with a valid token.

## Risks

- A leaked but unexpired access token works until it expires. Mitigation: short Cognito access-token lifetime (`#88`), RBAC limits, per-decision audit by `sub`.
- The origin secret is a shared secret. Mitigation: SSM Parameter Store, constant-time compare, the CloudFront prefix-list security group (ADR-0020), and rotation as an operations task.
- The role claim may differ from the assumption. Mitigation: configurable claim name and a fail-closed default; verify on a real token in `#88`.
- Clock skew near expiry: a small fixed leeway applies to `exp`/`nbf` only, never to `expires_at`.

## Open questions

- Is the Cognito access-token group claim exactly `cognito:groups` (F-058 does not confirm it)? Verified in `#88` on a real token.
- Approval-secret and signing-key rotation procedure (operations runbook in `#88`).
