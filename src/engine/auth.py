"""OIDC/JWKS access-token verification and role checks (#86, spec RBAC-1..RBAC-7).

Cognito (or the local dev issuer) signs RS256 access tokens. This module verifies the
signature and claims, maps one configured claim to `viewer` / `approver` / `admin`, and
fails closed on anything unexpected. Error messages never include token contents.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import jwt

ROLES = ("viewer", "approver", "admin")  # lowest to highest; a higher role includes lower ones
ALGORITHMS = ["RS256"]
MAX_TOKEN_LENGTH = 8192
MAX_SUBJECT_LENGTH = 128
LEEWAY_SECONDS = 30  # applies to exp/nbf only, never to approval expires_at
MIN_ORIGIN_SECRET_LENGTH = 32
DEFAULT_APPROVAL_TTL_HOURS = 24


class AuthError(Exception):
    """The credentials are missing or invalid (HTTP 401). Never carries token contents."""


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str]

    def has(self, required: str) -> bool:
        """True if any held role is at or above `required`; unknown roles never match."""
        if required not in ROLES:
            return False
        needed = ROLES.index(required)
        return any(role in ROLES and ROLES.index(role) >= needed for role in self.roles)


@dataclass(frozen=True)
class AuthSettings:
    issuer: str
    client_id: str
    roles_claim: str
    jwks_url: str | None = None
    jwks_file: str | None = None
    audience: str | None = None
    origin_verify_secret: str | None = field(default=None, repr=False)
    approval_ttl: timedelta = timedelta(hours=DEFAULT_APPROVAL_TTL_HOURS)

    def __post_init__(self) -> None:
        if not self.issuer.strip() or not self.client_id.strip() or not self.roles_claim.strip():
            raise ValueError("OIDC_ISSUER, OIDC_CLIENT_ID, and OIDC_ROLES_CLAIM must be set")
        if bool(self.jwks_url) == bool(self.jwks_file):
            raise ValueError("set exactly one of OIDC_JWKS_URL or OIDC_JWKS_FILE")
        if self.jwks_url is not None and not self.jwks_url.startswith("https://"):
            raise ValueError("OIDC_JWKS_URL must be an https URL")
        if self.origin_verify_secret is not None:
            secret = self.origin_verify_secret
            if len(secret) < MIN_ORIGIN_SECRET_LENGTH or not secret.isascii():
                raise ValueError("APPROVAL_ORIGIN_VERIFY_SECRET must be 32+ ASCII characters")
            if not secret.isprintable() or any(c.isspace() for c in secret):
                raise ValueError("APPROVAL_ORIGIN_VERIFY_SECRET must be printable, no whitespace")
            if self.jwks_file is not None:
                # The local key file is a dev/CI mode and must never reach the CloudFront path.
                raise ValueError("OIDC_JWKS_FILE cannot be combined with origin verification")
        if not timedelta(hours=1) <= self.approval_ttl <= timedelta(hours=168):
            raise ValueError("APPROVAL_TTL_HOURS must be between 1 and 168")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AuthSettings:
        def value(name: str) -> str | None:
            raw = env.get(name, "").strip()
            return raw or None

        raw_ttl = value("APPROVAL_TTL_HOURS")
        try:
            hours = int(raw_ttl) if raw_ttl else DEFAULT_APPROVAL_TTL_HOURS
        except ValueError as error:
            raise ValueError("APPROVAL_TTL_HOURS must be an integer") from error
        return cls(
            issuer=value("OIDC_ISSUER") or "",
            client_id=value("OIDC_CLIENT_ID") or "",
            roles_claim=value("OIDC_ROLES_CLAIM") or "",
            jwks_url=value("OIDC_JWKS_URL"),
            jwks_file=value("OIDC_JWKS_FILE"),
            audience=value("OIDC_AUDIENCE"),
            origin_verify_secret=value("APPROVAL_ORIGIN_VERIFY_SECRET"),
            approval_ttl=timedelta(hours=hours),
        )


class TokenVerifier:
    """Verifies access tokens against a JWKS (cached URL client or a local file)."""

    def __init__(self, settings: AuthSettings) -> None:
        self._settings = settings
        self._client: jwt.PyJWKClient | None = None
        self._keys: dict[str, Any] = {}
        if settings.jwks_url is not None:
            self._client = jwt.PyJWKClient(settings.jwks_url, cache_keys=True, timeout=5)
        else:
            path = Path(str(settings.jwks_file))
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                keys = jwt.PyJWKSet.from_dict(data).keys
            except (OSError, ValueError, jwt.PyJWTError) as error:
                raise ValueError("OIDC_JWKS_FILE is missing or is not a valid JWKS") from error
            self._keys = {key.key_id: key.key for key in keys if key.key_id}
            if not self._keys:
                raise ValueError("OIDC_JWKS_FILE has no usable signing keys")

    def _signing_key(self, token: str) -> Any:
        if self._client is not None:
            return self._client.get_signing_key_from_jwt(token).key
        kid = jwt.get_unverified_header(token).get("kid")
        key = self._keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise AuthError("invalid credentials")
        return key

    def verify(self, token: str) -> Principal:
        if not token or len(token) > MAX_TOKEN_LENGTH or not token.isascii():
            raise AuthError("invalid credentials")
        settings = self._settings
        try:
            key = self._signing_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                issuer=settings.issuer,
                audience=settings.audience,
                leeway=LEEWAY_SECONDS,
                options={
                    "require": ["exp", "iss", "sub"],
                    "verify_aud": settings.audience is not None,
                },
            )
        except AuthError:
            raise
        except jwt.PyJWTError, KeyError, ValueError, TypeError:
            raise AuthError("invalid credentials") from None
        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not subject
            or len(subject) > MAX_SUBJECT_LENGTH
            or claims.get("token_use") != "access"
            or claims.get("client_id") != settings.client_id
        ):
            raise AuthError("invalid credentials")
        raw_roles = claims.get(settings.roles_claim)
        roles = (
            frozenset(role for role in raw_roles if isinstance(role, str) and role in ROLES)
            if isinstance(raw_roles, list)
            else frozenset()
        )
        return Principal(subject=subject, roles=roles)
