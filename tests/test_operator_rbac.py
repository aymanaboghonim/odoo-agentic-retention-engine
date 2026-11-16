"""#86 operator RBAC (OIDC/JWKS) invariants RBAC-1..RBAC-6.

RBAC-7 and AA-5/AA-6 live in test_approval.py (they need a pending approval); AA-7 lives in
test_execution.py (it needs an approved run). Spec:
docs/specs/operator-rbac-and-expiring-approvals.md.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from engine import approval_api
from engine.approval_api import create_app
from engine.auth import AuthError, AuthSettings, Principal, TokenVerifier
from engine.config import normalize_database_url
from engine.db import RunRepository, create_engine, upgrade_schema
from scripts.dev_oidc import DevIssuer, jwks_for

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

ISSUER = DevIssuer(Path(tempfile.mkdtemp(prefix="dev-oidc-rbac-")))
OTHER = DevIssuer(Path(tempfile.mkdtemp(prefix="dev-oidc-other-")))  # same kid, different key
SECRET = "origin-secret-" + "y" * 32


@pytest.fixture
def db() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    yield engine
    engine.dispose()


def api(db: sa.Engine, *, secret: str | None = None, host: str = "127.0.0.1") -> TestClient:
    settings = ISSUER.settings()
    return TestClient(
        create_app(RunRepository(db), TokenVerifier(settings), origin_verify_secret=secret),
        client=(host, 1234),
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def claims(**changes: Any) -> dict[str, Any]:
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": ISSUER.issuer,
        "sub": "operator-1",
        "client_id": ISSUER.client_id,
        "token_use": "access",
        "iat": now,
        "exp": now + 600,
        ISSUER.roles_claim: ["approver"],
    }
    base.update(changes)
    return {key: value for key, value in base.items() if value is not None}


def sign(payload: dict[str, Any], *, kid: str = "dev-key-1") -> str:
    return jwt.encode(payload, ISSUER.key, algorithm="RS256", headers={"kid": kid})


# --- RBAC-1 -------------------------------------------------------------------------


def test_forged_or_unsigned_tokens_are_rejected(db: sa.Engine) -> None:
    good = ISSUER.token("operator-1", ["approver"])
    header, payload, signature = good.split(".")
    escalated = b64(json.dumps(claims(**{ISSUER.roles_claim: ["admin"]})).encode())
    unsigned = (
        f"{b64(json.dumps({'alg': 'none', 'typ': 'JWT', 'kid': 'dev-key-1'}).encode())}.{payload}."
    )
    # HS256 "signed" with the public key material: the classic algorithm-confusion forgery.
    hs_header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "dev-key-1"}).encode())
    public_bytes = json.dumps(jwks_for(ISSUER.key)).encode()
    mac = hmac.new(public_bytes, f"{hs_header}.{payload}".encode(), hashlib.sha256).digest()
    forged = {
        "wrong key, same kid": OTHER.token("operator-1", ["approver"]),
        "tampered payload": f"{header}.{escalated}.{signature}",
        "alg none": unsigned,
        "HS256 confusion": f"{hs_header}.{payload}.{b64(mac)}",
        "unknown kid": sign(claims(), kid="not-in-jwks"),
        "no kid": jwt.encode(claims(), ISSUER.key, algorithm="RS256"),
        "garbage": "not.a.jwt",
        "empty": "x",
    }
    client = api(db)
    verifier = TokenVerifier(ISSUER.settings())
    for name, token in forged.items():
        assert client.get("/approvals", headers=bearer(token)).status_code == 401, name
        with pytest.raises(AuthError):
            verifier.verify(token)
    assert client.get("/approvals", headers=bearer(good)).status_code == 200
    assert client.get("/approvals", headers={"Authorization": f"Basic {good}"}).status_code == 401
    assert client.get("/approvals").status_code == 401


# --- RBAC-2 -------------------------------------------------------------------------


def test_invalid_claims_are_rejected(db: sa.Engine) -> None:
    now = int(time.time())
    audience = TokenVerifier(ISSUER.settings(audience="approval-api"))
    cases: dict[str, dict[str, Any]] = {
        "expired": claims(exp=now - 120),
        "not yet valid": claims(nbf=now + 3600),
        "wrong issuer": claims(iss="https://evil.example.test"),
        "wrong client": claims(client_id="another-client"),
        "id token": claims(token_use="id"),
        "no token_use": claims(token_use=None),
        "no sub": claims(sub=None),
        "empty sub": claims(sub=""),
        "oversized sub": claims(sub="s" * 129),
        "no exp": claims(exp=None),
        "no iss": claims(iss=None),
    }
    client = api(db)
    for name, payload in cases.items():
        assert client.get("/approvals", headers=bearer(sign(payload))).status_code == 401, name
    # An audience is enforced only when configured; then it must match.
    assert client.get("/approvals", headers=bearer(sign(claims(aud="other")))).status_code == 200
    with pytest.raises(AuthError):
        audience.verify(sign(claims(aud="other")))
    with pytest.raises(AuthError):
        audience.verify(sign(claims()))
    assert audience.verify(sign(claims(aud="approval-api"))).subject == "operator-1"
    # Small clock skew on exp is tolerated; a clearly expired token is not.
    assert client.get("/approvals", headers=bearer(sign(claims(exp=now - 5)))).status_code == 200


# --- RBAC-3 / RBAC-4 -----------------------------------------------------------------


def test_role_matrix(db: sa.Engine) -> None:
    client = api(db)
    missing = uuid.uuid4()
    body = {"snapshot_hash": "0" * 64, "decision": "approve"}
    expected = {"viewer": (200, 403), "approver": (200, 409), "admin": (200, 409)}
    for role, (listing, decision) in expected.items():
        headers = bearer(ISSUER.token(f"{role}-1", [role]))
        assert client.get("/approvals", headers=headers).status_code == listing, role
        status = client.post(f"/approvals/{missing}/decision", headers=headers, json=body)
        # 409 means the role was accepted and the request reached the (stale) approval check.
        assert status.status_code == decision, role
    principal = Principal("p", frozenset({"admin"}))
    assert principal.has("viewer") and principal.has("approver") and principal.has("admin")
    assert not Principal("p", frozenset({"viewer"})).has("approver")
    assert not principal.has("superuser")


def test_missing_or_unknown_roles_fail_closed(db: sa.Engine) -> None:
    client = api(db)
    body = {"snapshot_hash": "0" * 64, "decision": "approve"}
    bad_claims: dict[str, Any] = {
        "empty list": [],
        "unknown role": ["root", "superuser"],
        "wrong case": ["Approver", "ADMIN"],
        "string instead of list": "approver",
        "dict": {"approver": True},
        "list of non-strings": [1, None, ["admin"]],
    }
    for name, roles in bad_claims.items():
        headers = bearer(sign(claims(**{ISSUER.roles_claim: roles})))
        assert client.get("/approvals", headers=headers).status_code == 403, name
        decide = client.post(f"/approvals/{uuid.uuid4()}/decision", headers=headers, json=body)
        assert decide.status_code == 403, name
    no_claim = bearer(sign(claims(**{ISSUER.roles_claim: None})))
    assert client.get("/approvals", headers=no_claim).status_code == 403
    # A role in a differently named claim is ignored.
    other_claim = bearer(sign(claims(**{ISSUER.roles_claim: None, "groups": ["admin"]})))
    assert client.get("/approvals", headers=other_claim).status_code == 403


# --- RBAC-5 -------------------------------------------------------------------------


def test_origin_verify_header(db: sa.Engine, caplog: pytest.LogCaptureFixture) -> None:
    token = ISSUER.token("operator-1", ["approver"])
    # Behind CloudFront the client is not loopback; the secret header is what proves the edge.
    client = api(db, secret=SECRET, host="203.0.113.9")
    ok = client.get("/approvals", headers={**bearer(token), "X-Origin-Verify": SECRET})
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    assert client.get("/approvals", headers=bearer(token)).status_code == 403
    wrong = {**bearer(token), "X-Origin-Verify": SECRET[:-1] + "z"}
    assert client.get("/approvals", headers=wrong).status_code == 403
    # The origin check runs before authentication: a bad token and no secret is 403, not 401.
    assert client.get("/approvals", headers=bearer("garbage")).status_code == 403
    with_secret_bad_token = {**bearer("garbage"), "X-Origin-Verify": SECRET}
    assert client.get("/approvals", headers=with_secret_bad_token).status_code == 401
    assert SECRET not in ok.text and SECRET not in caplog.text
    # Without an origin secret the API stays loopback-only.
    assert api(db, host="203.0.113.9").get("/approvals", headers=bearer(token)).status_code == 403


def test_local_jwks_refused_with_origin_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="OIDC_JWKS_FILE"):
        ISSUER.settings(origin_verify_secret=SECRET)
    monkeypatch.setenv("APPROVAL_API_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="ORIGIN_VERIFY"):
        approval_api._host(ISSUER.settings())
    cloud = AuthSettings(
        issuer="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_EXAMPLE",
        client_id="client",
        roles_claim="cognito:groups",
        jwks_url="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_EXAMPLE/.well-known/jwks.json",
        origin_verify_secret=SECRET,
    )
    assert approval_api._host(cloud) == "0.0.0.0"
    assert SECRET not in repr(cloud)


# --- RBAC-6 -------------------------------------------------------------------------


def test_auth_settings_fail_closed(tmp_path: Path) -> None:
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(jwks_for(ISSUER.key)))
    good = {
        "OIDC_ISSUER": "https://issuer.example.test",
        "OIDC_CLIENT_ID": "client",
        "OIDC_ROLES_CLAIM": "cognito:groups",
        "OIDC_JWKS_FILE": str(jwks),
    }
    settings = AuthSettings.from_env(good)
    assert settings.approval_ttl.total_seconds() == 24 * 3600  # owner default (spec)
    assert AuthSettings.from_env({**good, "APPROVAL_TTL_HOURS": "72"}).approval_ttl.days == 3
    for missing in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_ROLES_CLAIM", "OIDC_JWKS_FILE"):
        with pytest.raises(ValueError):
            AuthSettings.from_env({k: v for k, v in good.items() if k != missing})
    bad_envs = [
        {},
        {**good, "OIDC_JWKS_URL": "https://issuer.example.test/jwks.json"},  # both sources
        {
            **{k: v for k, v in good.items() if k != "OIDC_JWKS_FILE"},
            "OIDC_JWKS_URL": "http://issuer.example.test/jwks.json",
        },  # not https
        {**good, "APPROVAL_TTL_HOURS": "0"},
        {**good, "APPROVAL_TTL_HOURS": "169"},
        {**good, "APPROVAL_TTL_HOURS": "soon"},
        {
            **{k: v for k, v in good.items() if k != "OIDC_JWKS_FILE"},
            "OIDC_JWKS_URL": "https://issuer.example.test/jwks.json",
            "APPROVAL_ORIGIN_VERIFY_SECRET": "short",
        },
    ]
    for env in bad_envs:
        with pytest.raises(ValueError):
            AuthSettings.from_env(env)
    empty = tmp_path / "empty.json"
    empty.write_text('{"keys": []}')
    with pytest.raises(ValueError, match="JWKS"):
        TokenVerifier(AuthSettings.from_env({**good, "OIDC_JWKS_FILE": str(empty)}))
    with pytest.raises(ValueError, match="JWKS"):
        TokenVerifier(AuthSettings.from_env({**good, "OIDC_JWKS_FILE": str(tmp_path / "nope")}))


def test_static_token_removed(db: sa.Engine) -> None:
    assert not hasattr(approval_api, "Operator")
    source = Path(approval_api.__file__).read_text(encoding="utf-8")
    assert "APPROVAL_TOKEN" not in source and "APPROVAL_OPERATOR" not in source
    assert "operator.token" not in source
    legacy = "synthetic-token-" + "x" * 32
    assert api(db).get("/approvals", headers=bearer(legacy)).status_code == 401
