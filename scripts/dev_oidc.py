"""Local and CI OIDC issuer for the approval API (#86). Synthetic dev use only.

It generates an RSA key, writes a JWKS file the API can load with `OIDC_JWKS_FILE`, and mints
short-lived RS256 access tokens that look like Cognito access tokens. It is not a server and
must never be used with the cloud (origin-verify) configuration.

    python -m scripts.dev_oidc init --dir .local/dev-oidc
    APPROVAL_ACCESS_TOKEN=$(python -m scripts.dev_oidc mint --dir .local/dev-oidc \
        --issuer https://dev-oidc.example.test --client-id dev-client \
        --subject dev-operator --role approver)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from engine.auth import AuthSettings

KEY_FILE = "private-key.pem"
JWKS_FILE = "jwks.json"
KEY_ID = "dev-key-1"


def generate_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwks_for(key: rsa.RSAPrivateKey, kid: str = KEY_ID) -> dict[str, Any]:
    numbers = key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "kid": kid,
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        ]
    }


def mint_token(
    key: rsa.RSAPrivateKey,
    *,
    issuer: str,
    client_id: str,
    subject: str,
    roles: Sequence[str],
    roles_claim: str = "cognito:groups",
    expires_in: int = 900,
    token_use: str = "access",
    kid: str = KEY_ID,
    extra: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "client_id": client_id,
        "token_use": token_use,
        "iat": now,
        "exp": now + expires_in,
        roles_claim: list(roles),
    }
    claims.update(extra or {})
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def write_key_files(directory: Path, key: rsa.RSAPrivateKey) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(directory / KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    (directory / JWKS_FILE).write_text(json.dumps(jwks_for(key)), encoding="utf-8")


def load_key(directory: Path) -> rsa.RSAPrivateKey:
    loaded = serialization.load_pem_private_key((directory / KEY_FILE).read_bytes(), password=None)
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise ValueError("dev OIDC key must be an RSA private key")
    return loaded


class DevIssuer:
    """A throwaway issuer for hermetic tests: one key, a JWKS file, matching settings."""

    def __init__(
        self,
        directory: Path,
        *,
        issuer: str = "https://dev-oidc.example.test",
        client_id: str = "dev-client",
        roles_claim: str = "cognito:groups",
    ) -> None:
        self.directory = directory
        self.issuer = issuer
        self.client_id = client_id
        self.roles_claim = roles_claim
        self.key = generate_key()
        write_key_files(directory, self.key)

    def settings(self, **overrides: Any) -> AuthSettings:
        values: dict[str, Any] = {
            "issuer": self.issuer,
            "client_id": self.client_id,
            "roles_claim": self.roles_claim,
            "jwks_file": str(self.directory / JWKS_FILE),
        }
        values.update(overrides)
        return AuthSettings(**values)

    def env(self) -> dict[str, str]:
        """Environment for a subprocess API that trusts this issuer (local file mode)."""
        return {
            "OIDC_ISSUER": self.issuer,
            "OIDC_CLIENT_ID": self.client_id,
            "OIDC_ROLES_CLAIM": self.roles_claim,
            "OIDC_JWKS_FILE": str(self.directory / JWKS_FILE),
        }

    def token(
        self,
        subject: str = "dev-operator",
        roles: Sequence[str] = ("approver",),
        *,
        expires_in: int = 3600,
        **overrides: Any,
    ) -> str:
        values: dict[str, Any] = {
            "issuer": self.issuer,
            "client_id": self.client_id,
            "roles_claim": self.roles_claim,
        }
        values.update(overrides)
        return mint_token(self.key, subject=subject, roles=roles, expires_in=expires_in, **values)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local OIDC issuer for the approval API")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a key and JWKS file (refuses to overwrite)")
    init.add_argument("--dir", type=Path, required=True)
    mint = commands.add_parser("mint", help="print one short-lived access token")
    mint.add_argument("--dir", type=Path, required=True)
    mint.add_argument("--issuer", required=True)
    mint.add_argument("--client-id", required=True)
    mint.add_argument("--subject", required=True)
    mint.add_argument("--role", action="append", default=[])
    mint.add_argument("--roles-claim", default="cognito:groups")
    mint.add_argument("--expires-in", type=int, default=900)
    args = parser.parse_args(argv)
    if args.command == "init":
        write_key_files(args.dir, generate_key())
        print(f"stage=init status=complete jwks={args.dir / JWKS_FILE}")
        return 0
    if not 1 <= args.expires_in <= 3600:
        parser.error("--expires-in must be between 1 and 3600 seconds")
    print(
        mint_token(
            load_key(args.dir),
            issuer=args.issuer,
            client_id=args.client_id,
            subject=args.subject,
            roles=args.role,
            roles_claim=args.roles_claim,
            expires_in=args.expires_in,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
