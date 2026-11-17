"""#97 Cognito PKCE helper: hermetic checks of the pieces that do not need a browser."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from pathlib import Path

from scripts.cognito_login import authorize_url, claims, main, pkce_pair, write_private


def test_pkce_pair_is_s256() -> None:
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()


def test_authorize_url_uses_code_flow_with_pkce() -> None:
    url = authorize_url(
        "https://x.auth.us-east-1.amazoncognito.com/",
        "cid",
        "http://localhost:8765/callback",
        "st",
        "ch",
    )
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    assert parsed.path == "/oauth2/authorize"
    assert query == {
        "response_type": "code",
        "client_id": "cid",
        "redirect_uri": "http://localhost:8765/callback",
        "scope": "openid email",
        "state": "st",
        "code_challenge": "ch",
        "code_challenge_method": "S256",
    }


def test_token_file_is_private(tmp_path: Path) -> None:
    target = tmp_path / "dir" / "a.token"
    write_private(target, "value")
    assert target.read_text() == "value"
    assert (target.stat().st_mode & 0o777) == 0o600
    assert (target.parent.stat().st_mode & 0o777) == 0o700


def test_claims_display_only() -> None:
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "s", "cognito:groups": ["viewer"]}).encode()
    ).rstrip(b"=")
    assert claims(f"h.{payload.decode()}.sig")["cognito:groups"] == ["viewer"]


def test_rejects_non_https_domain(tmp_path: Path) -> None:
    assert main(["--domain", "http://x", "--client-id", "c", "--out", str(tmp_path / "t")]) == 1
